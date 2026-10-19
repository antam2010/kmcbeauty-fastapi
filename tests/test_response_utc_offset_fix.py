"""초대코드 · 대시보드 응답 일시의 UTC 오프셋 교정 테스트.

대상:
- app/schemas/shop_invite.py::ShopInviteResponse (POST/GET /shops/{shop_id}/invites)
- app/schemas/dashboard.py 응답 스키마 (GET /summary/dashboard,
  DashboardCustomerInsight.reserved_at)

결함: 두 스키마가 일반 BaseModel 이라 DB 의 naive UTC 일시가 오프셋 없이 직렬화됐다
("2026-10-22T01:00:00"). 앱(new Date)은 이를 현지 시각으로 읽어 9시간 어긋났다.
/treatments 는 BaseResponseModel(naive 일시에 UTC 부착)이라 "...Z" 로 나간다.

요구 동작: 두 응답의 일시도 Z(+00:00)를 붙여 직렬화한다. 그 밖의 응답 모양(키 · 값)은
그대로다. 대시보드는 캐시 미스 · 히트와 배포 전 캐시(오프셋 없는 문자열) 모두 같다.

DB 없이 검증한다. 초대 서비스가 import 한 CRUD 는 monkeypatch 하고, 대시보드는 실제
통계 CRUD 를 tests/fake_sql.py 위에서 실행한다(Redis 는 conftest 의 메모리 구현).
"""

import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.api import shop as shop_api
from app.api import summary as summary_api
from app.core.rate_limit import limiter
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.shop import get_current_shop
from app.enum.treatment_status import PaymentMethod, TreatmentStatus
from app.models.shop_invite import ShopInvite
from app.models.treatment import Treatment
from app.schemas.dashboard import DashboardCustomerInsight, DashboardSummaryResponse
from app.services import shop_invite_service
from app.utils.redis import dashboard as dashboard_cache
from tests.fake_sql import FakeSQLSession

SHOP_ID = 10
OWNER = SimpleNamespace(id=1)
# DB 저장 규약: naive UTC. 2026-10-22 01:00 UTC = 2026-10-22 10:00 KST
EXPIRED_AT_NAIVE_UTC = datetime(2026, 10, 22, 1, 0)  # noqa: DTZ001  # naive UTC
OCT_15 = date(2026, 10, 15)


def _is_utc_string(value: str) -> bool:
    return value.endswith(("Z", "+00:00"))


class FakeSession:
    """초대 서비스가 쓰는 Session API(commit/rollback/refresh)."""

    def commit(self) -> None:
        """커밋할 상태 없음."""

    def rollback(self) -> None:
        """롤백할 상태 없음."""

    def refresh(self, obj: object) -> None:
        """DB 재조회 없음(저장값은 naive UTC 그대로)."""


def _invite() -> ShopInvite:
    return ShopInvite(
        id=1,
        shop_id=SHOP_ID,
        invite_code="AbCdEfGhIjK",
        expired_at=EXPIRED_AT_NAIVE_UTC,
    )


@pytest.fixture
def invite_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """대표 원장으로 초대코드를 만들고 조회하는 클라이언트(CRUD 는 스텁)."""
    monkeypatch.setattr(
        shop_invite_service,
        "get_shop_user",
        lambda *_args: SimpleNamespace(is_primary_owner=1),
    )
    monkeypatch.setattr(
        shop_invite_service,
        "get_valid_invite_by_shop_id",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        shop_invite_service,
        "delete_invite_by_shop_id",
        lambda *_args: None,
    )
    monkeypatch.setattr(shop_invite_service, "create_invite", lambda *_args: _invite())
    monkeypatch.setattr(
        shop_invite_service,
        "get_invite_by_shop_id",
        lambda *_args: _invite(),
    )
    api = FastAPI()
    api.include_router(shop_api.router)
    api.state.limiter = limiter
    api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    api.dependency_overrides[get_db] = FakeSession
    api.dependency_overrides[get_current_user] = lambda: OWNER
    return TestClient(api)


@pytest.mark.parametrize(
    ("method", "expected_status"),
    [("POST", 201), ("GET", 200)],
    ids=["create", "get"],
)
def test_invite_expired_at_is_serialized_as_utc(
    invite_client: TestClient,
    method: str,
    expected_status: int,
) -> None:
    """만료 일시는 UTC 오프셋(Z)을 달고, 다른 필드 · 키는 그대로다."""
    response = invite_client.request(method, f"/shops/{SHOP_ID}/invites")

    assert response.status_code == expected_status, response.text
    assert response.json() == {
        "shop_id": SHOP_ID,
        "invite_code": "AbCdEfGhIjK",
        "expired_at": "2026-10-22T01:00:00Z",
    }


# ---------------------------------------------------------------------------
# 대시보드 고객 인사이트의 예약 일시
# ---------------------------------------------------------------------------


def _treatment(treatment_id: int, hour: int) -> Treatment:
    return Treatment(
        id=treatment_id,
        shop_id=SHOP_ID,
        reserved_at=datetime(2026, 10, 15, hour, 0),  # noqa: DTZ001  # naive UTC
        status=TreatmentStatus.RESERVED,
        payment_method=PaymentMethod.CARD,
        customer_name=f"고객{treatment_id}",
        created_at=EXPIRED_AT_NAIVE_UTC,
        updated_at=EXPIRED_AT_NAIVE_UTC,
    )


@pytest.fixture
def dashboard_client() -> TestClient:
    """10월 15일(KST) 예약 2건이 있는 샵의 대시보드 클라이언트."""
    db = FakeSQLSession(_treatment(1, 1), _treatment(2, 5))
    api = FastAPI()
    api.include_router(summary_api.router)
    api.dependency_overrides[get_db] = lambda: db
    api.dependency_overrides[get_current_shop] = lambda: SimpleNamespace(id=SHOP_ID)
    return TestClient(api)


def _insights(client: TestClient) -> list[dict]:
    response = client.get("/summary/dashboard", params={"target_date": "2026-10-15"})
    assert response.status_code == 200, response.text
    return response.json()["customer_insights"]


def test_dashboard_reserved_at_is_utc_on_cache_miss_and_hit(
    dashboard_client: TestClient,
) -> None:
    """캐시 미스(DB 조회)와 히트(캐시 복원) 모두 예약 일시가 Z 로 나간다."""
    miss = _insights(dashboard_client)
    hit = _insights(dashboard_client)

    expected = ["2026-10-15T01:00:00Z", "2026-10-15T05:00:00Z"]
    assert [item["reserved_at"] for item in miss] == expected
    assert [item["reserved_at"] for item in hit] == expected


def test_dashboard_reserved_at_is_utc_for_cache_written_before_fix(
    dashboard_client: TestClient,
) -> None:
    """배포 전 캐시(오프셋 없는 문자열)도 응답에서는 UTC 로 나간다."""
    legacy = DashboardCustomerInsight(
        id=9,
        reserved_at=datetime(2026, 10, 15, 3, 0),  # noqa: DTZ001  # naive UTC
        status="RESERVED",
        treatments=[],
        total_duration_min=0,
        total_price=0,
        payment_method="CARD",
        total_reservations=1,
        no_show_count=0,
        no_show_rate=0.0,
        unpaid_amount=0,
        total_spent=0,
    ).model_dump(mode="json")
    legacy["reserved_at"] = "2026-10-15T03:00:00"  # 수정 전 직렬화 형태
    key = dashboard_cache.get_dashboard_cache_key(
        SHOP_ID,
        0,
        "customer_insight",
        dashboard_cache.day_period(OCT_15),
    )
    dashboard_cache.redis_client.set(key, json.dumps([legacy]))

    [item] = _insights(dashboard_client)

    assert item["reserved_at"] == "2026-10-15T03:00:00Z"


def test_dashboard_response_shape_is_unchanged(dashboard_client: TestClient) -> None:
    """키 구성은 응답 스키마 필드 그대로다(일시 문자열만 오프셋이 붙는다)."""
    response = dashboard_client.get(
        "/summary/dashboard",
        params={"target_date": "2026-10-15"},
    )
    body = response.json()

    assert set(body) == set(DashboardSummaryResponse.model_fields)
    assert body["target_date"] == "2026-10-15"
    [first, _second] = body["customer_insights"]
    assert set(first) == set(DashboardCustomerInsight.model_fields)
    assert (first["id"], first["customer_name"], first["status"]) == (
        1,
        "고객1",
        "RESERVED",
    )
    insights = body["customer_insights"]
    assert all(_is_utc_string(item["reserved_at"]) for item in insights)
