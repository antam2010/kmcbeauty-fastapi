"""MySQL → PostgreSQL(psycopg v3) 전환 호환성 — DB 세션 타임존 UTC 고정 테스트.

naive DateTime 컬럼(reserved_at, finished_at, expired_at, deleted_at)은 UTC wall-clock
저장 규약이다(worker/tasks/treatment_task.py 의 @MX:WARN 참조). psycopg 는 aware
datetime 을 timestamptz 로 보내고 SQLAlchemy 는 ``::TIMESTAMP WITHOUT TIME ZONE``
캐스트를 붙이므로, PostgreSQL 이 *세션 타임존* 기준으로 wall-clock 을 계산한다.
로컬 서버 기본 타임존(Asia/Seoul)에서는 01:00Z 가 10:00 으로 바뀌는 9시간 어긋남이
실DB 에서 확인되었다(MySQL+PyMySQL 은 tzinfo 를 버리고 UTC wall-clock 을 저장했다).

대상: app/database.py::engine — 모든 커넥션의 세션 타임존을 UTC 로 고정해야 한다.
DB 없이 do_connect 이벤트로 DBAPI connect() 인자를 가로채 확인한다.
"""

import pytest
from sqlalchemy import event

from app.database import engine


class _ConnectInterceptedError(Exception):
    """실제 접속 직전에 중단시키기 위한 센티널 예외."""


def test_engine_pins_postgresql_session_timezone_to_utc() -> None:
    """앱 엔진의 DBAPI 접속 인자에 세션 타임존 UTC 고정 옵션이 포함되어야 한다."""
    captured: list[str | None] = []

    def _capture(
        _dialect: object,
        _conn_rec: object,
        _cargs: tuple,
        cparams: dict,
    ) -> None:
        # 비밀번호 등 다른 접속 인자는 보관하지 않는다(실패 메시지 노출 방지).
        captured.append(cparams.get("options"))
        raise _ConnectInterceptedError

    event.listen(engine, "do_connect", _capture)
    try:
        with pytest.raises(_ConnectInterceptedError):
            engine.connect()
    finally:
        event.remove(engine, "do_connect", _capture)

    (options,) = captured
    assert options is not None
    assert "-c timezone=UTC" in options
