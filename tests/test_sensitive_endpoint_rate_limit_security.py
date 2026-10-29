"""민감 엔드포인트 rate limit 테스트 (보안).

대상(slowapi, IP 기준 — app/core/rate_limit.py 의 limiter):
- POST /users (가입)                5/minute
- GET  /users/exists/email          20/minute
- POST /auth/refresh                30/minute
- POST /shops/{shop_id}/invites     10/minute
- POST /device-tokens/send-fcm      10/minute

한도까지는 통과하고 한도를 넘는 첫 요청은 429 다. TestClient 의 클라이언트 주소는 늘
같으므로 같은 IP 의 연속 요청이다. 저장소는 memory:// 이고 conftest 의 autouse
픽스처가 테스트마다 카운터를 비운다(다른 테스트와 독립).

DB · 인증 · 서비스는 스텁한다. 라우터 · limiter · 429 핸들러는 main.py 와 같은 방식으로
등록한다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api import auth as auth_api
from app.api import device_push_token as device_push_token_api
from app.api import shop as shop_api
from app.api import user as user_api
from app.core.rate_limit import limiter
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.enum.role import UserRole
from app.schemas.device_push_token import FCMMessageResponse
from app.schemas.shop_invite import ShopInviteResponse
from app.schemas.user import UserEmailCheckResponse, UserResponse

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _returning(value: object) -> Callable[..., object]:
    """호출 인자와 무관하게 value 를 돌려주는 서비스 스텁."""
    return lambda *_args, **_kwargs: value


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """민감 엔드포인트 라우터 + limiter 를 main.py 와 같이 올린 클라이언트."""
    monkeypatch.setattr(
        user_api,
        "create_user_service",
        _returning(
            UserResponse(
                id=1,
                name="홍길동",
                email="new@test.com",
                role=UserRole.MASTER,
                created_at=NOW,
                updated_at=NOW,
            ),
        ),
    )
    monkeypatch.setattr(
        user_api,
        "check_user_email_service",
        _returning(UserEmailCheckResponse(exists=False, message="사용 가능")),
    )
    monkeypatch.setattr(
        auth_api,
        "refresh_access_token",
        _returning(("new-access", "new-refresh")),
    )
    monkeypatch.setattr(
        shop_api,
        "generate_invite_code_service",
        _returning(
            ShopInviteResponse(shop_id=1, invite_code="AbCdEfGhIjK", expired_at=NOW),
        ),
    )
    monkeypatch.setattr(
        device_push_token_api,
        "send_fcm_notification_service",
        _returning(FCMMessageResponse(success=True, message_id="m-1")),
    )

    api = FastAPI()
    for router in (
        user_api.router,
        auth_api.router,
        shop_api.router,
        device_push_token_api.router,
    ):
        api.include_router(router)
    api.state.limiter = limiter
    api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    api.dependency_overrides[get_db] = lambda: None
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    return TestClient(api)


SIGNUP = {"name": "홍길동", "email": "new@test.com", "password": "pass1234"}
PUSH = {"shop_id": 1, "title": "예약 알림", "body": "10시 예약"}


@pytest.mark.parametrize(
    ("method", "path", "request_kwargs", "limit"),
    [
        pytest.param("POST", "/users", {"json": SIGNUP}, 5, id="signup"),
        pytest.param(
            "GET",
            "/users/exists/email",
            {"params": {"email": "new@test.com"}},
            20,
            id="email-exists",
        ),
        pytest.param("POST", "/auth/refresh", {}, 30, id="refresh"),
        pytest.param("POST", "/shops/1/invites", {}, 10, id="shop-invite"),
        pytest.param(
            "POST",
            "/device-tokens/send-fcm",
            {"json": PUSH},
            10,
            id="send-fcm",
        ),
    ],
)
def test_exceeding_limit_returns_429(
    client: TestClient,
    method: str,
    path: str,
    request_kwargs: dict,
    limit: int,
) -> None:
    """같은 IP 에서 한도까지는 통과하고, 한도를 넘는 첫 요청은 429 다."""
    allowed = [
        client.request(method, path, **request_kwargs).status_code for _ in range(limit)
    ]
    over = client.request(method, path, **request_kwargs)

    assert all(code < 400 for code in allowed), allowed
    assert over.status_code == 429
    assert over.json() == {"error": f"Rate limit exceeded: {limit} per 1 minute"}
