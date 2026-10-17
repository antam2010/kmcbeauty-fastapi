"""토큰 · 세션 하드닝 테스트 (보안).

대상:
- app/services/auth_service.py::refresh_access_token
  - type=refresh 가 아닌 토큰(액세스 · type 없음)으로는 재발급할 수 없다(401).
    Redis 대조가 이미 막지만 그 앞단의 심층 방어다.
- app/services/user_service.py::update_user_service
  - 리프레시 토큰 폐기와 유저 캐시 삭제는 커밋이 성공한 뒤에만 한다.
    커밋 전에 지우면 실패해도 세션이 끊기고, 유저 캐시를 안 지우면 바뀐
    role/email/name 이 캐시 TTL(24h) 동안 이전 값으로 남는다.
- app/api/auth.py::logout — 응답 본문에 리프레시 토큰을 되돌려주지 않는다.

DB/Redis 없이 검증한다. Redis 헬퍼는 monkeypatch 로 스텁하고 커밋은 이벤트 순서를
기록하는 스파이 세션으로 관찰한다. 토큰은 실제 발급 함수로 만든다.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.api import auth as auth_api
from app.core.config import settings
from app.core.security import create_jwt_token
from app.enum.role import UserRole
from app.exceptions import CustomException
from app.schemas.user import UserUpdate
from app.services import auth_service, user_service

USER_ID = 7
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
REFRESH_COOKIE = "refresh-token-in-cookie"


class FakeUser:
    """토큰 발급 · 재발급 · 정보 수정 응답이 참조하는 최소 User 인터페이스."""

    def __init__(self) -> None:
        self.id = USER_ID
        self.role = UserRole.MASTER
        self.email = "owner@test.com"
        self.name = "원장님"
        self.created_at = NOW
        self.updated_at = NOW

    def is_deleted(self) -> bool:
        return False


class FakeRequest:
    """refresh_access_token 이 읽는 headers/cookies 만 흉내낸다."""

    def __init__(self, refresh_token: str) -> None:
        self.headers = {}
        self.cookies = {"refresh_token": refresh_token}


# ---------------------------------------------------------------------------
# 재발급은 type=refresh 토큰만
# ---------------------------------------------------------------------------


@pytest.fixture
def saved_refresh(monkeypatch: pytest.MonkeyPatch) -> dict:
    """refresh_access_token 의 Redis · DB 의존을 스텁한다(store 가 Redis 저장값)."""
    store: dict[int, str] = {}
    user = FakeUser()
    monkeypatch.setattr(auth_service, "get_refresh_token_redis", store.get)
    monkeypatch.setattr(auth_service, "set_refresh_token_redis", store.__setitem__)
    monkeypatch.setattr(
        auth_service,
        "get_refresh_token_ttl",
        lambda _user_id: settings.REFRESH_TOKEN_EXPIRE_SECONDS,
    )
    monkeypatch.setattr(auth_service, "get_user_by_id", lambda _db, _uid: user)
    return store


@pytest.mark.parametrize("kind", ["access", "untyped"])
def test_non_refresh_token_cannot_refresh(saved_refresh: dict, kind: str) -> None:
    """액세스 · type 없는 토큰은 Redis 저장값과 같아도 재발급 401 이다(심층 방어)."""
    if kind == "access":
        token = auth_service.generate_access_token(FakeUser())
    else:
        token = create_jwt_token({"sub": str(USER_ID)}, timedelta(days=1))
    saved_refresh[USER_ID] = token

    with pytest.raises(CustomException) as exc:
        auth_service.refresh_access_token(db=None, request=FakeRequest(token))

    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "AUTH_REFRESH_TOKEN_INVALID"


def test_refresh_token_still_refreshes(saved_refresh: dict) -> None:
    """대조군: Redis 에 저장된 리프레시 토큰으로는 그대로 재발급된다."""
    token = auth_service.generate_refresh_token(FakeUser())
    saved_refresh[USER_ID] = token

    access, refresh = auth_service.refresh_access_token(
        db=None,
        request=FakeRequest(token),
    )

    assert access
    assert refresh == token


# ---------------------------------------------------------------------------
# 정보 수정: 세션 · 캐시 정리는 커밋 성공 뒤에만
# ---------------------------------------------------------------------------


class EventSession:
    """commit/refresh/rollback 을 이벤트 순서로 기록하는 스파이(커밋 실패 주입 가능)."""

    def __init__(self, events: list, commit_error: Exception | None = None) -> None:
        self.events = events
        self.commit_error = commit_error

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.events.append("commit")

    def refresh(self, _obj: object) -> None:
        self.events.append("refresh")

    def rollback(self) -> None:
        self.events.append("rollback")


@pytest.fixture
def update_events(monkeypatch: pytest.MonkeyPatch) -> list:
    """update_user_service 의 조회 · Redis 의존을 이벤트 기록 스텁으로 대체한다."""
    events: list = []
    user = FakeUser()
    monkeypatch.setattr(user_service, "get_user_by_id", lambda _db, _uid: user)
    monkeypatch.setattr(
        user_service,
        "clear_refresh_token_redis",
        lambda user_id: events.append(f"clear_refresh:{user_id}"),
    )
    monkeypatch.setattr(
        user_service,
        "clear_user_redis",
        lambda user_id: events.append(f"clear_user:{user_id}"),
    )
    return events


def _update(db: EventSession) -> object:
    return user_service.update_user_service(
        db=db,
        user_update=UserUpdate(name="새 이름", email="new-owner@test.com"),
        current_user=SimpleNamespace(id=USER_ID),
    )


def test_update_clears_refresh_and_user_cache_after_commit(
    update_events: list,
) -> None:
    """수정이 커밋되면 리프레시 토큰과 유저 캐시를 둘 다, 커밋 뒤에 지운다."""
    response = _update(EventSession(update_events))

    commit_at = update_events.index("commit")
    assert update_events.index(f"clear_refresh:{USER_ID}") > commit_at
    assert update_events.index(f"clear_user:{USER_ID}") > commit_at
    assert (response.name, response.email) == ("새 이름", "new-owner@test.com")


@pytest.mark.parametrize(
    ("commit_error", "status_code"),
    [
        (IntegrityError("UPDATE users", {}, Exception("duplicate email")), 409),
        (SQLAlchemyError("commit failed"), 500),
    ],
    ids=["duplicate-email", "db-error"],
)
def test_update_commit_failure_keeps_session_and_cache(
    update_events: list,
    commit_error: Exception,
    status_code: int,
) -> None:
    """커밋이 실패하면 리프레시 토큰도 유저 캐시도 지우지 않는다."""
    with pytest.raises(CustomException) as exc:
        _update(EventSession(update_events, commit_error=commit_error))

    assert exc.value.status_code == status_code
    assert [e for e in update_events if e.startswith("clear_")] == []


# ---------------------------------------------------------------------------
# 로그아웃: 응답 본문에 리프레시 토큰을 싣지 않는다
# ---------------------------------------------------------------------------


def test_logout_body_does_not_echo_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """로그아웃은 쿠키의 토큰으로 폐기하되, 응답 본문에는 토큰을 되돌려주지 않는다."""
    revoked: list = []
    monkeypatch.setattr(
        auth_api,
        "logout_user",
        lambda token: revoked.append(token) or True,
    )
    api = FastAPI()
    api.include_router(auth_api.router)
    client = TestClient(api)
    client.cookies.set("refresh_token", REFRESH_COOKIE)

    response = client.post("/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"message": "로그아웃 성공", "result": True}
    assert REFRESH_COOKIE not in response.text
    assert revoked == [REFRESH_COOKIE]
    assert 'refresh_token=""' in response.headers["set-cookie"]
