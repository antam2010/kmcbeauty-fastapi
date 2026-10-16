"""시술 자동완료 배치 회귀 테스트.

대상:
- app/crud/treatment_crud.py::get_treatments_to_autocomplete
- worker/tasks/treatment_task.py::auto_complete_treatment
- app/schemas/treatment_item.py::TreatmentItemCreate / TreatmentItemUpdate

결함:
- 조회가 전 상점의 미완료(RESERVED/VISITED, finished_at IS NULL) 예약을 시간 조건과
  ORDER BY 없이 LIMIT 100 으로 가져왔고, 태스크는 실행당 그 한 배치만 처리했다. 미래
  예약이 배치를 채우면 이미 끝난 예약은 영영 완료되지 않았다. 대시보드 실매출은
  COMPLETED 만 세므로 실매출이 과소 집계된다.
- UPDATE 가 id 목록만 조건으로 써서, 조회 직후 취소된 예약도 COMPLETED 로 덮어썼다.
- duration_min 상한이 없어 거대한 값이 timedelta 계산을 넘쳐 배치 전체가 실패했다.

요구 동작:
- 종료 시각(reserved_at + 항목 소요시간 합)이 now(UTC) 이하인 예약만 SQL 에서 고르고,
  reserved_at 순으로 배치를 소진할 때까지(상한 있는 루프) 처리한다.
- UPDATE 는 자동완료 대상 상태(VISITED)와 finished_at IS NULL 을 다시 확인한다.
- 항목 입력값에 범위를 둔다(duration_min 0~1440, base_price 0 이상). 위반은 422 다.
- 자동완료 대상은 직원이 방문 처리한 VISITED 뿐이다(제품 결정,
  test_treatment_autocomplete_visited_only_fix.py). 그래서 이 파일의 예약은 기본
  VISITED 다.

tests/fake_sql.py 평가기로 실제 조회문과 UPDATE 조건을 DB 없이 실행한다.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.crud import treatment_crud
from app.enum.treatment_status import TreatmentStatus
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from app.schemas.treatment_item import TreatmentItemCreate, TreatmentItemUpdate
from tests.fake_sql import FakeSQLSession
from worker.tasks import treatment_task

# 태스크 비교 기준(UTC naive). reserved_at 컬럼도 naive UTC 로 저장된다.
NOW = datetime(2026, 7, 2, 12, 0)  # noqa: DTZ001  # naive UTC 컬럼 규약


def _treatment(
    treatment_id: int,
    reserved_at: datetime,
    *durations: int,
    status: TreatmentStatus = TreatmentStatus.VISITED,
) -> list[object]:
    """예약 1건과 소요시간별 항목 행을 만든다(상점은 id 로 분산, 기본 상태 VISITED)."""
    treatment = Treatment(
        id=treatment_id,
        shop_id=10 + treatment_id % 3,
        reserved_at=reserved_at,
        status=status,
        finished_at=None,
    )
    items = [
        TreatmentItem(
            id=treatment_id * 10 + seq,
            treatment_id=treatment_id,
            menu_detail_id=1,
            base_price=10000,
            duration_min=minutes,
            session_no=1,
        )
        for seq, minutes in enumerate(durations)
    ]
    return [treatment, *items]


def _future(first_id: int, count: int) -> list[object]:
    """아직 시작하지 않은 미래 예약 count 건(항목 60분)."""
    rows: list[object] = []
    for offset in range(count):
        reserved_at = NOW + timedelta(hours=offset + 1)
        rows += _treatment(first_id + offset, reserved_at, 60)
    return rows


def _finished(first_id: int, count: int) -> list[object]:
    """이미 끝난 예약 count 건(3시간 전 시작 + 60분)."""
    rows: list[object] = []
    for offset in range(count):
        reserved_at = NOW - timedelta(hours=3, minutes=offset)
        rows += _treatment(first_id + offset, reserved_at, 60)
    return rows


@pytest.fixture
def run_task(monkeypatch: pytest.MonkeyPatch) -> object:
    """가짜(fake) 세션과 고정된 현재 시각으로 자동완료 태스크를 실행하는 함수."""
    monkeypatch.setattr(treatment_task, "now_utc", lambda: NOW.replace(tzinfo=UTC))

    def _run(db: FakeSQLSession) -> FakeSQLSession:
        monkeypatch.setattr(treatment_task, "SessionLocal", lambda: db)
        treatment_task.auto_complete_treatment()
        return db

    return _run


def _status(db: FakeSQLSession, treatment_id: int) -> TreatmentStatus:
    return next(t.status for t in db.rows("treatment") if t.id == treatment_id)


def _completed_ids(db: FakeSQLSession) -> list[int]:
    return sorted(
        t.id for t in db.rows("treatment") if t.status == TreatmentStatus.COMPLETED
    )


def test_finished_treatment_completes_despite_150_future_ones(run_task: object) -> None:
    """미래 예약 150건이 먼저 있어도 한 번의 실행으로 끝난 예약이 완료된다."""
    db = FakeSQLSession(
        *_future(1, 150),
        *_treatment(999, NOW - timedelta(hours=3), 60),
    )

    run_task(db)

    assert _completed_ids(db) == [999]
    finished = next(t for t in db.rows("treatment") if t.id == 999)
    assert finished.finished_at == NOW
    assert db.commit_count >= 1


def test_one_run_drains_every_batch(run_task: object) -> None:
    """끝난 예약이 배치 크기(100)를 넘어도 한 번의 실행으로 모두 완료된다."""
    db = FakeSQLSession(*_finished(1, 250))

    run_task(db)

    assert _completed_ids(db) == list(range(1, 251))


def test_batch_loop_is_bounded(
    run_task: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """배치 반복에는 상한이 있다(상한 2 → 실행당 최대 200건)."""
    monkeypatch.setattr(treatment_task, "MAX_BATCHES_PER_RUN", 2)
    db = FakeSQLSession(*_finished(1, 250))

    run_task(db)

    assert len(_completed_ids(db)) == 200


def test_treatment_cancelled_between_select_and_update_stays_cancelled(
    run_task: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """조회 직후 취소된 예약은 UPDATE 가 COMPLETED 로 덮어쓰지 않는다."""
    db = FakeSQLSession(*_finished(1, 2))
    real_select = treatment_crud.get_treatments_to_autocomplete

    def select_then_cancel(*args: object, **kwargs: object) -> list:
        rows = real_select(*args, **kwargs)
        # 다른 요청이 조회와 UPDATE 사이에 예약 1을 취소한다.
        cancelled = next(t for t in db.rows("treatment") if t.id == 1)
        cancelled.status = TreatmentStatus.CANCELLED
        return rows

    monkeypatch.setattr(
        treatment_task,
        "get_treatments_to_autocomplete",
        select_then_cancel,
    )

    run_task(db)

    assert _status(db, 1) == TreatmentStatus.CANCELLED
    assert next(t for t in db.rows("treatment") if t.id == 1).finished_at is None
    assert _status(db, 2) == TreatmentStatus.COMPLETED


def test_in_progress_itemless_and_finished_states_are_left_alone(
    run_task: object,
) -> None:
    """진행 중·항목 없음·이미 종결(취소/노쇼) 예약은 건드리지 않는다(정책 유지)."""
    db = FakeSQLSession(
        *_treatment(1, NOW - timedelta(minutes=10), 60),  # 진행 중
        *_treatment(2, NOW - timedelta(hours=3)),  # 항목 없음
        *_treatment(3, NOW - timedelta(hours=3), 60, status=TreatmentStatus.CANCELLED),
        *_treatment(4, NOW - timedelta(hours=3), 60, status=TreatmentStatus.NO_SHOW),
        *_treatment(5, NOW - timedelta(hours=3), 60, status=TreatmentStatus.VISITED),
        *_treatment(6, NOW - timedelta(minutes=60), 30, 30),  # 합 60분: 지금 종료
    )

    run_task(db)

    assert _completed_ids(db) == [5, 6]
    assert _status(db, 1) == TreatmentStatus.VISITED
    assert _status(db, 2) == TreatmentStatus.VISITED
    assert _status(db, 3) == TreatmentStatus.CANCELLED
    assert _status(db, 4) == TreatmentStatus.NO_SHOW


def test_select_orders_oldest_first_and_limits_batch() -> None:
    """조회는 reserved_at(→id) 오름차순이며 배치 크기로 자른다."""
    db = FakeSQLSession(*_finished(1, 5))

    rows = treatment_crud.get_treatments_to_autocomplete(db, NOW, limit=3)

    # _finished 는 id 가 클수록 더 이른 시각에 시작했다.
    assert [row.treatment_id for row in rows] == [5, 4, 3]


# ---------------------------------------------------------------------------
# 항목 입력 범위: 위반은 FastAPI 본문 검증 오류(422)이며 500·overflow 가 아니다.
# ---------------------------------------------------------------------------

_VALID_ITEM = {"menu_detail_id": 1, "base_price": 0, "duration_min": 0, "session_no": 1}


@pytest.mark.parametrize("schema", [TreatmentItemCreate, TreatmentItemUpdate])
@pytest.mark.parametrize(
    "overrides",
    [
        {"duration_min": 1441},
        {"duration_min": -1},
        {"duration_min": 10**12},
        {"base_price": -1},
        {"base_price": 2**31},
    ],
    ids=[
        "duration-over-1day",
        "negative-duration",
        "huge-duration",
        "neg-price",
        "int4",
    ],
)
def test_item_schema_rejects_out_of_range_values(
    schema: type,
    overrides: dict,
) -> None:
    with pytest.raises(ValidationError):
        schema(**{**_VALID_ITEM, **overrides})


@pytest.mark.parametrize("schema", [TreatmentItemCreate, TreatmentItemUpdate])
def test_item_schema_accepts_boundary_values(schema: type) -> None:
    item = schema(**{**_VALID_ITEM, "duration_min": 1440, "base_price": 2**31 - 1})
    assert (item.duration_min, item.base_price) == (1440, 2**31 - 1)
