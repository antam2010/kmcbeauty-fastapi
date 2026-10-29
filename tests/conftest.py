"""pytest 전역 설정.

app.core.config 는 import 시점에 필수 환경변수를 읽고 int() 캐스팅을 수행하므로,
어떤 app 모듈을 import 하기 전에 테스트용 환경변수를 먼저 설정한다.
"""

import os

import pytest

# app.* import 이전에 필수 환경변수를 주입한다.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_SECONDS", "900")
# 리프레시 만료(초). refresh 회전 임계치 계산에 사용된다.
os.environ.setdefault("REFRESH_TOKEN_EXPIRE_SECONDS", "1209600")
os.environ.setdefault("APP_ENV", "local")
# Settings 필수 필드. 테스트는 DB 를 전부 모킹하므로 실제 연결되지 않는다
# (create_engine 은 pool_pre_ping 이어도 import 시점에 접속하지 않음).
# 단, create_engine 은 import 시점에 URL 의 DBAPI(psycopg)를 import 하므로
# 해당 드라이버는 설치되어 있어야 한다.
# CI 처럼 .env 파일이 없는 환경에서도 수집이 가능해야 한다.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://test:test@localhost:5432/test",
)
os.environ.setdefault("SENTRY_DSN", "")
# Fernet 키(url-safe base64 인코딩된 32바이트, 정확히 44자). security.py 가
# import 시점에 Fernet(FERNET_KEY) 를 생성하므로 유효한 키여야 한다.
os.environ.setdefault("FERNET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
# rate limit 카운터는 프로세스 메모리에 둔다. .env 가 Redis 를 가리켜도 테스트 요청이
# 로컬 Redis 카운터를 바꾸지 않고, 아래 픽스처로 테스트마다 비울 수 있다.
os.environ.setdefault("RATE_LIMIT_STORAGE_URI", "memory://")


class _InMemoryRedis:
    """대시보드 캐시가 쓰는 Redis 명령(get/set/delete/incr)의 메모리 구현."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:  # noqa: ARG002
        self.store[key] = value

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)

    def incr(self, key: str) -> int:
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])


@pytest.fixture(autouse=True)
def _isolate_dashboard_cache_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """예약 쓰기 경로의 대시보드 캐시 무효화가 실제 Redis 에 닿지 않게 한다.

    예약 생성/수정/취소와 자동완료는 커밋 후 캐시 버전을 올린다(INCR). 테스트의
    REDIS_URL 이 로컬 Redis 를 가리킬 수 있으므로, 단위 테스트가 실제 키를 바꾸지
    않도록 대시보드 캐시 모듈의 클라이언트를 메모리 구현으로 바꾼다.
    """
    from app.utils.redis import dashboard

    monkeypatch.setattr(dashboard, "redis_client", _InMemoryRedis())


@pytest.fixture(autouse=True)
def _reset_rate_limit_counters() -> None:
    """테스트마다 rate limit 카운터를 비운다(테스트 순서 · 요청 수와 무관하게).

    limiter 는 모듈 전역(app.core.rate_limit)이라 TestClient 요청 수가 테스트 사이에
    누적된다. limiter 는 켠 채로 두고(전용 rate limit 테스트가 429 를 검증) 카운터만
    리셋한다. 인메모리 저장소만 비운다(외부 Redis 를 가리키면 건드리지 않는다).
    """
    from limits.storage import MemoryStorage

    from app.core.rate_limit import limiter

    storage = limiter.limiter.storage
    if isinstance(storage, MemoryStorage):
        storage.reset()
