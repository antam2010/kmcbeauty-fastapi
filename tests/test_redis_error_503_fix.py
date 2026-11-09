"""Redis 오류가 넓은 except 에서 500 이 되지 않고 503 처리기에 닿는지 테스트.

대상(PR #6 리뷰 M1 · M2):
- app/services/shop_service.py::set_selected_shop_service · get_selected_shop_service ·
  delete_selected_shop_service (POST · GET · DELETE /shops/selected)
- app/services/user_service.py::update_user_service (PUT /users/me 의 커밋 뒤 세션 ·
  캐시 정리)

결함: 이 서비스들은 try 본문에서 Redis 를 쓰면서 `except Exception` 으로 모든 오류를
CustomException(500)으로 감쌌다. Redis 장애가 전역 Redis 오류 처리기(503, app/main.py)에
닿지 못하고 500(SHOP_INTERNAL_ERROR · USER_INTERNAL_ERROR)이 됐다.

요구 동작: RedisError 는 넓은 except 앞에서 그대로 다시 던져 503(SERVICE_UNAVAILABLE)이
된다. Redis 가 정상이면 응답은 그대로다.

DB · Redis 없이 실제 앱(app.main)의 예외 처리기를 그대로 탄다. Redis 클라이언트는 장애
(모든 명령이 연결 오류) 또는 dict 기반 가짜로 바꾼다.
"""

import importlib
import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import NoReturn

import pytest
import redis
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app import exceptions as exceptions_module
from app.core.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.enum.role import UserRole
from app.models.shop import Shop
from app.models.shop_user import ShopUser
from app.models.user import User
from app.utils.redis import auth as refresh_cache
from app.utils.redis import shop as selected_shop_cache
from app.utils.redis import user as user_cache
from tests.fake_sql import FakeSQLSession

USER_ID, SHOP_ID = 7, 10
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
_DUMMY_HASH = "not-a-real-hash"
UNAVAILABLE = {
    "detail": {
        "code": "SERVICE_UNAVAILABLE",
        "detail": "일시적으로 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    },
}
UPDATE_BODY = {"name": "새 이름입니다", "email": "new-owner@test.com"}


class DownRedis:
    """모든 명령이 연결 오류인 Redis(장애)."""

    def __getattr__(self, _command: str) -> Callable[..., NoReturn]:
        def fail(*_args: object, **_kwargs: object) -> NoReturn:
            msg = "Error 111 connecting to redis:6379. Connection refused."
            raise redis.ConnectionError(msg)

        return fail


class MemoryRedis:
    """선택 샵 · 리프레시 토큰 · 유저 캐시가 쓰는 명령의 dict 구현(정상 Redis)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: object, ex: int | None = None) -> None:  # noqa: ARG002
        self.store[key] = str(value)

    def setex(self, key: str, _ttl: int, value: str) -> None:
        self.store[key] = value

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)


def _world() -> FakeSQLSession:
    """유저 한 명과 그가 대표원장인 샵."""
    return FakeSQLSession(
        User(
            id=USER_ID,
            email="owner@test.com",
            name="원장님",
            password=_DUMMY_HASH,
            role=UserRole.MASTER,
            created_at=NOW,
            updated_at=NOW,
        ),
        Shop(
            id=SHOP_ID,
            user_id=USER_ID,
            name="강남점",
            address="서울시 강남구",
            created_at=NOW,
            updated_at=NOW,
        ),
        ShopUser(id=1, shop_id=SHOP_ID, user_id=USER_ID, is_primary_owner=1),
    )


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
def db() -> FakeSQLSession:
    return _world()


@pytest.fixture
def client(
    sentry_events: list[BaseException],  # noqa: ARG001  # Sentry 기록 스텁 적용
    db: FakeSQLSession,
) -> Iterator[TestClient]:
    """실제 앱(app.main). 인증은 이 유저, DB 는 가짜 세션이다."""
    main: FastAPI = importlib.import_module("app.main").app
    saved = dict(main.dependency_overrides)
    main.dependency_overrides[get_db] = lambda: db
    main.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=USER_ID)
    yield TestClient(main, raise_server_exceptions=False)
    main.dependency_overrides.clear()
    main.dependency_overrides.update(saved)


def _server_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


SELECTED_SHOP_REQUESTS = [
    pytest.param("POST", {"json": {"shop_id": SHOP_ID}}, id="select"),
    pytest.param("GET", {}, id="get-selected"),
    pytest.param("DELETE", {}, id="clear-selected"),
]


# ---------------------------------------------------------------------------
# Redis 장애: 넓은 except 를 지나 503
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "kwargs"), SELECTED_SHOP_REQUESTS)
def test_selected_shop_endpoints_are_503_while_redis_is_down(  # noqa: PLR0913  # 픽스처
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentry_events: list[BaseException],
    method: str,
    kwargs: dict,
) -> None:
    """선택 샵 설정 · 조회 · 해제는 Redis 장애에서 500 이 아니라 503 이다."""
    monkeypatch.setattr(selected_shop_cache, "redis_client", DownRedis())

    response = client.request(method, "/shops/selected", **kwargs)

    assert response.status_code == 503, response.text
    assert response.json() == UNAVAILABLE
    assert [type(e) for e in sentry_events] == [redis.ConnectionError]
    assert _server_logs(caplog) == []


def test_user_update_is_503_when_session_cleanup_hits_redis_outage(
    client: TestClient,
    db: FakeSQLSession,
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: list[BaseException],
) -> None:
    """정보 수정 뒤 리프레시 토큰 · 유저 캐시 정리가 Redis 장애면 503 이다.

    수정 자체는 이미 커밋됐다(세션 정리 보장은 이번 범위 밖 — 후속).
    """
    monkeypatch.setattr(refresh_cache, "redis_client", DownRedis())
    monkeypatch.setattr(user_cache, "redis_client", DownRedis())

    response = client.put("/users/me", json=UPDATE_BODY)

    assert response.status_code == 503, response.text
    assert response.json() == UNAVAILABLE
    assert [type(e) for e in sentry_events] == [redis.ConnectionError]
    assert db.commit_count == 1


# ---------------------------------------------------------------------------
# 대조군: Redis 가 정상이면 그대로다
# ---------------------------------------------------------------------------


def test_selected_shop_round_trip_is_unchanged_with_redis_up(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """선택(204) → 조회(200) → 해제(204) → 다시 조회(404 미선택)."""
    monkeypatch.setattr(selected_shop_cache, "redis_client", MemoryRedis())

    selected = client.post("/shops/selected", json={"shop_id": SHOP_ID})
    current = client.get("/shops/selected")
    cleared = client.delete("/shops/selected")
    after = client.get("/shops/selected")

    assert (selected.status_code, current.status_code) == (204, 200)
    assert current.json()["id"] == SHOP_ID
    assert cleared.status_code == 204
    assert after.status_code == 404
    assert after.json()["detail"]["code"] == "SHOP_NOT_SELECTED"


def test_user_update_is_unchanged_with_redis_up(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정보 수정은 200 이고 커밋 뒤 리프레시 토큰 · 유저 캐시를 지운다."""
    refresh_store, user_store = MemoryRedis(), MemoryRedis()
    refresh_store.setex(f"auth:refresh:{USER_ID}", 60, "refresh-token")
    user_store.setex(f"user:{USER_ID}", 60, "{}")
    monkeypatch.setattr(refresh_cache, "redis_client", refresh_store)
    monkeypatch.setattr(user_cache, "redis_client", user_store)

    response = client.put("/users/me", json=UPDATE_BODY)

    assert response.status_code == 200, response.text
    assert (response.json()["name"], response.json()["email"]) == (
        UPDATE_BODY["name"],
        UPDATE_BODY["email"],
    )
    assert (refresh_store.store, user_store.store) == ({}, {})
