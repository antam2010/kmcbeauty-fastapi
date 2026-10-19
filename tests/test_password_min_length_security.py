"""비밀번호 최소 길이 8자 테스트 (보안, 사용자 결정 2026-10-17).

대상:
- app/schemas/user.py::UserCreate.password / UserUpdate.password (min_length 4 → 8)
- POST /auth/login (OAuth2PasswordRequestForm, app/services/auth_service.py)

결함: 가입 · 비밀번호 변경이 4자 비밀번호를 허용했다.

요구 동작:
- 가입(POST /users)과 변경(PUT /users/me)은 8자 이상만 받는다(7자 이하 422).
- 로그인은 길이를 검사하지 않는다. 4자 비밀번호로 만든 기존 계정도 그대로 로그인하고,
  짧은 비밀번호도 검증 오류(422)가 아니라 인증(401/200)까지 간다.

라우터만 올린 최소 FastAPI 앱에서 가입 · 수정 서비스는 스텁한다. 로그인은 실제 인증
서비스(해시 검증)를 쓰고 유저 조회와 토큰 발급(Redis 저장)만 스텁한다.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api import auth as auth_api
from app.api import user as user_api
from app.core.rate_limit import limiter
from app.core.security import hash_password
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.enum.role import UserRole
from app.schemas.user import UserCreate, UserResponse, UserUpdate
from app.services import auth_service

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
LEGACY_EMAIL = "legacy@example.com"
# 4자 규칙 시절에 가입한 계정의 비밀번호(테스트용 더미 값)
LEGACY_PLAINTEXT = "1234"
# 토큰 발급 스텁이 돌려주는 더미 JWT
ISSUED_ACCESS, ISSUED_REFRESH = "access-jwt", "refresh-jwt"

USER = UserResponse(
    id=1,
    name="홍길동",
    email="owner@example.com",
    role=UserRole.MASTER,
    created_at=NOW,
    updated_at=NOW,
)
SIGNUP = {"name": "홍길동", "email": "new@example.com"}
UPDATE = {"name": "홍길동", "email": "owner@example.com"}

SHORT_PASSWORDS = ["1234", "1234567", "가나다라마바사"]
SHORT_IDS = ["4-chars", "7-chars", "7-korean-chars"]
EIGHT_CHAR_PASSWORDS = ["12345678", "가나다라마바사아"]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(user_api, "create_user_service", lambda *_a, **_k: USER)
    monkeypatch.setattr(user_api, "update_user_service", lambda *_a, **_k: USER)
    legacy_user = SimpleNamespace(
        id=2,
        email=LEGACY_EMAIL,
        role=UserRole.MASTER,
        password=hash_password(LEGACY_PLAINTEXT),
    )
    monkeypatch.setattr(
        auth_service,
        "get_user_by_email",
        lambda _db, email: legacy_user if email == LEGACY_EMAIL else None,
    )
    monkeypatch.setattr(
        auth_api,
        "generate_tokens",
        lambda _user: (ISSUED_ACCESS, ISSUED_REFRESH),
    )

    api = FastAPI()
    for router in (user_api.router, auth_api.router):
        api.include_router(router)
    api.state.limiter = limiter
    api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    api.dependency_overrides[get_db] = lambda: None
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    return TestClient(api, raise_server_exceptions=False)


def _assert_too_short(response: object) -> None:
    assert response.status_code == 422, response.text
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "password"]
    assert error["type"] == "string_too_short"
    assert error["ctx"]["min_length"] == 8


# ---------------------------------------------------------------------------
# 가입 · 변경: 8자 이상
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("password", SHORT_PASSWORDS, ids=SHORT_IDS)
def test_signup_with_password_under_8_chars_is_422(
    client: TestClient,
    password: str,
) -> None:
    _assert_too_short(client.post("/users", json={**SIGNUP, "password": password}))


@pytest.mark.parametrize("password", SHORT_PASSWORDS, ids=SHORT_IDS)
def test_password_change_under_8_chars_is_422(
    client: TestClient,
    password: str,
) -> None:
    _assert_too_short(client.put("/users/me", json={**UPDATE, "password": password}))


@pytest.mark.parametrize("password", EIGHT_CHAR_PASSWORDS, ids=["8", "8-korean"])
def test_signup_and_change_with_8_chars_succeed(
    client: TestClient,
    password: str,
) -> None:
    signup = client.post("/users", json={**SIGNUP, "password": password})
    change = client.put("/users/me", json={**UPDATE, "password": password})

    assert signup.status_code == 201, signup.text
    assert change.status_code == 200, change.text


def test_profile_update_without_password_is_unaffected(client: TestClient) -> None:
    """비밀번호를 바꾸지 않는 정보 수정은 길이 규칙과 무관하다."""
    assert client.put("/users/me", json=UPDATE).status_code == 200


@pytest.mark.parametrize("schema", [UserCreate, UserUpdate])
def test_min_length_is_documented_in_openapi_schema(
    schema: type[UserCreate | UserUpdate],
) -> None:
    password = schema.model_json_schema()["properties"]["password"]

    assert password["minLength"] == 8
    assert "8자 이상" in password["description"]


# ---------------------------------------------------------------------------
# 로그인: 길이 검사 없음(기존 짧은 비밀번호 계정 유지)
# ---------------------------------------------------------------------------


def _login(client: TestClient, password: str) -> object:
    return client.post(
        "/auth/login",
        data={"username": LEGACY_EMAIL, "password": password},
    )


def test_legacy_account_with_short_password_still_logs_in(client: TestClient) -> None:
    response = _login(client, LEGACY_PLAINTEXT)

    assert response.status_code == 200, response.text
    assert response.json()["access_token"] == ISSUED_ACCESS


@pytest.mark.parametrize("password", ["1", "999"], ids=["1-char", "3-chars"])
def test_short_wrong_password_reaches_authentication_not_validation(
    client: TestClient,
    password: str,
) -> None:
    """짧은 비밀번호도 422 가 아니라 인증 단계의 401 이다."""
    response = _login(client, password)

    assert response.status_code == 401, response.text
