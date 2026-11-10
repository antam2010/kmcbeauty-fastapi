"""유저 이름 길이 · 공백 이름 교정 테스트.

대상: app/schemas/user.py 의 UserBase(name) · UserCreate · UserUpdate

결함: 이름 최소 길이가 3자라 두 글자 한국어 이름("이산")은 가입(POST /users)과
내 정보 수정(PUT /users/me)이 422 였다. 응답 스키마도 같은 UserBase 를 상속해 2자
이름을 담지 못했다. 반대로 공백 세 칸("   ")은 3자라 이름으로 저장됐다.

요구 동작(사용자 결정, 2026-11-10):
- 이름은 1~50자다. 입력(가입 · 수정)과 응답(UserResponse 등)이 함께 바뀐다.
- 공백만 있는 이름은 빈 값으로 보고 입력에서 422 다. 저장 값은 손대지 않는다
  (앞뒤 공백을 지우지 않는다).
- 공백 검사는 입력에만 둔다. 저장된 이름이 공백뿐이어도 응답은 그대로 나간다
  (응답 검증 실패로 500 이 되지 않게).

DB 없이 검증한다. 유저 라우터와 limiter 를 main.py 와 같이 올리고, 서비스는 받은
이름을 그대로 담아 돌려주는 스텁이다(응답 스키마 검증까지 탄다).
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.api import user as user_api
from app.core.rate_limit import limiter
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.enum.role import UserRole
from app.schemas.user import (
    UserBase,
    UserBaseResponse,
    UserCreate,
    UserResponse,
    UserUpdate,
)

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
PASSWORD = "pass1234"  # noqa: S105  # 테스트 픽스처 값


def _signup(name: str) -> dict[str, str]:
    return {"name": name, "email": "new@example.com", "password": PASSWORD}


def _profile(name: str) -> dict[str, str]:
    return {"name": name, "email": "owner@example.com"}


def _stored_user(name: str) -> dict[str, object]:
    """DB 에서 읽은 유저 한 명(응답 스키마 입력)."""
    return {
        "id": 1,
        "name": name,
        "email": "owner@example.com",
        "role": UserRole.MASTER,
        "created_at": NOW,
        "updated_at": NOW,
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """유저 라우터 + limiter. 서비스는 요청의 이름을 담은 응답을 돌려준다."""

    def _create(_db: object, user: UserCreate) -> UserResponse:
        return UserResponse(**_stored_user(user.name))

    def _update(_db: object, user: UserUpdate, _current: object) -> UserResponse:
        return UserResponse(**_stored_user(user.name))

    monkeypatch.setattr(user_api, "create_user_service", _create)
    monkeypatch.setattr(user_api, "update_user_service", _update)

    api = FastAPI()
    api.include_router(user_api.router)
    api.state.limiter = limiter
    api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    api.dependency_overrides[get_db] = lambda: None
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    return TestClient(api)


ACCEPTED_NAMES = [
    pytest.param("이산", id="two-hangul"),
    pytest.param("김", id="one-hangul"),
    pytest.param("Al", id="two-latin"),
    pytest.param("가" * 50, id="50-chars"),
    # 앞뒤 공백은 지우지 않고 그대로 저장 · 응답한다.
    pytest.param(" 이산 ", id="kept-as-is"),
]
REJECTED_NAMES = [
    pytest.param("", id="empty"),
    pytest.param("   ", id="spaces-only"),
    pytest.param("\t \n", id="whitespace-only"),
    pytest.param("가" * 51, id="51-chars"),
]


# ---------------------------------------------------------------------------
# 입력: 1~50자는 받고, 빈 값 · 공백뿐 · 51자는 422
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ACCEPTED_NAMES)
def test_short_name_is_accepted_on_signup(client: TestClient, name: str) -> None:
    """두 글자 · 한 글자 이름도 가입되고, 응답에 그 이름이 그대로 나간다."""
    response = client.post("/users", json=_signup(name))

    assert response.status_code == 201, response.text
    assert response.json()["name"] == name


@pytest.mark.parametrize("name", ACCEPTED_NAMES)
def test_short_name_is_accepted_on_profile_update(
    client: TestClient,
    name: str,
) -> None:
    """내 정보 수정(PUT /users/me)도 같은 규칙이다."""
    response = client.put("/users/me", json=_profile(name))

    assert response.status_code == 200, response.text
    assert response.json()["name"] == name


@pytest.mark.parametrize("name", REJECTED_NAMES)
def test_blank_or_too_long_name_is_422(client: TestClient, name: str) -> None:
    """빈 값 · 공백뿐 · 51자 이름은 가입과 수정 모두 name 필드 오류 422 다."""
    signup = client.post("/users", json=_signup(name))
    update = client.put("/users/me", json=_profile(name))

    assert (signup.status_code, update.status_code) == (422, 422)
    for response in (signup, update):
        errors = response.json()["detail"]
        assert [error["loc"][-1] for error in errors] == ["name"], errors


# ---------------------------------------------------------------------------
# 응답: 2자 이름을 담고, 공백 검사는 하지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema",
    [
        # UserBase 는 예약 응답의 staff_user 타입이기도 하다.
        pytest.param(UserBase, id="UserBase"),
        pytest.param(UserBaseResponse, id="UserBaseResponse"),
        pytest.param(UserResponse, id="UserResponse"),
    ],
)
def test_two_letter_name_fits_response_schemas(schema: type[BaseModel]) -> None:
    """응답 스키마도 UserBase 를 상속하므로 2자 이름을 그대로 담는다."""
    assert schema.model_validate(_stored_user("이산")).name == "이산"


def test_blank_check_is_input_only() -> None:
    """대조군: 저장된 이름이 공백뿐이어도 응답 검증은 통과한다(500 이 되지 않는다)."""
    assert UserResponse.model_validate(_stored_user("   ")).name == "   "


def test_name_length_is_documented_in_openapi_schema() -> None:
    """API 문서(OpenAPI)의 이름 길이도 1~50자다."""
    for schema in (UserCreate, UserUpdate):
        name = schema.model_json_schema()["properties"]["name"]
        assert (name["minLength"], name["maxLength"]) == (1, 50), schema.__name__
