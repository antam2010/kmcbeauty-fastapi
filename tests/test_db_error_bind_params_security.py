"""DB 오류 로그의 바인드 값(사용자 입력) 노출 차단 테스트 (보안).

대상: app/database.py::engine (create_engine(hide_parameters=True))

결함: CustomException 은 원인 예외를 str(exception) 으로 경고 로그에 남기고, 5xx 면
그 예외를 Sentry 로 보낸다(app/exceptions.py). SQLAlchemy 의 DB 오류 문자열에는 SQL
문과 함께 바인드 파라미터(검색어 · 이름 · 전화번호 같은 사용자 입력, 개인정보일 수
있음)가 그대로 들어 있었다.

요구 동작: 엔진이 hide_parameters=True 라 오류 문자열의 파라미터 자리에
"[SQL parameters hidden due to hide_parameters=True]" 가 들어간다. SQL 문은 그대로
남는다(어디서 실패했는지는 보인다).

실 DB 가 없으므로, 엔진이 실행 오류를 감쌀 때와 같은 호출로 예외를 만든다
(sqlalchemy/engine/base.py::Connection._handle_dbapi_exception 의
DBAPIError.instance(..., hide_parameters=engine.hide_parameters, dialect=...)).
"""

import logging

import psycopg
import pytest
from sqlalchemy.exc import DataError, DBAPIError, StatementError

from app import exceptions as exceptions_module
from app.database import engine
from app.exceptions import CustomException

SQL = "SELECT phonebook.id FROM phonebook WHERE phonebook.name ILIKE %(name_1)s"
USER_TEXT = "홍길동 010-1234-5678"  # 사용자 입력(개인정보일 수 있음)
HIDDEN = "[SQL parameters hidden due to hide_parameters=True]"

# 드라이버가 거절한 값(DBAPI 오류 → DataError)과, 바인드 처리 중 난 비 DBAPI 오류
# (→ StatementError). 엔진은 둘 다 SQL · 파라미터를 붙여 감싼다.
DB_ERRORS = [
    pytest.param(
        psycopg.DataError("value too long for type character varying(50)"),
        DataError,
        id="data-error",
    ),
    pytest.param(
        TypeError("can't adapt type 'object'"),
        StatementError,
        id="statement-error",
    ),
]


def _wrapped(orig: Exception) -> StatementError:
    """엔진이 실행 오류를 감싸는 것과 같은 호출로 만든 SQLAlchemy 예외."""
    return DBAPIError.instance(
        SQL,
        {"name_1": f"%{USER_TEXT}%"},
        orig,
        engine.dialect.loaded_dbapi.Error,
        hide_parameters=engine.hide_parameters,
        dialect=engine.dialect,
    )


def test_engine_hides_bind_parameters() -> None:
    """앱 엔진은 DB 오류 · 로그에 바인드 파라미터를 싣지 않는다."""
    assert engine.hide_parameters is True


@pytest.mark.parametrize(("orig", "wrapped_type"), DB_ERRORS)
def test_db_error_string_keeps_sql_but_hides_bind_values(
    orig: Exception,
    wrapped_type: type[StatementError],
) -> None:
    """DB 오류 문자열은 SQL 문은 남기고 바인드 값 자리는 가린다."""
    error = _wrapped(orig)

    assert type(error) is wrapped_type
    text = str(error)
    assert USER_TEXT not in text
    assert HIDDEN in text
    assert SQL in text


@pytest.mark.parametrize(("orig", "_wrapped_type"), DB_ERRORS)
def test_custom_exception_log_and_sentry_hide_bind_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    orig: Exception,
    _wrapped_type: type[StatementError],
) -> None:
    """서비스가 감싼 DB 오류의 경고 로그와 Sentry 이벤트에도 바인드 값이 없다."""
    sentry_events: list[BaseException] = []
    monkeypatch.setattr(exceptions_module, "capture_exception", sentry_events.append)
    caplog.set_level(logging.WARNING)

    CustomException(
        status_code=500,
        domain="PHONEBOOK",
        detail="DB Error",
        exception=_wrapped(orig),
    )

    assert caplog.records
    assert USER_TEXT not in caplog.text
    assert HIDDEN in caplog.text
    assert all(USER_TEXT not in str(event) for event in sentry_events)
