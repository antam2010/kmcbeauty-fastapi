# DB 세션 관리 함수
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# 엔진 생성 (DATABASE_URL 은 중앙 settings 에서 단일 소싱)
# TODO echo=False
# @MX:NOTE: [AUTO] PostgreSQL 세션 타임존을 UTC 로 고정한다. naive DateTime 컬럼
#            (reserved_at 등)은 UTC wall-clock 저장 규약인데, psycopg 는 aware datetime
#            을 세션 타임존 기준으로 timestamp 변환하므로 서버 기본값(로컬 Windows PG:
#            Asia/Seoul)에 맡기면 9시간 어긋난다. (MySQL+PyMySQL 은 tzinfo 를 버리고
#            UTC wall-clock 을 저장했다)
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    echo=False,
    # DB 오류 문자열(str(e))에 바인드 파라미터(사용자 입력 · 개인정보일 수 있음)를 싣지
    # 않는다. CustomException 이 원인 예외를 str 로 경고 로그에 남기고 5xx 면 Sentry 로
    # 보낸다(app/exceptions.py). SQL 문은 그대로 남아 실패 위치는 보인다.
    hide_parameters=True,
    connect_args={"options": "-c timezone=UTC"},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
