"""SPEC-INFRA-002 T-102: Settings(BaseSettings) 단위 테스트.

계약 §1-B 의 6개 필수 케이스를 검증한다.
- AC-2 env-var 우선 / file fallback
- AC-4 Sentry degrade
- /run/secrets 부재 환경 호환
- FERNET_KEY plain str Fernet 소비
- REDIS_URL 단일 진실원천
- 필드명 == secrets 파일명 매칭
"""

import warnings
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings

# 유효한 Fernet 키(url-safe base64, 32바이트 → 44자).
_VALID_FERNET = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

# 필수 필드를 채우기 위한 공통 env 세트(테스트별로 개별 항목만 오버라이드).
_BASE_ENV = {
    "SECRET_KEY": "base-secret",
    "FERNET_KEY": _VALID_FERNET,
    "DATABASE_URL": "sqlite:///./test.db",
    "ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_SECONDS": "900",
    "REFRESH_TOKEN_EXPIRE_SECONDS": "1209600",
}


def _write_secret(directory: Path, name: str, value: str) -> None:
    """secrets_dir 규약(파일명 == 필드명, 내용 == 값)으로 시크릿 파일을 생성한다."""
    (directory / name).write_text(value, encoding="utf-8")


def test_env_var_beats_secrets_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-2: env var 와 secrets_dir 파일이 동시 존재하면 env var 가 우선한다."""
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SECRET_KEY", "env-val")
    _write_secret(tmp_path, "SECRET_KEY", "file-val")

    settings = Settings(_secrets_dir=str(tmp_path))

    assert settings.SECRET_KEY == "env-val"  # noqa: S105  # 테스트 픽스처 값


def test_secrets_file_fallback_when_env_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-2 역방향: env var 부재 시 secrets_dir 파일값으로 fallback 한다."""
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    _write_secret(tmp_path, "SECRET_KEY", "file-val")

    # .env 비활성화: repo 의 .env 에 SECRET_KEY 가 있어 secrets_dir 보다 우선하므로
    # fallback 검증을 위해 파일 소스는 secrets_dir 만 남긴다.
    settings = Settings(_env_file=None, _secrets_dir=str(tmp_path))

    assert settings.SECRET_KEY == "file-val"  # noqa: S105  # 테스트 픽스처 값


def test_sentry_dsn_degrades_to_empty_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-4: SENTRY_DSN 이 env/secrets 어디에도 없으면 빈 문자열로 degrade."""
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SENTRY_DSN", raising=False)

    settings = Settings(_env_file=None, _secrets_dir=str(tmp_path))

    assert settings.SENTRY_DSN == ""


def test_missing_run_secrets_dir_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EC-2: /run/secrets 부재(로컬/CI)에서 Settings 초기화가 예외 없이 완료된다."""
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)

    with warnings.catch_warnings():
        # 부재 디렉터리 경고는 예외가 아니며 정상 동작이다(테스트 출력만 정리).
        warnings.simplefilter("ignore")
        settings = Settings(_secrets_dir="/run/secrets")

    assert settings.SECRET_KEY == "base-secret"  # noqa: S105  # 테스트 픽스처 값


def test_fernet_key_is_plain_str_and_consumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: FERNET_KEY 는 plain str 로 유지되어 Fernet 이 바로 소비 가능하다."""
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)

    settings = Settings(_secrets_dir=str(tmp_path))

    assert isinstance(settings.FERNET_KEY, str)
    assert isinstance(Fernet(settings.FERNET_KEY.encode()), Fernet)


def test_redis_url_single_source_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-INFRA-103: REDIS_URL 이 접근 가능하며 기본값은 로컬 compose 서비스 DNS 다.

    운영은 기본값이 아니라 compose 시크릿 파일(secrets/redis_url,
    공유 Redis DB 5)을 쓴다.
    """
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("REDIS_URL", raising=False)

    settings = Settings(_env_file=None, _secrets_dir=str(tmp_path))

    assert settings.REDIS_URL == "redis://redis:6379/0"


def test_field_name_equals_secret_file_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EC-5: 시크릿 파일명(필드명, 대소문자 정확)으로 값이 로딩된다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key, value in _BASE_ENV.items():
        if key == "DATABASE_URL":
            continue
        monkeypatch.setenv(key, value)
    _write_secret(tmp_path, "DATABASE_URL", "sqlite:///./from-secret.db")

    settings = Settings(_env_file=None, _secrets_dir=str(tmp_path))

    assert settings.DATABASE_URL == "sqlite:///./from-secret.db"
