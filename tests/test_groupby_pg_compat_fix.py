"""MySQL → PostgreSQL(psycopg v3) 전환 호환성 — GROUP BY 엄격성 회귀 테스트.

MySQL 은 GROUP BY 에 없는 비집계 컬럼(예: 조인한 users.name)의 SELECT 를 허용했지만,
PostgreSQL 은 해당 테이블의 PK 가 GROUP BY 에 직접 있지 않으면 파싱 시점에 거부한다
("column ... must appear in the GROUP BY clause or be used in an aggregate function").

대상(GROUP BY 를 사용하는 조회 전부):
- app/crud/statistics_crud.py: get_treatment_summary, get_treatment_sales_summary,
  get_customer_insight_bulk, get_staff_summary
- app/crud/phonebook_crud.py: get_group_counts_by_groupname
- app/crud/treatment_crud.py: get_treatments_to_autocomplete

CI 에는 실DB 가 없으므로, 세션이 실행하려는 문장을 DB 없이 가로채 PostgreSQL 방언으로
컴파일한 뒤 SELECT 의 비집계 컬럼이 모두 GROUP BY 에 포함되는지 확인한다.
"""

import re
from collections.abc import Callable
from datetime import date

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query
from sqlalchemy.sql.elements import ClauseElement, Label
from sqlalchemy.sql.functions import Function

from app.crud import phonebook_crud, statistics_crud, treatment_crud

_AGGREGATES = frozenset({"count", "sum", "avg", "min", "max"})
_START = date(2026, 10, 1)
_END = date(2026, 10, 31)


class _EmptyResult:
    def fetchall(self) -> list:
        return []


class _CaptureQuery(Query):
    """레거시 db.query() 체인의 최종 SELECT 를 기록하고 빈 결과를 돌려준다."""

    def all(self) -> list:
        self.recorder.append(self.statement)
        return []


class CaptureSession:
    """execute()/query() 로 실행하려는 문장을 기록하는 DB-free 스파이 세션."""

    def __init__(self) -> None:
        self.statements: list[Select] = []

    def execute(self, stmt: Select) -> _EmptyResult:
        self.statements.append(stmt)
        return _EmptyResult()

    def query(self, *entities: object) -> _CaptureQuery:
        query = _CaptureQuery(entities)
        # Query 는 generative 복사 시 __dict__ 를 복제하므로 체인 끝까지 전달된다.
        query.recorder = self.statements
        return query


def _pg_sql(clause: ClauseElement) -> str:
    return str(clause.compile(dialect=postgresql.dialect()))


def _non_aggregated_columns(stmt: Select) -> list[str]:
    """SELECT 목록에서 집계 함수가 아닌 컬럼의 PostgreSQL SQL 표현을 반환한다."""
    columns = []
    for col in stmt.selected_columns:
        expr = col.element if isinstance(col, Label) else col
        if isinstance(expr, Function) and expr.name in _AGGREGATES:
            continue
        columns.append(_pg_sql(expr))
    return columns


def _group_by_items(sql: str) -> list[str]:
    """컴파일된 SQL 의 GROUP BY 절 항목을 반환한다."""
    assert "GROUP BY" in sql, sql
    tail = sql.split("GROUP BY", 1)[1]
    tail = re.split(r"\b(?:HAVING|ORDER BY|LIMIT|OFFSET)\b", tail, maxsplit=1)[0]
    return [item.strip() for item in tail.split(",")]


_GROUP_BY_QUERIES = [
    pytest.param(
        lambda db: statistics_crud.get_treatment_summary(db, 1, _START, _END),
        id="statistics.get_treatment_summary",
    ),
    pytest.param(
        lambda db: statistics_crud.get_treatment_sales_summary(db, 1, _START, _END),
        id="statistics.get_treatment_sales_summary",
    ),
    pytest.param(
        lambda db: statistics_crud.get_customer_insight_bulk(db, 1, [1, 2]),
        id="statistics.get_customer_insight_bulk",
    ),
    pytest.param(
        lambda db: statistics_crud.get_staff_summary(db, 1, _START, _END),
        id="statistics.get_staff_summary",
    ),
    pytest.param(
        lambda db: phonebook_crud.get_group_counts_by_groupname(db, 1),
        id="phonebook.get_group_counts_by_groupname",
    ),
    pytest.param(
        treatment_crud.get_treatments_to_autocomplete,
        id="treatment.get_treatments_to_autocomplete",
    ),
]


@pytest.mark.parametrize("run_query", _GROUP_BY_QUERIES)
def test_group_by_covers_every_non_aggregated_column(
    run_query: Callable[[CaptureSession], object],
) -> None:
    """GROUP BY 조회의 비집계 SELECT 컬럼은 모두 GROUP BY 에 포함되어야 한다."""
    db = CaptureSession()
    run_query(db)

    (stmt,) = db.statements
    sql = _pg_sql(stmt)
    group_by = _group_by_items(sql)
    missing = [col for col in _non_aggregated_columns(stmt) if col not in group_by]
    assert not missing, f"GROUP BY 누락(PostgreSQL 거부 대상): {missing}\n{sql}"


def test_staff_summary_group_by_includes_users_name() -> None:
    """get_staff_summary 는 SELECT 한 users.name 을 GROUP BY 에 포함해야 한다."""
    db = CaptureSession()
    statistics_crud.get_staff_summary(db, 1, _START, _END)

    (stmt,) = db.statements
    assert "users.name" in _group_by_items(_pg_sql(stmt))
