"""애플리케이션 중앙 설정 모듈 (SPEC-INFRA-002 M1).

pydantic-settings ``BaseSettings`` 로 모든 설정을 단일 ``Settings`` 객체로 로드한다.

값 우선순위(높음 → 낮음): 초기화 인자 > 환경변수 > ``.env`` 파일 > ``secrets_dir``
(``/run/secrets``) 파일 > 필드 기본값.
- 환경변수가 secrets 파일보다 우선하므로 로컬 개발과 테스트 스위트(conftest 의
  ``os.environ`` 주입)가 그대로 동작한다(REQ-INFRA-101).
- 운영 컨테이너(docker compose secrets)에서는 시크릿이 ``/run/secrets/<필드명>``
  파일로 마운트되며, 필드명(case-sensitive)이 곧 컨테이너 내 시크릿 파일명과 일치한다.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Docker(compose secrets)가 시크릿 파일을 마운트하는 표준 경로.
# 로컬/CI/개발 환경에는 이 디렉터리가 없다. 존재할 때만 secrets 소스로 등록해
# pydantic-settings 의 "directory does not exist" 경고 출력을 억제한다(EC-2).
_SECRETS_DIR = "/run/secrets"


class Settings(BaseSettings):
    """중앙 설정 스키마.

    필드명은 case-sensitive 이며 compose secret 의 ``target``(컨테이너 파일명)과
    정확히 일치한다(예: ``SECRET_KEY`` → ``/run/secrets/SECRET_KEY``).
    시크릿 필드는 SPEC 결정에 따라 plain ``str`` 로 유지한다(SecretStr 미채택).
    """

    # --- 시크릿(컨테이너에서 /run/secrets/<필드명> 로 주입) ---
    SECRET_KEY: str
    FERNET_KEY: str
    DATABASE_URL: str

    # --- 인증/토큰 설정 ---
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_SECONDS: int
    REFRESH_TOKEN_EXPIRE_SECONDS: int

    # --- 런타임 환경 ---
    APP_ENV: str = "local"
    # 파일 로그(logs/*.log) 기록 여부. 컨테이너에서는 false 로 두고 stdout 만 사용한다
    # (docker logs 로 수집; FileHandler 는 바인드 마운트 권한·리로더 루프 부작용 유발).
    LOG_TO_FILE: bool = True
    # SENTRY_DSN 부재 시 빈 문자열 degrade → Sentry 비활성 기동(REQ-INFRA-104).
    SENTRY_DSN: str = ""

    # --- 인프라 설정(운영에서는 Redis URL 도 시크릿) ---
    # REDIS_URL 은 이 객체에서 단일 정의한다
    # (REQ-INFRA-103: redis_client/celery_app 이중 정의 통일).
    # 기본값은 로컬 dev compose 의 서비스 DNS 다. 운영의 공유 Redis 는
    # 비밀번호가 필수라 URL 에 자격 증명이 들어간다
    # (redis://:<password>@redis:6379/5) → 서버의 시크릿 파일 secrets/redis_url 을
    # compose secret 으로 /run/secrets/REDIS_URL 에 주입하고 .env.prod 에는 두지 않는다.
    REDIS_URL: str = "redis://redis:6379/0"
    # rate limit(slowapi/limits) 카운터 저장소. 기본 memory:// 는 프로세스 로컬이라
    # 로컬 개발·테스트 전용이다(프로세스마다 한도가 따로 적용됨). 운영은 공유 Redis
    # DB 6 을 쓴다(redis://:<password>@redis:6379/6 — REDIS_URL 의 DB 5 와 분리).
    # 역시 비밀번호가 들어가 secrets/rate_limit_storage_uri 파일로 주입한다.
    RATE_LIMIT_STORAGE_URI: str = "memory://"
    # Firebase 서비스 계정 키 경로. 미설정 시 빈 문자열 → 초기화 스킵(현행 보존).
    FIREBASE_SERVICE_ACCOUNT_KEY_PATH: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir=_SECRETS_DIR if Path(_SECRETS_DIR).is_dir() else None,
        case_sensitive=True,
        extra="ignore",
    )


# @MX:ANCHOR: [AUTO] 중앙 설정 싱글톤 — 전 모듈이 임포트
# @MX:REASON: config·database·redis_client·celery_app·security·sentry·firebase·
#             alembic 등 전 계층이 import 시점에 이 객체를 소비하는 최고 fan-in
#             지점이다. 값 소스(env > .env > secrets_dir) 변경 시 이 한 곳만 바뀌며,
#             import-time 부작용(engine/redis/celery 생성)이 이 값에 의존한다.
settings = Settings()
