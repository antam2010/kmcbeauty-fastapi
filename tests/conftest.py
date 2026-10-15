"""pytest 전역 설정.

app.core.config 는 import 시점에 필수 환경변수를 읽고 int() 캐스팅을 수행하므로,
어떤 app 모듈을 import 하기 전에 테스트용 환경변수를 먼저 설정한다.
"""

import os

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
