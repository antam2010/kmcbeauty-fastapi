"""SPEC-INFRA-002 T-101: 설정 계층 특성화 테스트.

리팩터링(T-102/T-103) 전 현행 config 로딩 동작과 import 시점 부작용을 스냅샷한다.
항목 1~7 은 리팩터링 전/후 모두 GREEN 이어야 하며, 항목 8(REDIS_URL 이중 하드코딩)은
T-103(소비처 통일) 완료 후 RED 로 전환되는 것이 정상이다(계약 §1-A).

conftest.py 가 app.* import 이전에 테스트용 환경변수를 주입하므로, config 모듈은
그 값을 그대로 읽어야 한다(env-var 우선).
"""

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REDIS_DEFAULT = "redis://redis:6379/0"


def test_config_module_values_match_injected_env() -> None:
    """항목 1: config 모듈 노출값이 conftest 주입 env 값과 일치한다."""
    from app.core import config

    assert os.environ["SECRET_KEY"] == config.settings.SECRET_KEY
    assert os.environ["ALGORITHM"] == config.settings.ALGORITHM
    assert os.environ["APP_ENV"] == config.settings.APP_ENV
    assert os.environ["SENTRY_DSN"] == config.settings.SENTRY_DSN
    assert os.environ["FERNET_KEY"] == config.settings.FERNET_KEY


def test_token_expire_seconds_are_int() -> None:
    """항목 2: 토큰 만료값이 int 로 반환된다(int() 캐스팅 현행 동작 스냅샷)."""
    from app.core import config

    assert isinstance(config.settings.ACCESS_TOKEN_EXPIRE_SECONDS, int)
    assert isinstance(config.settings.REFRESH_TOKEN_EXPIRE_SECONDS, int)
    assert (
        int(
            os.environ["ACCESS_TOKEN_EXPIRE_SECONDS"],
        )
        == config.settings.ACCESS_TOKEN_EXPIRE_SECONDS
    )
    assert (
        int(
            os.environ["REFRESH_TOKEN_EXPIRE_SECONDS"],
        )
        == config.settings.REFRESH_TOKEN_EXPIRE_SECONDS
    )


def test_app_main_imports_without_exception_when_sentry_dsn_empty() -> None:
    """항목 3: SENTRY_DSN='' 일 때 app.main import 경로가 예외 없이 통과한다.

    main.py 는 init_sentry 를 조건부 가드 없이 무조건 호출한다. sentry_sdk.init(dsn='')
    는 no-op 이므로, init_sentry 를 모킹하지 않고 실제 호출을 통과시킨다.
    ("SENTRY_DSN 이 비면 init_sentry 를 호출하지 않는다" 는 현행 코드와 다르므로 틀린
    어설션이다 — 계약 §7 위반 위험 4).
    """
    assert os.environ["SENTRY_DSN"] == ""

    import app.main

    assert app.main.app is not None


def test_security_fernet_initialized_at_import() -> None:
    """항목 4: security import 시 Fernet(FERNET_KEY.encode()) 가 성공한다."""
    from app.core import security

    assert isinstance(security.fernet, Fernet)


def test_database_engine_created_at_import() -> None:
    """항목 5: database import 시 engine 이 None 이 아니다(import-time 생성)."""
    from app import database

    assert database.engine is not None


def test_redis_client_created_at_import() -> None:
    """항목 6: redis_client import 시 redis_client 가 None 이 아니다."""
    from app.core import redis_client

    assert redis_client.redis_client is not None


def test_celery_app_importable_from_repo_root() -> None:
    """항목 7: repo-root celery_app 모듈 import 시 celery_app 이 None 이 아니다."""
    import celery_app

    assert celery_app.celery_app is not None


@pytest.mark.xfail(
    strict=False,
    reason=(
        "[계약 §1-A] T-101 에서는 GREEN(이중 하드코딩 스냅샷), T-103 완료 후 "
        "settings.REDIS_URL 단일화로 RED 전환되는 것이 정상이다. 예정된 특성화 "
        "소멸을 xfail 로 문서화하여 스위트 GREEN 을 유지한다."
    ),
)
def test_redis_url_default_hardcoded_in_both_modules() -> None:
    """항목 8: redis_client.py 와 celery_app.py 에 REDIS_URL 기본값이 독립 하드코딩됨.

    [계약 §1-A] 이 어설션은 리팩터링 전 현행 동작(이중 정의)의 스냅샷이다. T-103 에서
    settings.REDIS_URL 단일 참조로 통일되면 이 어설션은 FAIL 로 전환되는 것이 정상이며,
    xfail 마커로 예정된 RED 전환을 표기한다.
    """
    redis_src = (_REPO_ROOT / "app" / "core" / "redis_client.py").read_text(
        encoding="utf-8",
    )
    celery_src = (_REPO_ROOT / "celery_app.py").read_text(encoding="utf-8")

    needle = f'os.getenv("REDIS_URL", "{_REDIS_DEFAULT}")'
    assert needle in redis_src
    assert needle in celery_src
