"""DataError(입력 값을 DB 가 거절) 500 → 400 교정 테스트.

대상:
- app/exceptions.py::CustomException — 서비스가 SQLAlchemyError 를 감싼 500
  (CustomException(500, exception=e) 와 exception 없이 `raise ... from e` 둘 다)
- app/exceptions.py::data_error_handler 와 app/main.py 등록 — 서비스가 감싸지 않은
  DataError(GET /phonebooks · /treatment-menus 의 검색어 등)
- 스스로 logging.exception(ERROR)을 남기던 경로:
  phonebook_service.update_phonebook_service,
  treatment_menu_service.create_treatment_menu_service ·
  create_treatment_menu_detail_service

결함(실 PostgreSQL 로 확인): 문자열의 NUL 문자(0x00)는 psycopg 가 DataError 로
거절한다("PostgreSQL text fields cannot contain NUL (0x00) bytes"). 범위 초과 ·
형식 오류(NumericValueOutOfRange · InvalidTextRepresentation)도 같은 DataError 다.
- 감싼 경로(예: GET /treatments?search=%00, POST /phonebooks)는 500 · Sentry 전송이었다.
- 감싸지 않은 경로(GET /phonebooks · /treatment-menus 검색)는 처리되지 않은 예외로
  500 · ERROR 로그 · Sentry 전송이었다.

요구 동작(사용자 결정): DataError 는 400 이고 detail 은 일반 문구다. 원문(SQL ·
파라미터 · 드라이버 메시지)은 어떤 APP_ENV 에서도 응답에 싣지 않는다. 경고(WARNING)로만
기록하고 Sentry 로 보내지 않는다. 다른 SQLAlchemyError · 500 경로는 그대로다.

DB 없이 검증한다. psycopg 의 DataError 를 SQLAlchemy 가 감싼 것과 같은 예외를 CRUD
스텁 · 가짜 세션이 던진다(NUL 문자가 든 값만 거절). HTTP 경로는 실제 앱(app.main)의
미들웨어 · 예외 처리기를 그대로 탄다.
"""

import importlib
import logging
from collections.abc import Iterator
from types import SimpleNamespace

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_pagination import create_page, resolve_params
from sqlalchemy.exc import DataError, IntegrityError, OperationalError, SQLAlchemyError

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app import exceptions as exceptions_module
from app.core.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.shop import get_current_shop
from app.exceptions import CustomException
from app.models.phonebook import Phonebook
from app.models.treatment_menu import TreatmentMenu
from app.services import phonebook_service, treatment_menu_service, treatment_service

SHOP_ID = 10
NUL_MESSAGE = "PostgreSQL text fields cannot contain NUL (0x00) bytes"
SQL = "SELECT count(*) AS count_1 FROM phonebook WHERE phonebook.name ILIKE %(name_1)s"
USER_TEXT = "고객검색어"
# 응답에 나오면 안 되는 원문 조각(드라이버 메시지 · SQL · 바인드 값 · 드라이버 이름).
LEAKS = [NUL_MESSAGE, "SELECT", "ILIKE", USER_TEXT, "psycopg", "sqlalche.me"]
# DataError 400 의 일반 문구(계약).
GENERIC = {
    "detail": "처리할 수 없는 값이 요청에 있습니다.",
    "hint": "입력 값을 확인해 주세요.",
}


def _data_error(value: str = f"%\x00{USER_TEXT}%") -> DataError:
    """SQLAlchemy 가 psycopg 의 바인드 값 거절을 감싼 것과 같은 DataError."""
    return DataError(SQL, {"name_1": value}, psycopg.DataError(NUL_MESSAGE))


def _reject_nul(value: object) -> None:
    """NUL 문자가 든 문자열 바인드 값을 psycopg 처럼 거절한다."""
    if isinstance(value, str) and "\x00" in value:
        raise _data_error(value)


def _server_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    """CustomException 의 Sentry 전송(capture_exception)을 기록한다."""
    events: list[BaseException] = []
    monkeypatch.setattr(exceptions_module, "capture_exception", events.append)
    return events


# ---------------------------------------------------------------------------
# CustomException: 서비스가 감싼 DataError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app_env", ["local", "debug", "production"])
def test_wrapped_data_error_becomes_generic_400(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentry_events: list[BaseException],
    app_env: str,
) -> None:
    """CustomException(500, exception=DataError) 는 일반 문구의 400 이다(debug 포함)."""
    monkeypatch.setattr(settings, "APP_ENV", app_env)
    caplog.set_level(logging.WARNING)

    error = CustomException(
        status_code=500,
        domain="TREATMENT",
        detail="DB Error",
        exception=_data_error(),
    )

    assert error.status_code == 400
    assert error.detail == {"code": "TREATMENT_BAD_REQUEST", **GENERIC}
    assert not [leak for leak in LEAKS if leak in str(error.detail)]
    assert sentry_events == []
    assert _server_logs(caplog) == []
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_data_error_raised_from_without_exception_argument_is_400(
    caplog: pytest.LogCaptureFixture,
    sentry_events: list[BaseException],
) -> None:
    """CustomException(500) 에 원인을 넘기지 않고 `from e` 만 한 서비스도 400 이다."""
    caplog.set_level(logging.WARNING)

    try:
        raise _data_error()
    except SQLAlchemyError:
        error = CustomException(status_code=500, domain="PHONEBOOK")

    assert error.status_code == 400
    assert error.detail == {"code": "PHONEBOOK_BAD_REQUEST", **GENERIC}
    assert sentry_events == []
    assert _server_logs(caplog) == []


@pytest.mark.parametrize(
    "exception",
    [
        OperationalError(SQL, {}, psycopg.OperationalError("connection lost")),
        IntegrityError(SQL, {}, psycopg.IntegrityError("duplicate key")),
        RuntimeError("unexpected"),
    ],
    ids=["operational-error", "integrity-error-as-500", "non-db-error"],
)
def test_other_server_errors_stay_500_and_go_to_sentry(
    sentry_events: list[BaseException],
    exception: Exception,
) -> None:
    """DataError 가 아닌 500 은 그대로다(500 · 원래 문구 · Sentry 전송)."""
    error = CustomException(
        status_code=500,
        domain="TREATMENT",
        detail="DB Error",
        exception=exception,
    )

    assert error.status_code == 500
    assert error.detail["code"] == "TREATMENT_INTERNAL_ERROR"
    assert error.detail["detail"] == "DB Error"
    assert sentry_events == [exception]


def test_conflict_from_integrity_error_is_unchanged(
    sentry_events: list[BaseException],
) -> None:
    """IntegrityError 를 409 로 바꾸는 기존 경로는 그대로다."""
    error = CustomException(
        status_code=409,
        domain="PHONEBOOK",
        exception=IntegrityError(SQL, {}, psycopg.IntegrityError("duplicate key")),
    )

    assert (error.status_code, error.detail["code"]) == (409, "PHONEBOOK_CONFLICT")
    assert sentry_events == []


# ---------------------------------------------------------------------------
# 실제 앱(app.main) HTTP 경로
# ---------------------------------------------------------------------------


class _NulRejectingSession:
    """psycopg 처럼 NUL 문자가 든 문자열 값을 flush 에서 DataError 로 거절하는 세션."""

    def __init__(self, error: Exception | None = None) -> None:
        self.tracked: list[object] = []
        # 지정하면 flush 가 항상 이 예외를 던진다(DataError 가 아닌 오류의 대조군).
        self.error = error
        self.rollbacks = 0

    def add(self, obj: object) -> None:
        self.tracked.append(obj)

    def flush(self) -> None:
        if self.error is not None:
            raise self.error
        for obj in self.tracked:
            for value in vars(obj).values():
                _reject_nul(value)

    def commit(self) -> None:
        self.flush()

    def rollback(self) -> None:
        self.rollbacks += 1

    def refresh(self, obj: object) -> None:
        """DB 재조회 없음."""


@pytest.fixture
def main_client(
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: list[BaseException],
) -> Iterator[tuple[TestClient, FastAPI]]:
    """실제 앱(app.main)에 인증 · 상점 의존성만 바꾼 클라이언트.

    처리되지 않은 예외의 Sentry 전송(error_logger 미들웨어)도 같은 목록에 기록한다.
    """
    monkeypatch.setattr(settings, "LOG_TO_FILE", False)
    main_module = importlib.import_module("app.main")
    monkeypatch.setattr(main_module, "capture_exception", sentry_events.append)
    main_app = main_module.app
    saved = dict(main_app.dependency_overrides)
    main_app.dependency_overrides[get_db] = lambda: None
    main_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    main_app.dependency_overrides[get_current_shop] = lambda: SimpleNamespace(
        id=SHOP_ID,
        user_id=1,
    )
    yield TestClient(main_app, raise_server_exceptions=False), main_app
    main_app.dependency_overrides.clear()
    main_app.dependency_overrides.update(saved)


@pytest.fixture
def nul_rejecting_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """목록 CRUD 를 검색어에 NUL 문자가 있으면 거절하는 스텁으로 바꾼다."""

    def _list(**kwargs: object) -> object:
        filters = kwargs.get("filters")
        _reject_nul(kwargs.get("search", getattr(filters, "search", None)))
        return create_page([], total=0, params=resolve_params())

    # 시술 예약 목록은 서비스가 SQLAlchemyError 를 감싼다(CustomException 경로).
    monkeypatch.setattr(treatment_service, "get_treatment_list", _list)
    # 전화번호부 · 시술 메뉴 목록 서비스는 감싸지 않는다(전역 처리기 경로).
    monkeypatch.setattr(phonebook_service, "get_phonebooks_by_user", _list)
    monkeypatch.setattr(treatment_menu_service, "get_treatment_menus_by_user", _list)


LIST_CASES = [
    ("/treatments", "TREATMENT_BAD_REQUEST"),
    ("/phonebooks", "BAD_REQUEST"),
    ("/treatment-menus", "BAD_REQUEST"),
]


@pytest.mark.usefixtures("nul_rejecting_lists")
@pytest.mark.parametrize("app_env", ["local", "debug"])
@pytest.mark.parametrize(("path", "code"), LIST_CASES, ids=[p for p, _ in LIST_CASES])
def test_nul_in_list_search_is_generic_400(  # noqa: PLR0913  # pytest 픽스처 · 파라미터
    main_client: tuple[TestClient, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentry_events: list[BaseException],
    path: str,
    code: str,
    app_env: str,
) -> None:
    """검색어의 NUL 문자는 감싼 경로 · 감싸지 않은 경로 모두 일반 문구의 400 이다."""
    monkeypatch.setattr(settings, "APP_ENV", app_env)
    caplog.set_level(logging.WARNING)
    client, _ = main_client

    response = client.get(path, params={"search": f"\x00{USER_TEXT}"})

    assert response.status_code == 400, response.text
    assert response.json() == {"detail": {"code": code, **GENERIC}}
    assert not [leak for leak in LEAKS if leak in response.text]
    assert sentry_events == []
    assert _server_logs(caplog) == []


@pytest.mark.usefixtures("nul_rejecting_lists")
@pytest.mark.parametrize("path", [p for p, _ in LIST_CASES])
def test_ordinary_search_still_lists(
    main_client: tuple[TestClient, FastAPI],
    path: str,
) -> None:
    """NUL 문자가 없는 검색은 그대로 200 이다(스텁이 실제로 값을 가린다는 대조군)."""
    client, _ = main_client

    response = client.get(path, params={"search": "김민지"})

    assert response.status_code == 200, response.text


def test_main_app_registers_data_error_handler(
    main_client: tuple[TestClient, FastAPI],
) -> None:
    """감싸지 않은 DataError 를 처리하는 전역 처리기가 실제 앱에 등록되어 있다."""
    _, main_app = main_client
    handler = exceptions_module.data_error_handler

    assert main_app.exception_handlers[DataError] is handler


def _stub_phonebook_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """전화번호 중복 없음 · 수정 대상은 이 샵의 연락처 1건."""
    monkeypatch.setattr(
        phonebook_service,
        "get_phonebook_by_phone_number",
        lambda *_args, **_kwargs: None,
    )

    def _existing(db: _NulRejectingSession, phonebook_id: int, shop_id: int) -> object:
        phonebook = Phonebook(
            id=phonebook_id,
            shop_id=shop_id,
            name="홍길동",
            phone_number="010-1234-5678",
        )
        db.add(phonebook)
        return phonebook

    monkeypatch.setattr(phonebook_service, "get_phonebook_by_id", _existing)


def _stub_menu_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        treatment_menu_service,
        "get_menu_by_id",
        lambda **kwargs: TreatmentMenu(id=kwargs["menu_id"], shop_id=SHOP_ID),
    )


WRITE_CASES = [
    # 서비스가 exception=e 로 감싼다.
    (
        "POST",
        "/phonebooks",
        {"name": f"홍\x00{USER_TEXT}", "phone_number": "010-1234-5678"},
        "PHONEBOOK_BAD_REQUEST",
    ),
    # exception 없이 감싸고 logging.exception(ERROR)을 남기던 경로.
    (
        "PUT",
        "/phonebooks/7",
        {"memo": f"메모\x00{USER_TEXT}"},
        "PHONEBOOK_BAD_REQUEST",
    ),
    # logging.exception(ERROR)을 남기던 경로.
    (
        "POST",
        "/treatment-menus",
        {"name": f"젤\x00{USER_TEXT}"},
        "TREATMENT_MENU_BAD_REQUEST",
    ),
    (
        "POST",
        "/treatment-menus/3/details",
        {"name": f"속\x00{USER_TEXT}", "duration_min": 60, "base_price": 30000},
        "TREATMENT_MENU_BAD_REQUEST",
    ),
]
WRITE_IDS = ["phonebook-create", "phonebook-update", "menu-create", "detail-create"]


@pytest.mark.parametrize(("method", "path", "body", "code"), WRITE_CASES, ids=WRITE_IDS)
def test_nul_in_written_value_is_generic_400(  # noqa: PLR0913  # pytest 픽스처 · 파라미터
    main_client: tuple[TestClient, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sentry_events: list[BaseException],
    method: str,
    path: str,
    body: dict,
    code: str,
) -> None:
    """저장할 값의 NUL 문자도 400 이다(롤백 · 경고 로그, ERROR · Sentry 없음)."""
    client, main_app = main_client
    session = _NulRejectingSession()
    main_app.dependency_overrides[get_db] = lambda: session
    _stub_phonebook_lookups(monkeypatch)
    _stub_menu_lookup(monkeypatch)
    caplog.set_level(logging.WARNING)

    response = client.request(method, path, json=body)

    assert response.status_code == 400, response.text
    assert response.json() == {"detail": {"code": code, **GENERIC}}
    assert not [leak for leak in LEAKS if leak in response.text]
    assert session.rollbacks == 1
    assert sentry_events == []
    assert _server_logs(caplog) == []


@pytest.mark.parametrize(
    ("method", "path", "body", "_code"),
    WRITE_CASES,
    ids=WRITE_IDS,
)
def test_other_db_errors_on_write_stay_500(  # noqa: PLR0913  # pytest 픽스처 · 파라미터
    main_client: tuple[TestClient, FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: list[BaseException],
    method: str,
    path: str,
    body: dict,
    _code: str,
) -> None:
    """같은 쓰기 경로라도 DataError 가 아니면 기존처럼 500 · Sentry 전송이다."""
    client, main_app = main_client
    failure = OperationalError(SQL, {}, psycopg.OperationalError("server closed"))
    session = _NulRejectingSession(error=failure)
    main_app.dependency_overrides[get_db] = lambda: session
    _stub_phonebook_lookups(monkeypatch)
    _stub_menu_lookup(monkeypatch)

    response = client.request(method, path, json=body)

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"].endswith("_INTERNAL_ERROR")
    assert len(sentry_events) == 1


def test_menu_create_still_logs_unexpected_errors_with_traceback(
    main_client: tuple[TestClient, FastAPI],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DataError 가 아닌 예외는 지금처럼 logging.exception(ERROR · 스택)으로 남는다."""
    client, main_app = main_client
    failure = OperationalError(SQL, {}, psycopg.OperationalError("server closed"))
    main_app.dependency_overrides[get_db] = lambda: _NulRejectingSession(error=failure)

    response = client.post("/treatment-menus", json={"name": "젤네일"})

    assert response.status_code == 500
    [record] = _server_logs(caplog)
    assert record.exc_info is not None
    assert record.exc_info[1] is failure
