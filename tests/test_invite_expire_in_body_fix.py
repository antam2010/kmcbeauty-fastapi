"""초대코드 만료 기간(expire_in)을 JSON 본문에서 읽는 교정 테스트.

대상:
- app/api/shop.py::create_invite_link (POST /shops/{shop_id}/invites)
- app/schemas/shop_invite.py::ShopInviteCreateRequest

결함: 라우트가 ShopInviteCreateRequest 를 Depends() 로 받아 expire_in 을 쿼리 파라미터로
읽었다. 앱(kmcbeauty-native src/api/services/invite.ts)은 { expire_in } 을 JSON 본문으로
보낸다. 본문은 무시되어 앱이 고른 기간(1시간~30일)과 상관없이 모든 초대코드가 기본값
7일로 만들어졌다.

요구 동작(사용자 결정): 본문을 읽는다. 본문이 없거나 {} 이면 기본값 7일이다. 범위
(60초~30일) 검증 · 응답 · rate limit(IP 당 10/minute)은 그대로다. OpenAPI 에는
requestBody 가 있고 expire_in 쿼리 파라미터는 없다.

라우터와 limiter 만 올린 최소 FastAPI 앱에서 인증 · DB 의존성을 바꾸고, 서비스를 스텁해
서비스가 받은 expire_in 을 기록한다.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api import shop as shop_api
from app.core.rate_limit import limiter
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.schemas.shop_invite import ShopInviteCreateRequest, ShopInviteResponse

SHOP_ID = 10
INVITES = f"/shops/{SHOP_ID}/invites"
NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
SEVEN_DAYS = 7 * 24 * 60 * 60

# 앱(InviteCodeGeneratorModal)의 만료 선택지(초): 1시간 · 6시간 · 12시간 · 1일 · 3일 ·
# 7일 · 30일.
APP_EXPIRE_IN_OPTIONS = [3600, 21600, 43200, 86400, 259200, 604800, 2592000]


@pytest.fixture
def invite_calls(monkeypatch: pytest.MonkeyPatch) -> list[ShopInviteCreateRequest]:
    """초대코드 생성 서비스가 받은 요청 모델을 기록한다."""
    calls: list[ShopInviteCreateRequest] = []

    def _generate(**kwargs: object) -> ShopInviteResponse:
        calls.append(kwargs["invite_data"])
        return ShopInviteResponse(
            shop_id=SHOP_ID,
            invite_code="AbCdEfGhIjK",
            expired_at=NOW,
        )

    monkeypatch.setattr(shop_api, "generate_invite_code_service", _generate)
    return calls


def _api() -> FastAPI:
    """샵 라우터 + limiter 를 main.py 와 같이 올린 앱(인증 · DB 의존성 대체)."""
    api = FastAPI()
    api.include_router(shop_api.router)
    api.state.limiter = limiter
    api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    api.dependency_overrides[get_db] = lambda: None
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    return api


@pytest.fixture
def client(
    invite_calls: list[ShopInviteCreateRequest],  # noqa: ARG001  # 서비스 스텁 적용
) -> TestClient:
    return TestClient(_api(), raise_server_exceptions=False)


def test_body_expire_in_reaches_service(
    client: TestClient,
    invite_calls: list[ShopInviteCreateRequest],
) -> None:
    """앱처럼 본문으로 보낸 3600 초가 서비스에 그대로 간다(응답은 그대로 201)."""
    response = client.post(INVITES, json={"expire_in": 3600})

    assert response.status_code == 201, response.text
    assert [call.expire_in for call in invite_calls] == [3600]
    assert response.json() == {
        "shop_id": SHOP_ID,
        "invite_code": "AbCdEfGhIjK",
        "expired_at": "2026-10-17T09:00:00Z",
    }


@pytest.mark.parametrize(
    "expire_in",
    APP_EXPIRE_IN_OPTIONS,
    ids=["1h", "6h", "12h", "1d", "3d", "7d", "30d"],
)
def test_app_expire_in_options_reach_service(
    client: TestClient,
    invite_calls: list[ShopInviteCreateRequest],
    expire_in: int,
) -> None:
    """앱의 선택지 7개는 모두 검증을 통과해 그 값으로 서비스에 간다."""
    response = client.post(INVITES, json={"expire_in": expire_in})

    assert response.status_code == 201, response.text
    assert [call.expire_in for call in invite_calls] == [expire_in]


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {},
        {"json": {}},
        {"content": b"null", "headers": {"content-type": "application/json"}},
    ],
    ids=["no-body", "empty-object", "json-null-body"],
)
def test_missing_or_empty_body_uses_seven_day_default(
    client: TestClient,
    invite_calls: list[ShopInviteCreateRequest],
    request_kwargs: dict,
) -> None:
    """본문이 없거나 {} 이면 기본값 7일(604800초)이다."""
    response = client.post(INVITES, **request_kwargs)

    assert response.status_code == 201, response.text
    assert [call.expire_in for call in invite_calls] == [SEVEN_DAYS]


@pytest.mark.parametrize(
    "expire_in",
    [59, 0, -1, 2_592_001, None],
    ids=["59s", "zero", "negative", "over-30d", "null"],
)
def test_out_of_range_body_is_422(
    client: TestClient,
    invite_calls: list[ShopInviteCreateRequest],
    expire_in: int | None,
) -> None:
    """범위(60초~30일) 밖 값과 null 은 본문 필드의 422 이고 서비스에 가지 않는다.

    null 을 허용하면 서비스의 timedelta(seconds=None) 가 TypeError(500)다.
    """
    response = client.post(INVITES, json={"expire_in": expire_in})

    assert response.status_code == 422, response.text
    assert [tuple(e["loc"]) for e in response.json()["detail"]] == [
        ("body", "expire_in"),
    ]
    assert invite_calls == []


def test_openapi_has_request_body_and_no_expire_in_query() -> None:
    """문서도 본문 계약이다: requestBody(선택)가 있고 expire_in 쿼리 파라미터는 없다."""
    spec = _api().openapi()
    operation = spec["paths"]["/shops/{shop_id}/invites"]["post"]

    assert [(p["name"], p["in"]) for p in operation["parameters"]] == [
        ("shop_id", "path"),
    ]
    body = operation["requestBody"]
    assert body.get("required") is not True  # 본문 생략 가능(기본값 7일)
    assert body["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/ShopInviteCreateRequest"
    )
    expire_in = spec["components"]["schemas"]["ShopInviteCreateRequest"]["properties"][
        "expire_in"
    ]
    assert expire_in["type"] == "integer"
    assert (expire_in["minimum"], expire_in["maximum"]) == (60, 2_592_000)
    assert expire_in["default"] == SEVEN_DAYS


def test_rate_limit_still_applies_to_body_requests(client: TestClient) -> None:
    """본문 요청도 IP 당 분당 10회까지만 받는다(11번째는 429)."""
    allowed = [
        client.post(INVITES, json={"expire_in": 3600}).status_code for _ in range(10)
    ]
    over = client.post(INVITES, json={"expire_in": 3600})

    assert allowed == [201] * 10
    assert over.status_code == 429
    assert over.json() == {"error": "Rate limit exceeded: 10 per 1 minute"}
