"""유저 캐시 조회가 실패한 인증 요청의 캐시 저장 생략 테스트 (Redis 장애 지연).

대상:
- app/utils/redis/user.py::get_user_redis (조회 오류는 경고 로그 뒤 다시 던진다)
- app/dependencies/auth.py::get_current_user (조회 오류면 DB 로 인증, 저장 생략)

결함: 응답 없는 Redis 는 명령마다 2초(socket_timeout) 뒤에 실패한다(from_url
클라이언트는 재시도하지 않는다). get_user_redis 가 조회 오류를 삼켜 미스(None)로
돌려주면 get_current_user 는 DB 로 읽은 뒤 set_user_redis 로 다시 쓰고, 이 쓰기도
2초를 기다린다. 그래서 Redis 가 멈추면 인증이 필요한 요청마다 4초가 걸렸다.

요구 동작(사용자 결정, 2026-11-10):
- get_user_redis 는 같은 경고 로그(user_id 만, exc_info)를 남긴 뒤 Redis 오류를 다시
  던진다.
- get_current_user 는 그 오류를 캐시 미스로 보고 DB 로 인증하되, 그 요청에서는 캐시
  저장(set_user_redis)을 하지 않는다.
- 조회는 됐는데 값이 없는 보통 미스는 지금처럼 캐시에 쓴다. set_user_redis 는 그대로다.

DB · Redis 없이 검증한다. DB 는 유저 한 명이 있는 가짜 세션(tests/fake_sql.py), Redis 는
받은 명령 이름을 기록하는 가짜 클라이언트다. HTTP 경로는 실제 앱(app.main)을 탄다.
"""

import importlib
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest
import redis
from fastapi.testclient import TestClient

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.core.config import settings
from app.database import get_db
from app.enum.role import UserRole
from app.models.user import User
from app.services import auth_service
from app.utils.redis import user as user_cache
from tests.fake_sql import FakeSQLSession

USER_ID = 7
EMAIL = "owner@test.com"
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
_DUMMY_HASH = "not-a-real-hash"
USER_CACHE_LOGGER = "app.utils.redis.user"
# Redis 장애 종류: 연결 거부(즉시 실패) · 응답 없음(socket_timeout 2초 뒤 실패).
REDIS_FAILURES = [
    pytest.param(
        redis.ConnectionError,
        "Error 111 connecting to redis:6379. Connection refused.",
        id="connection-refused",
    ),
    pytest.param(redis.TimeoutError, "Timeout reading from socket", id="timeout"),
]


class RecordingRedis:
    """받은 명령 이름을 차례로 기록하는 가짜 Redis.

    error 를 주면 모든 명령이 그 오류를 던진다(장애). 없으면 정상 Redis 처럼 get 은 캐시
    미스(None)이고 그 밖의 명령(setex)은 받아들인다.
    """

    def __init__(self, error: redis.RedisError | None = None) -> None:
        self.commands: list[str] = []
        self.error = error

    def __getattr__(self, command: str) -> Callable[..., None]:
        def run(*_args: object, **_kwargs: object) -> None:
            self.commands.append(command)
            if self.error is not None:
                raise self.error

        return run


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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """실제 앱(app.main). DB 는 이 유저 한 명이 있는 가짜 세션이다."""
    monkeypatch.setattr(settings, "LOG_TO_FILE", False)
    main = importlib.import_module("app.main").app
    saved = dict(main.dependency_overrides)
    db = FakeSQLSession(_user())
    main.dependency_overrides[get_db] = lambda: db
    yield TestClient(main, raise_server_exceptions=False)
    main.dependency_overrides.clear()
    main.dependency_overrides.update(saved)


@pytest.mark.parametrize(("error_type", "message"), REDIS_FAILURES)
def test_failed_cache_read_skips_cache_write(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[redis.RedisError],
    message: str,
) -> None:
    """캐시 조회가 Redis 오류면 DB 로 인증해 200 이고, 캐시에 다시 쓰지 않는다.

    응답 없는 Redis 는 명령마다 2초를 기다리므로 Redis 로 가는 명령은 실패한 조회(get)
    한 번뿐이어야 한다(저장 setex 까지 보내면 요청이 4초 걸린다).
    """
    cache = RecordingRedis(error_type(message))
    monkeypatch.setattr(user_cache, "redis_client", cache)

    response = client.get("/users/me", headers=_bearer())

    assert response.status_code == 200, response.text
    assert response.json()["email"] == EMAIL
    assert cache.commands == ["get"]


def test_plain_cache_miss_still_writes_cache(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대조군: 조회는 됐는데 값이 없으면(보통 미스) DB 로 읽은 유저를 캐시에 쓴다."""
    cache = RecordingRedis()
    monkeypatch.setattr(user_cache, "redis_client", cache)

    response = client.get("/users/me", headers=_bearer())

    assert response.status_code == 200, response.text
    assert cache.commands == ["get", "setex"]


@pytest.mark.parametrize(("error_type", "message"), REDIS_FAILURES)
def test_cache_read_error_is_logged_then_reraised(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[redis.RedisError],
    message: str,
) -> None:
    """get_user_redis 는 같은 경고(user_id 만, exc_info)를 남긴 뒤 오류를 다시 던진다.

    get_current_user 는 이 오류로 조회 실패를 알고 그 요청의 캐시 저장을 건너뛴다.
    """
    monkeypatch.setattr(user_cache, "redis_client", RecordingRedis(error_type(message)))
    caplog.set_level(logging.WARNING, logger=USER_CACHE_LOGGER)

    with pytest.raises(error_type):
        user_cache.get_user_redis(USER_ID)

    records = [r for r in caplog.records if r.name == USER_CACHE_LOGGER]
    assert [(r.levelno, r.args) for r in records] == [(logging.WARNING, (USER_ID,))]
    assert records[0].exc_info is not None
    assert records[0].exc_info[0] is error_type
