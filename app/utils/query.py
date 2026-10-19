from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

from sqlalchemy import Select
from sqlalchemy.orm import Query
from sqlalchemy.sql.elements import ColumnElement

from app.utils.datetime import KST


class UnsupportedStatementTypeError(TypeError):
    """Unsupported SQLAlchemy statement type for date range filter."""


def apply_date_range_filter[S: (Select, Query)](
    stmt: S,
    field: ColumnElement,
    start_date: date | None,
    end_date: date | None,
    tz: timezone = KST,
) -> S:
    """KST 기준 날짜 범위를 UTC로 변환해 '시작 포함(>=), 끝 배타(<)'로 적용.

    - 일 단위: [YYYY-MM-DD 00:00 KST, YYYY-MM-DD+1 00:00 KST)
    - 월 단위: [YYYY-MM-01 00:00 KST, 다음달 01 00:00 KST)
    """
    if start_date is not None:
        start_dt = datetime.combine(start_date, time.min, tzinfo=tz).astimezone(UTC)
        stmt = _add_condition(stmt, field >= start_dt)

    if end_date is not None:
        next_day = end_date + timedelta(days=1)
        end_exclusive = datetime.combine(next_day, time.min, tzinfo=tz).astimezone(UTC)
        stmt = _add_condition(stmt, field < end_exclusive)

    return stmt


def _add_condition[S: (Select, Query)](stmt: S, cond: ColumnElement) -> S:
    """Select / Query 양쪽 모두 지원하여 동일 타입으로 반환."""
    if hasattr(stmt, "where"):  # Select (SQLAlchemy 2.x)
        return stmt.where(cond)  # type: ignore[return-value]
    if hasattr(stmt, "filter"):  # Query (1.4+)
        return stmt.filter(cond)  # type: ignore[return-value]
    # Ruff TRY003: 긴 메시지 대신 커스텀 예외 사용
    raise UnsupportedStatementTypeError
