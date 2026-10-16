"""대시보드 캐시 키 충돌 · 쓰기 후 무효화 회귀 테스트.

대상:
- app/services/summary.py::get_dashboard_summary_service
- app/utils/redis/dashboard.py
- 무효화 호출: app/services/treatment_service.py(생성/수정/취소),
  worker/tasks/treatment_task.py(자동완료)

결함:
- 캐시 키가 dashboard:{shop_id}:{field}:{period} 이고 period 는 날짜뿐이었다. 1일을
  조회하면 일 키와 월 키(month_start)가 summary/sales/staff_summary 에서 같아져, 월간
  통계에 하루치가 나오거나 그 반대가 됐다.
- TTL 30분 동안 force_refresh 외에는 무효화가 없어 예약 생성·수정·취소·자동완료가
  대시보드에 반영되지 않았다.

요구 동작:
- 키에 기간 종류를 넣는다(day:YYYY-MM-DD / month:YYYY-MM).
- 쓰기 커밋이 성공한 뒤 상점별 캐시 버전을 올려 다음 일반 조회가 새 데이터를 본다.
  커밋 전에는 무효화하지 않는다. Redis 오류가 커밋된 쓰기를 실패로 만들지 않는다.

실제 통계 CRUD 를 tests/fake_sql.py 위에서 실행하고 Redis 는 dict 기반 가짜로 바꾼다.
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
import redis
from sqlalchemy.exc import SQLAlchemyError

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.enum.treatment_status import PaymentMethod, TreatmentStatus
from app.exceptions import CustomException
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from app.models.treatment_menu import TreatmentMenu
from app.models.treatment_menu_detail import TreatmentMenuDetail
from app.schemas.dashboard import DashboardFilter
from app.schemas.treatment import TreatmentCreate, TreatmentUpdate
from app.services import summary, treatment_service
from app.utils.redis import dashboard as dashboard_cache
from tests.fake_sql import FakeSQLSession
from worker.tasks import treatment_task

SHOP_ID = 10
SHOP = SimpleNamespace(id=SHOP_ID, user_id=1)
REQUESTER = SimpleNamespace(id=1)
OCT_1, OCT_15 = date(2026, 10, 1), date(2026, 10, 15)


class FakeRedis:
    """대시보드 캐시가 쓰는 Redis 명령(get/set/delete/incr)의 dict 구현."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.store: dict[str, str] = {}
        self.events = events if events is not None else []

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:  # noqa: ARG002
        self.store[key] = value

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)

    def incr(self, key: str) -> int:
        self.events.append("invalidate")
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])


@pytest.fixture
def cache(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(dashboard_cache, "redis_client", fake)
    return fake


def _kst_morning(day: date) -> datetime:
    """해당 KST 날짜 10:00(= 01:00 UTC)의 naive UTC 값."""
    return datetime(day.year, day.month, day.day, 1, 0)  # noqa: DTZ001  # naive UTC


def _treatment(treatment_id: int, day: date, status: TreatmentStatus) -> list[object]:
    """항목 1개(메뉴 상세 1100)를 가진 예약."""
    return [
        Treatment(
            id=treatment_id,
            shop_id=SHOP_ID,
            reserved_at=_kst_morning(day),
            status=status,
            payment_method=PaymentMethod.CARD,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        TreatmentItem(
            id=treatment_id * 10,
            treatment_id=treatment_id,
            menu_detail_id=1100,
            base_price=10000,
            duration_min=60,
            session_no=1,
        ),
    ]


def _world() -> FakeSQLSession:
    """10월 1일 1건, 10월 15일 2건 → 일 1 / 일 2 / 월 3."""
    return FakeSQLSession(
        TreatmentMenu(id=1000, shop_id=SHOP_ID, name="메뉴"),
        TreatmentMenuDetail(
            id=1100,
            menu_id=1000,
            name="젤네일",
            duration_min=60,
            base_price=10000,
        ),
        *_treatment(1, OCT_1, TreatmentStatus.RESERVED),
        *_treatment(2, OCT_15, TreatmentStatus.RESERVED),
        *_treatment(3, OCT_15, TreatmentStatus.RESERVED),
    )


def _dashboard(db: FakeSQLSession, day: date) -> dict:
    """캐시를 거치는 일반 조회(force_refresh 아님)."""
    params = DashboardFilter(target_date=day, force_refresh=False)
    return summary.get_dashboard_summary_service(db, SHOP, params)


def _totals(result: dict) -> tuple[int, int]:
    """(조회일 총 예약, 월 총 예약)."""
    return (
        result["summary"]["target_date"]["total_reservations"],
        result["summary"]["month"]["total_reservations"],
    )


def _sales_counts(result: dict) -> tuple[int, int]:
    """(조회일 메뉴 1100 건수, 월 메뉴 1100 건수)."""
    day = {
        item["menu_detail_id"]: item["count"] for item in result["sales"]["target_date"]
    }
    month = {item["menu_detail_id"]: item["count"] for item in result["sales"]["month"]}
    return day.get(1100, 0), month.get(1100, 0)


# ---------------------------------------------------------------------------
# 1일 조회: 일 키와 월 키가 섞이지 않는다(조회 순서 무관)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("cache")
def test_first_day_then_mid_month_keep_day_and_month_apart() -> None:
    db = _world()

    first = _dashboard(db, OCT_1)
    mid = _dashboard(db, OCT_15)

    assert _totals(first) == (1, 3)
    assert _sales_counts(first) == (1, 3)
    assert _totals(mid) == (2, 3)
    assert _sales_counts(mid) == (2, 3)


@pytest.mark.usefixtures("cache")
def test_mid_month_then_first_day_keep_day_and_month_apart() -> None:
    db = _world()

    mid = _dashboard(db, OCT_15)
    first = _dashboard(db, OCT_1)

    assert _totals(mid) == (2, 3)
    assert _totals(first) == (1, 3)
    assert _sales_counts(first) == (1, 3)


def test_cache_keys_carry_period_kind(cache: FakeRedis) -> None:
    _dashboard(_world(), OCT_1)

    keys = set(cache.store)
    assert any(key.endswith(":summary:day:2026-10-01") for key in keys), keys
    assert any(key.endswith(":summary:month:2026-10") for key in keys), keys


# ---------------------------------------------------------------------------
# 쓰기 커밋 후 다음 일반 조회가 새 데이터를 본다
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("cache")
def test_create_invalidates_dashboard() -> None:
    db = _world()
    assert _totals(_dashboard(db, OCT_15)) == (2, 3)  # 캐시에 적재

    payload = TreatmentCreate(
        reserved_at=datetime(2026, 10, 15, 2, 0, tzinfo=UTC),
        status=TreatmentStatus.RESERVED,
        treatment_items=[
            {
                "menu_detail_id": 1100,
                "base_price": 0,
                "duration_min": 30,
                "session_no": 1,
            },
        ],
    )
    treatment_service.upsert_treatment_service(db, payload, SHOP, REQUESTER)

    assert _totals(_dashboard(db, OCT_15)) == (3, 4)


@pytest.mark.usefixtures("cache")
def test_update_invalidates_dashboard() -> None:
    db = _world()
    assert _dashboard(db, OCT_15)["summary"]["target_date"]["completed"] == 0

    payload = TreatmentUpdate(
        reserved_at=datetime(2026, 10, 15, 1, 0, tzinfo=UTC),
        status=TreatmentStatus.COMPLETED,
    )
    treatment_service.upsert_treatment_service(db, payload, SHOP, REQUESTER, 2)

    assert _dashboard(db, OCT_15)["summary"]["target_date"]["completed"] == 1


@pytest.mark.usefixtures("cache")
def test_cancel_invalidates_dashboard() -> None:
    db = _world()
    assert _dashboard(db, OCT_15)["summary"]["month"]["canceled"] == 0

    treatment_service.cancel_treatment_service(db, SHOP, 3)

    assert _dashboard(db, OCT_15)["summary"]["month"]["canceled"] == 1


@pytest.mark.usefixtures("cache")
def test_autocomplete_invalidates_affected_shop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _world()
    # 자동완료 대상은 직원이 방문 처리한 예약(VISITED)뿐이다.
    for treatment in db.rows("treatment"):
        if treatment.id in {2, 3}:
            treatment.status = TreatmentStatus.VISITED
    assert _dashboard(db, OCT_15)["summary"]["target_date"]["completed"] == 0

    # 10월 15일 저녁(UTC) 기준이면 그날 오전 예약 2건은 이미 끝났다.
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(treatment_task, "now_utc", lambda: now)
    monkeypatch.setattr(treatment_task, "SessionLocal", lambda: db)
    treatment_task.auto_complete_treatment()

    assert _dashboard(db, OCT_15)["summary"]["target_date"]["completed"] == 2


# ---------------------------------------------------------------------------
# 커밋 성공 후에만 무효화 · Redis 장애가 커밋된 쓰기를 실패로 만들지 않음
# ---------------------------------------------------------------------------


def test_invalidation_happens_after_commit(
    cache: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _world()
    real_commit = db.commit

    def commit() -> None:
        real_commit()
        cache.events.append("commit")

    monkeypatch.setattr(db, "commit", commit)

    treatment_service.cancel_treatment_service(db, SHOP, 3)

    assert cache.events == ["commit", "invalidate"]


def test_failed_commit_does_not_invalidate(
    cache: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _world()
    assert _totals(_dashboard(db, OCT_15)) == (2, 3)

    def failing_commit() -> None:
        msg = "commit failed"
        raise SQLAlchemyError(msg)

    monkeypatch.setattr(db, "commit", failing_commit)
    with pytest.raises(CustomException):
        treatment_service.cancel_treatment_service(db, SHOP, 3)

    assert cache.events == []


def test_redis_error_on_invalidation_does_not_fail_committed_write(
    cache: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _world()

    def broken_incr(_key: str) -> int:
        msg = "redis down"
        raise redis.ConnectionError(msg)

    monkeypatch.setattr(cache, "incr", broken_incr)

    treatment_service.cancel_treatment_service(db, SHOP, 3)

    assert db.commit_count == 1
    assert next(t for t in db.rows("treatment") if t.id == 3).status == "CANCELLED"
