"""Redis 장애 대응 테스트.

대상:
- app/utils/redis/user.py::get_user_redis · set_user_redis
  (app/dependencies/auth.py::get_current_user 의 유저 캐시 조회 · 저장)
- app/core/redis_client.py — 소켓 연결 · 응답 타임아웃
- app/exceptions.py::redis_error_handler 와 app/main.py 등록

결함: Redis 가 죽으면 인증이 필요한 모든 요청이 500 이었다(get_current_user 가 캐시
조회 · 저장 오류를 그대로 던졌다). Redis 오류 처리기가 없어 로그인 · 재발급 · 로그아웃
(리프레시 토큰은 Redis 에만 있다)과 rate limit 저장소 오류도 500 이었다. 응답 없는
Redis 는 타임아웃이 없어 워커를 붙잡았다.

요구 동작(사용자 결정):
- 유저 캐시 조회 · 저장 오류는 경고 로그(user_id 만)를 남기고 DB 로 인증한다.
- 캐시 삭제 · 리프레시 토큰 헬퍼는 그대로 오류를 던진다(조용히 넘기면 세션 · 캐시가
  남는다).
- 그 밖의 처리되지 않은 Redis 오류는 표준 오류 본문의 503 이고 Sentry 로 보낸다.
- Redis 클라이언트는 연결 · 응답을 2초까지만 기다린다.

DB · Redis 없이 검증한다. Redis 클라이언트는 명령마다 오류를 던지는 가짜로 바꾸고,
HTTP 경로는 실제 앱(app.main)의 미들웨어 · 예외 처리기를 그대로 탄다.
"""

import importlib
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import NoReturn

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app import exceptions as exceptions_module
from app.api import auth as auth_api
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.redis_client import redis_client
from app.database import get_db
from app.enum.role import UserRole
from app.models.user import User
from app.services import auth_service
from app.utils.redis import auth as refresh_cache
from app.utils.redis import shop as selected_shop_cache
from app.utils.redis import user as user_cache
from tests.fake_sql import FakeSQLSession

USER_ID = 7
EMAIL = "owner@test.com"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
_DUMMY_HASH = "not-a-real-hash"
REDIS_DOWN = "Error 111 connecting to redis:6379. Connection refused."
# 처리되지 않은 Redis 오류의 503 본문(계약). 원문(호스트 · 포트)은 싣지 않는다.
UNAVAILABLE = {
    "detail": {
        "code": "SERVICE_UNAVAILABLE",
        "detail": "일시적으로 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    },
}
USER_CACHE_LOGGER = "app.utils.redis.user"


class DownRedis:
    """모든 명령이 연결 오류인 Redis(장애)."""

    def __getattr__(self, _command: str) -> Callable[..., NoReturn]:
        def fail(*_args: object, **_kwargs: object) -> NoReturn:
            raise redis.ConnectionError(REDIS_DOWN)

        return fail


class FullRedis:
    """읽기는 캐시 미스, 쓰기는 거절(maxmemory 초과)인 Redis."""

    def get(self, _key: str) -> None:
        return None

    def setex(self, *_args: object) -> NoReturn:
        msg = "OOM command not allowed when used memory > 'maxmemory'."
        raise redis.ResponseError(msg)


def _user() -> User:
    return User(
        id=USER_ID,
        email=EMAIL,
        name="원장님",
        password=_DUMMY_HASH,
        role=UserRole.MASTER,
        created_at=NOW,
        updated_at=NOW,
    )


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_service.generate_access_token(_user())}"}


def _server_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    """Sentry 전송을 기록한다: CustomException(5xx) · 처리되지 않은 예외(미들웨어)."""
    events: list[BaseException] = []
    monkeypatch.setattr(exceptions_module, "capture_exception", events.append)
    monkeypatch.setattr(settings, "LOG_TO_FILE", False)
    main_module = importlib.import_module("app.main")
    monkeypatch.setattr(main_module, "capture_exception", events.append)
    return events


@pytest.fixture
def main_app(
    sentry_events: list[BaseException],  # noqa: ARG001  # Sentry 기록 스텁 적용
) -> Iterator[FastAPI]:
    """실제 앱(app.main). DB 는 이 유저 한 명이 있는 가짜 세션이다."""
    main = importlib.import_module("app.main").app
    saved = dict(main.dependency_overrides)
    db = FakeSQLSession(_user())
    main.dependency_overrides[get_db] = lambda: db
    yield main
    main.dependency_overrides.clear()
    main.dependency_overrides.update(saved)


@pytest.fixture
def client(main_app: FastAPI) -> TestClient:
    return TestClient(main_app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 유저 캐시: 조회 · 저장 오류는 DB 로 인증한다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cache", "failed_commands"),
    [
        pytest.param(DownRedis, 1, id="read-fails-write-skipped"),
        pytest.param(FullRedis, 1, id="write-through-fails"),
    ],
)
def test_user_cache_failure_falls_back_to_db(  # noqa: PLR0913  # pytest 픽스처 · 파라미터
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentry_events: list[BaseException],
    cache: type,
    failed_commands: int,
) -> None:
    """캐시 조회 · 저장이 실패해도 인증 요청은 DB 로 인증해 200 이다.

    실패마다 경고 로그를 남기되 인자는 user_id 하나뿐이다(email 등 PII 없음).
    """
    monkeypatch.setattr(user_cache, "redis_client", cache())
    caplog.set_level(logging.WARNING)

    response = client.get("/users/me", headers=_bearer())

    assert response.status_code == 200, response.text
    assert response.json()["email"] == EMAIL
    warnings = [r for r in caplog.records if r.name == USER_CACHE_LOGGER]
    assert [r.levelno for r in warnings] == [logging.WARNING] * failed_commands
    assert [r.args for r in warnings] == [(USER_ID,)] * failed_commands
    assert EMAIL not in caplog.text
    assert _server_logs(caplog) == []
    assert sentry_events == []


@pytest.mark.parametrize(
    "helper",
    [
        pytest.param(lambda: user_cache.clear_user_redis(USER_ID), id="clear-user"),
        pytest.param(
            lambda: refresh_cache.set_refresh_token_redis(USER_ID, "token"),
            id="set-refresh",
        ),
        pytest.param(
            lambda: refresh_cache.get_refresh_token_redis(USER_ID),
            id="get-refresh",
        ),
        pytest.param(
            lambda: refresh_cache.get_refresh_token_ttl(USER_ID),
            id="refresh-ttl",
        ),
        pytest.param(
            lambda: refresh_cache.clear_refresh_token_redis(USER_ID),
            id="clear-refresh",
        ),
    ],
)
def test_session_and_cache_clearing_helpers_still_raise(
    monkeypatch: pytest.MonkeyPatch,
    helper: Callable[[], object],
) -> None:
    """대조군: 캐시 삭제 · 리프레시 토큰 헬퍼는 Redis 오류를 삼키지 않는다."""
    monkeypatch.setattr(user_cache, "redis_client", DownRedis())
    monkeypatch.setattr(refresh_cache, "redis_client", DownRedis())

    with pytest.raises(redis.RedisError):
        helper()


# ---------------------------------------------------------------------------
# 처리되지 않은 Redis 오류는 503(표준 본문 · Sentry 전송)
# ---------------------------------------------------------------------------


def test_main_app_registers_redis_error_handler(main_app: FastAPI) -> None:
    """처리되지 않은 Redis 오류를 처리하는 전역 처리기가 실제 앱에 등록되어 있다."""
    handler = exceptions_module.redis_error_handler

    assert main_app.exception_handlers[redis.RedisError] is handler


AUTH_REQUESTS = [
    pytest.param("/auth/login", False, id="login"),
    pytest.param("/auth/refresh", True, id="refresh"),
    pytest.param("/auth/logout", True, id="logout"),
]


@pytest.mark.parametrize(("path", "with_refresh_cookie"), AUTH_REQUESTS)
def test_auth_endpoints_are_503_while_redis_is_down(  # noqa: PLR0913  # pytest 픽스처
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentry_events: list[BaseException],
    path: str,
    with_refresh_cookie: bool,
) -> None:
    """리프레시 토큰은 Redis 에만 있어 로그인 · 재발급 · 로그아웃은 503 이다.

    처리되지 않은 예외(500 · ERROR 로그)가 아니고, 5xx 이므로 Sentry 로 한 번 보낸다.
    """
    monkeypatch.setattr(refresh_cache, "redis_client", DownRedis())
    monkeypatch.setattr(user_cache, "redis_client", DownRedis())
    monkeypatch.setattr(
        auth_api,
        "authenticate_user_service",
        lambda _db, _email, _password: _user(),
    )
    if with_refresh_cookie:
        client.cookies.set(
            "refresh_token",
            auth_service.generate_refresh_token(_user()),
        )

    response = client.post(path, data={"username": EMAIL, "password": "pass1234"})

    assert response.status_code == 503, response.text
    assert response.json() == UNAVAILABLE
    assert "redis:6379" not in response.text
    assert [type(e) for e in sentry_events] == [redis.ConnectionError]
    assert _server_logs(caplog) == []


def test_shop_scoped_request_is_503_while_selected_shop_is_unreadable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: list[BaseException],
) -> None:
    """인증은 DB 로 넘어가지만 선택 샵은 Redis 에만 있어 샵 범위 API 는 503 이다."""
    monkeypatch.setattr(user_cache, "redis_client", DownRedis())
    monkeypatch.setattr(selected_shop_cache, "redis_client", DownRedis())

    response = client.get(
        "/summary/dashboard",
        params={"target_date": "2026-11-01"},
        headers=_bearer(),
    )

    assert response.status_code == 503, response.text
    assert response.json() == UNAVAILABLE
    assert [type(e) for e in sentry_events] == [redis.ConnectionError]


def test_rate_limit_storage_error_is_503(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: list[BaseException],
) -> None:
    """한도(rate limit) 저장소가 Redis 오류를 던지면 한도가 걸린 API 도 503 이다.

    한도 검사는 엔드포인트 본문보다 먼저라 로그인 처리(인증)까지 가지 않는다.
    """
    attempts: list[str] = []

    def storage_down(*_args: object, **_kwargs: object) -> NoReturn:
        raise redis.ConnectionError(REDIS_DOWN)

    monkeypatch.setattr(limiter.limiter.storage, "incr", storage_down)
    monkeypatch.setattr(
        auth_api,
        "authenticate_user_service",
        lambda _db, email, _password: attempts.append(email),
    )

    response = client.post(
        "/auth/login",
        data={"username": EMAIL, "password": "pass1234"},
    )

    assert response.status_code == 503, response.text
    assert response.json() == UNAVAILABLE
    assert attempts == []
    assert [type(e) for e in sentry_events] == [redis.ConnectionError]


# ---------------------------------------------------------------------------
# 응답 없는 Redis 는 2초 안에 실패한다
# ---------------------------------------------------------------------------


def test_redis_client_gives_up_within_two_seconds() -> None:
    """연결 · 응답 대기 상한이 2초다(없으면 응답 없는 Redis 가 워커를 붙잡는다)."""
    kwargs = redis_client.connection_pool.connection_kwargs

    assert kwargs.get("socket_connect_timeout") == 2
    assert kwargs.get("socket_timeout") == 2
