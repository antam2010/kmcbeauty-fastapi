import contextlib
import logging
from pathlib import Path

import sqlparse


def setup_logging(app_env: str, *, log_to_file: bool = True) -> None:
    """로깅 설정 함수.

    Args:
        app_env (str): 실행 환경 (local, debug, production)
        log_to_file (bool): 파일 로그(logs/*.log) 기록 여부.
            컨테이너에서는 False 로 두고 stdout 만 사용한다(docker logs 수집).

    """
    # 1) 기본 로깅 레벨 및 파일명 설정
    if app_env == "debug":
        log_level = logging.DEBUG
        log_file = "logs/debug.log"
        sql_log_level = logging.INFO
    elif app_env == "stage":
        log_level = logging.INFO
        log_file = "logs/stage.log"
        sql_log_level = logging.ERROR
    elif app_env == "production":
        log_level = logging.INFO
        log_file = "logs/prod.log"
        sql_log_level = logging.ERROR
    elif app_env == "local":
        log_level = logging.INFO
        log_file = "logs/local.log"
        sql_log_level = logging.INFO
    else:
        msg = "Invalid app_env. Choose from: local, debug, stage, production."
        raise ValueError(msg)
    # 2) 핸들러 구성 — 콘솔(stdout)은 항상, 파일은 log_to_file 일 때만
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_to_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.insert(0, logging.FileHandler(log_file, encoding="utf-8"))

    # 3) 기본 로깅 설정
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )

    # 4) SQLFormatterHandler 정의 (SQL 문장 예쁘게 출력)
    class SQLFormatterHandler(logging.StreamHandler):
        def emit(self, record: logging.LogRecord) -> None:
            if isinstance(record.msg, str):
                # 포매팅 실패가 로깅 자체를 중단시키면 안 되므로 광범위 억제한다.
                with contextlib.suppress(Exception):
                    record.msg = sqlparse.format(
                        record.msg,
                        reindent=True,
                        keyword_case="upper",
                    )
            super().emit(record)

    # 5) SQLAlchemy 엔진 로그 설정
    sql_logger = logging.getLogger("sqlalchemy.engine")
    # production 환경이면 ERROR 이상만, 아니면 위에서 지정한 대로
    sql_logger.setLevel(sql_log_level)
    sql_logger.handlers.clear()

    sql_handler = SQLFormatterHandler()
    sql_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s"))

    # (선택) 핸들러 레벨을 추가로 걸고 싶다면
    sql_handler.setLevel(sql_log_level)

    sql_logger.addHandler(sql_handler)
