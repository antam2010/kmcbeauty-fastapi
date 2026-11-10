"""SPEC-INFRA-002: rate limit 저장소(RATE_LIMIT_STORAGE_URI) 설정 테스트.

- 기본값은 memory:// — 로컬 개발·테스트 동작 불변.
- 값을 지정하면 slowapi Limiter 의 저장소로 그대로 쓰인다(운영: 공유 Redis DB 6,
  비밀번호 포함 URL 을 compose 시크릿 파일로 주입).
"""

import importlib
from pathlib import Path

import pytest
from limits.storage import MemoryStorage, RedisStorage

from app.core import config, rate_limit


def test_default_storage_is_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """미설정 시 memory:// 이며, 테스트 스위트의 limiter 도 인메모리 저장소를 쓴다."""
    monkeypatch.delenv("RATE_LIMIT_STORAGE_URI", raising=False)

    settings = config.Settings(_env_file=None, _secrets_dir=str(tmp_path))

    assert settings.RATE_LIMIT_STORAGE_URI == "memory://"
    assert isinstance(rate_limit.limiter.limiter.storage, MemoryStorage)


def test_configured_storage_uri_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env(RATE_LIMIT_STORAGE_URI) → Settings → Limiter 저장소로 그대로 전달된다.

    limits 의 Redis 백엔드는 생성 시 접속하지 않으므로 실제 Redis 없이 검증되며,
    설치된 redis 패키지를 limits 가 수용하는지(의존성 검사)도 함께 확인된다.
    """
    # 운영 형식: 공유 Redis(이름 redis)의 DB 6, 비밀번호 필수.
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "redis://:test-pw@redis:6379/6")
    # rate_limit 은 import 시점에 settings 를 읽으므로, env 로 새로 만든 Settings 를
    # 주입하고 모듈을 재실행한다. 종료 시 monkeypatch 가 settings·limiter 를
    # 원복하므로 app.main/app.api.auth 가 참조 중인 원본 limiter 는 영향받지 않는다.
    monkeypatch.setattr(config, "settings", config.Settings(_env_file=None))
    monkeypatch.setattr(rate_limit, "limiter", rate_limit.limiter)
    reloaded = importlib.reload(rate_limit)

    storage = reloaded.limiter.limiter.storage
    assert isinstance(storage, RedisStorage)
    conn_kwargs = storage.get_connection().connection_pool.connection_kwargs
    assert (conn_kwargs["host"], conn_kwargs["port"], conn_kwargs["db"]) == (
        "redis",
        6379,
        6,
    )
    assert conn_kwargs["password"] == "test-pw"  # noqa: S105  # 테스트 픽스처 값


def test_redis_storage_gives_up_within_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis 저장소도 연결 · 응답을 2초까지만 기다린다(유저 캐시 클라이언트와 같다).

    없으면 응답 없는 Redis 에 한도 검사(hit)가 끝없이 막힌다. 한도가 걸린 엔드포인트는
    모두 sync 라 막힌 검사가 스레드풀 스레드를 붙잡는다. 넘으면 RedisError 로 503 이다.
    """
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "redis://:test-pw@redis:6379/6")
    # test_configured_storage_uri_is_used 와 같이 settings 를 주입하고 모듈을 재실행한다
    # (종료 시 monkeypatch 가 settings · limiter 를 원복한다).
    monkeypatch.setattr(config, "settings", config.Settings(_env_file=None))
    monkeypatch.setattr(rate_limit, "limiter", rate_limit.limiter)
    reloaded = importlib.reload(rate_limit)

    storage = reloaded.limiter.limiter.storage
    conn_kwargs = storage.get_connection().connection_pool.connection_kwargs
    assert conn_kwargs.get("socket_connect_timeout") == 2
    assert conn_kwargs.get("socket_timeout") == 2
