"""DB 없이 SQLAlchemy SELECT 문을 in-memory 행 위에서 평가하는 SQL 엔진(테스트 전용).

CI 에는 실DB 가 없다. CRUD 가 만든 실제 문(statement)을 SQL 의미로 평가해 조인 행
곱(fan-out)이나 GROUP BY 집계 같은 SQL 수준 버그를 DB 없이 재현한다. 결과를 손으로
지어내는 스텁과 달리 SQL 자체의 오류가 그대로 드러난다. 지원하지 않는 구문은 조용히
넘기지 않고 AssertionError 로 실패시킨다(평가기 공백이 테스트 통과로 위장되지 않게).

지원 범위:
- FROM: 테이블, 테이블 별칭(aliased 자기조인), INNER / LEFT OUTER JOIN,
  서브쿼리(페이지네이션 count 문)
- WHERE: AND / OR, = != < <= > >=, IN, IS [NOT] NULL (SQL NULL 3값 논리),
  true / false 상수, ILIKE, 상관 EXISTS(관계 has()/any())
- SELECT: 컬럼, 엔티티(select(Model)), CASE, coalesce, + / * 산술, GROUP BY, HAVING
- 집계: count(*), count(x), count(DISTINCT x), sum(x)
- ORDER BY(ASC/DESC, PG 기본 NULL 순서), LIMIT / OFFSET
- 레거시 db.query(...) 체인(all/first)과 Query.update(), commit/rollback/close 기록
- add/flush(id·타임스탬프 부여)/delete(커밋 시 반영)/refresh

joinedload 같은 로더 옵션은 떼고 평가한다. ORM 은 컬렉션 eager load 에 LIMIT 가
붙으면 원래 문을 서브쿼리로 감싸 LIMIT 를 먼저 적용하고 eager JOIN 은 바깥에 붙인다.
즉 WHERE·정렬·LIMIT 가 보는 행은 명시적 FROM/JOIN 행뿐이다.

정렬 키가 같은 행(동률)의 순서는 SQL 이 보장하지 않는다. 실행마다 다를 수 있다는
점을 tie_order 훅으로 모델링한다(기본값은 저장 순서).

시간대가 있는 datetime 은 UTC naive 로 맞춰 비교한다(PG 세션 TZ=UTC + naive UTC
저장 규약).
"""

import itertools
import operator
import re
from collections import defaultdict, namedtuple
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Query
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    Case,
    ColumnClause,
    False_,
    Grouping,
    Label,
    Null,
    True_,
    UnaryExpression,
)
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.schema import Table
from sqlalchemy.sql.selectable import Alias, Exists, Join, Select, Subquery
from sqlalchemy.sql.visitors import iterate

# 테이블명 → 행 객체. LEFT OUTER JOIN 에서 짝이 없으면 None.
Context = dict[str, Any]
# (실행 순번, 정렬 전 행 문맥) → 엔진이 돌려줄 물리 순서. 동률 행의 순서를 정한다.
TieOrder = Callable[[int, list[Context]], list[Context]]

_AGGREGATES = frozenset({"count", "sum"})


def _normalize(value: object) -> object:
    """시간대가 있는 datetime 을 UTC naive 로 바꾼다(그 외 값은 그대로)."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _table_source(from_clause: object) -> tuple[str, str] | None:
    """테이블/테이블 별칭이면 (행 문맥 키, 실제 테이블명), 그 외에는 None.

    별칭(aliased 자기조인)은 별칭 이름을 문맥 키로 써서 같은 테이블의 행을 따로 묶는다.
    """
    if isinstance(from_clause, Table):
        return from_clause.name, from_clause.name
    if isinstance(from_clause, Alias) and isinstance(from_clause.element, Table):
        return from_clause.name, from_clause.element.name
    return None


def _has_aggregate(expr: object) -> bool:
    return any(
        isinstance(node, FunctionElement) and node.name in _AGGREGATES
        for node in iterate(expr)
    )


def _and(values: list[object]) -> bool | None:
    if any(v is False for v in values):
        return False
    return None if any(v is None for v in values) else True


def _or(values: list[object]) -> bool | None:
    if any(v is True for v in values):
        return True
    return None if any(v is None for v in values) else False


def _ilike(value: str, pattern: str) -> bool:
    """패턴을 SQL ILIKE 로 평가한다(% 는 임의 길이, _ 는 한 글자, 대소문자 무시)."""
    regex = "".join(
        ".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern
    )
    return re.fullmatch(regex, value, re.IGNORECASE | re.DOTALL) is not None


# 피연산자가 모두 NULL 이 아닐 때의 이항 연산. 산술은 시각 + 소요시간(interval)
# 계산용이다(datetime + timedelta, int * timedelta).
_BINARY_OPERATORS: dict[Callable[[Any, Any], Any], Callable[[Any, Any], Any]] = {
    operators.eq: operator.eq,
    operators.ne: operator.ne,
    operators.lt: operator.lt,
    operators.le: operator.le,
    operators.gt: operator.gt,
    operators.ge: operator.ge,
    operators.in_op: lambda left, right: left in right,
    operators.ilike_op: _ilike,
    operators.add: operator.add,
    operators.mul: operator.mul,
}


def _without_loader_options(stmt: Select) -> Select:
    """로더 옵션(joinedload 등)을 뗀 사본을 만든다(명시적 FROM/JOIN 만 남는다)."""
    if not stmt._with_options:  # noqa: SLF001  # 옵션 제거용 공개 API 없음
        return stmt
    core = stmt._generate()  # noqa: SLF001
    core._with_options = ()  # noqa: SLF001
    return core


class FakeResult:
    """Session.execute() 결과(fetchall/all/unique)."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return list(self._rows)

    def all(self) -> list:
        return list(self._rows)

    def unique(self) -> "FakeResult":
        # ORM 엔티티 행은 객체 동일성으로 중복 제거된다(Result.unique()).
        return FakeResult(list(dict.fromkeys(self._rows)))

    def scalars(self) -> "FakeResult":
        return FakeResult([row[0] for row in self._rows])


class _FakeQuery(Query):
    """레거시 db.query() 체인. 최종 문을 fake 세션에서 평가한다.

    Query 는 generative 복사 때 __dict__ 를 복제하므로 fake_db 가 체인 끝까지 전달된다.
    """

    fake_db: "FakeSQLSession"

    def all(self) -> list:
        rows = self.fake_db.execute(self.statement).all()
        (desc, *rest) = self.column_descriptions
        if (
            not rest
            and desc.get("entity") is not None
            and desc["expr"] is desc["entity"]
        ):
            # db.query(Model).all() 은 Row 가 아니라 엔티티 객체 목록을 돌려준다.
            return [row[0] for row in rows]
        return rows

    def first(self) -> object | None:
        rows = self.limit(1).all()
        return rows[0] if rows else None

    def update(
        self,
        values: dict,
        synchronize_session: object = None,  # noqa: ARG002  # Query.update 시그니처
        update_args: dict | None = None,  # noqa: ARG002
    ) -> int:
        """UPDATE ... WHERE: 조건에 맞는 행만 values 로 바꾸고 건수를 돌려준다."""
        (desc,) = self.column_descriptions
        table = desc["entity"].__tablename__
        where = self.statement.whereclause
        matched = [
            row
            for row in self.fake_db.rows(table)
            if where is None or self.fake_db.value(where, {table: row}) is True
        ]
        for row in matched:
            for column, value in values.items():
                setattr(row, getattr(column, "key", column), value)
        self.fake_db.updates.append([row.id for row in matched])
        return len(matched)


class FakeSQLSession:
    """테이블별 행 목록 위에서 SELECT 문을 평가하는 DB-free 세션."""

    def __init__(self, *rows: object, tie_order: TieOrder | None = None) -> None:
        self.tables: dict[str, list] = defaultdict(list)
        for row in rows:
            self.tables[row.__tablename__].append(row)
        self.statements: list[Select] = []
        self.tie_order = tie_order
        self.updates: list[list[int]] = []  # Query.update() 가 바꾼 행 id(호출별)
        self.added: list = []  # add() 된 객체 전체(관찰용)
        self.deleted: list = []  # 커밋으로 삭제된 객체(관찰용)
        self._pending_deletes: list = []
        self._ids = itertools.count(90000)
        self.commit_count = 0
        self.rollback_count = 0
        self.closed = False

    def rows(self, table_name: str) -> list:
        return list(self.tables[table_name])

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        """INSERT 시뮬레이션: PK/타임스탬프를 채우고 조회 대상 행으로 등록한다."""
        for obj in self.added:
            if obj.id is None:
                obj.id = next(self._ids)
                obj.created_at = obj.updated_at = datetime.now(UTC)
                self.tables[obj.__tablename__].append(obj)

    def delete(self, obj: object) -> None:
        self._pending_deletes.append(obj)

    def refresh(self, obj: object) -> None:
        """DB 재조회 없음(PK/타임스탬프는 flush 에서 채운다)."""

    def query(self, *entities: object) -> _FakeQuery:
        query = _FakeQuery(entities)
        query.fake_db = self
        return query

    def execute(self, stmt: Select) -> FakeResult:
        self.statements.append(stmt)
        return FakeResult(self.select(stmt, execution=len(self.statements)))

    def scalar(self, stmt: Select) -> object:
        rows = self.execute(stmt).all()
        return rows[0][0] if rows else None

    def commit(self) -> None:
        self.flush()
        for obj in self._pending_deletes:
            self.tables[obj.__tablename__].remove(obj)
            self.deleted.append(obj)
        self._pending_deletes.clear()
        self.commit_count += 1

    def rollback(self) -> None:
        self._pending_deletes.clear()
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True

    # ------------------------------------------------------------------ SELECT

    def select(self, stmt: Select, execution: int = 0) -> list:
        contexts = self._from_where(stmt, {})
        if self.tie_order is not None:
            contexts = self.tie_order(execution, contexts)
        columns = list(stmt.selected_columns)
        group_by = stmt._group_by_clauses  # noqa: SLF001  # 공개 접근자 없음
        having = stmt._having_criteria  # noqa: SLF001
        if group_by or having or any(_has_aggregate(col) for col in columns):
            units = [
                (group[0] if group else {}, group)
                for group in self._groups(contexts, group_by)
            ]
            units = [
                unit
                for unit in units
                if all(self.value(cond, *unit) is True for cond in having)
            ]
        else:
            units = [(ctx, None) for ctx in contexts]
        units = self._order(units, stmt._order_by_clauses)  # noqa: SLF001
        offset = stmt._offset or 0  # noqa: SLF001
        limit = stmt._limit  # noqa: SLF001
        units = units[offset : None if limit is None else offset + limit]
        return self._project(stmt, units)

    def _project(self, stmt: Select, units: list) -> list:
        (entity_desc, *rest) = stmt.column_descriptions
        entity = entity_desc.get("entity")
        if not rest and entity is not None and entity_desc.get("expr") is entity:
            # select(Model): 행은 엔티티 객체 하나다.
            row_type = namedtuple("Row", [entity_desc["name"]])
            return [row_type(ctx[entity.__tablename__]) for ctx, _ in units]
        columns = list(stmt.selected_columns)
        row_type = namedtuple("Row", list(stmt.selected_columns.keys()))
        return [
            row_type(*(self.value(col, ctx, group) for col in columns))
            for ctx, group in units
        ]

    def _order(self, units: list, order_by: Sequence) -> list:
        """ORDER BY 를 안정 정렬로 적용한다(동률은 정렬 전 순서를 유지)."""
        units = list(units)
        for clause in reversed(order_by):
            descending = False
            expr = clause
            if isinstance(clause, UnaryExpression) and clause.modifier in {
                operators.asc_op,
                operators.desc_op,
            }:
                descending = clause.modifier is operators.desc_op
                expr = clause.element

            def key(unit: tuple, expr: object = expr) -> tuple:
                value = self.value(expr, *unit)
                # PG 기본: ASC 는 NULL 이 마지막, DESC 는 처음(역순 정렬과 일치).
                return (value is None, value)

            units.sort(key=key, reverse=descending)
        return units

    def _from_where(self, stmt: Select, outer: Context) -> list[Context]:
        contexts = [dict(outer)]
        for from_clause in _without_loader_options(stmt).get_final_froms():
            if isinstance(from_clause, Table) and from_clause.name in outer:
                continue  # 상관 서브쿼리: 바깥 행을 그대로 쓴다.
            contexts = [
                joined for ctx in contexts for joined in self._expand(from_clause, ctx)
            ]
        where = stmt.whereclause
        if where is not None:
            contexts = [ctx for ctx in contexts if self.value(where, ctx) is True]
        return contexts

    def _expand(self, from_clause: object, ctx: Context) -> list[Context]:
        """FROM 항목 하나를 행 문맥 목록으로 펼친다(JOIN 은 행 곱)."""
        if source := _table_source(from_clause):
            key, table = source
            return [{**ctx, key: row} for row in self.rows(table)]
        if isinstance(from_clause, Subquery):
            # count(*) FROM (서브쿼리) 용: 서브쿼리 행 수만큼 문맥을 만든다.
            name = str(from_clause.name)
            return [{**ctx, name: row} for row in self.select(from_clause.element)]
        if isinstance(from_clause, Join) and (
            source := _table_source(from_clause.right)
        ):
            key, table = source
            out = []
            for left_ctx in self._expand(from_clause.left, ctx):
                candidates = [{**left_ctx, key: row} for row in self.rows(table)]
                matched = [
                    joined
                    for joined in candidates
                    if self.value(from_clause.onclause, joined) is True
                ]
                if not matched and from_clause.isouter:
                    matched = [{**left_ctx, key: None}]
                out.extend(matched)
            return out
        msg = f"fake 미지원 FROM: {from_clause!r}"
        raise AssertionError(msg)

    def _groups(
        self,
        contexts: list[Context],
        group_by: Sequence,
    ) -> list[list[Context]]:
        if not group_by:
            # 집계만 있는 SELECT 는 대상 행이 없어도 1행을 돌려준다.
            return [contexts]
        groups: dict[tuple, list[Context]] = {}
        for ctx in contexts:
            key = tuple(self.value(expr, ctx) for expr in group_by)
            groups.setdefault(key, []).append(ctx)
        return list(groups.values())

    # -------------------------------------------------------------- 식 평가

    # 식 종류별 디스패치라 반환 분기가 본질적으로 많다(평가기 자체의 구조).
    def value(  # noqa: C901, PLR0911
        self,
        expr: object,
        ctx: Context,
        group: list[Context] | None = None,
    ) -> object:
        """식을 행 문맥(ctx) 기준으로 평가한다. 집계 함수는 group 전체로 평가한다."""
        if isinstance(expr, (Label, Grouping)):
            return self.value(expr.element, ctx, group)
        if isinstance(expr, BindParameter):
            value = expr.effective_value
            if isinstance(value, (list, tuple)):
                return [_normalize(v) for v in value]
            return _normalize(value)
        if isinstance(expr, Null):
            return None
        if isinstance(expr, (True_, False_)):
            # Boolean 컬럼을 파이썬 bool 과 비교하면(== True) 바인드 대신 상수가 된다.
            return isinstance(expr, True_)
        if isinstance(expr, Exists):
            inner = expr.element
            while not isinstance(inner, Select):  # ScalarSelect / Grouping 래퍼
                inner = inner.element
            return bool(self._from_where(inner, ctx))
        if isinstance(expr, Case):
            return self._case(expr, ctx, group)
        if isinstance(expr, FunctionElement):
            return self._function(expr, ctx, group)
        if isinstance(expr, BooleanClauseList):
            values = [self.value(clause, ctx, group) for clause in expr.clauses]
            return _and(values) if expr.operator is operators.and_ else _or(values)
        if isinstance(expr, BinaryExpression):
            return self._binary(expr, ctx, group)
        if isinstance(expr, ColumnClause) and _table_source(expr.table):
            # 별칭 컬럼의 table.name 은 별칭 이름이다(_expand 의 문맥 키와 같다).
            row = ctx[expr.table.name]
            return None if row is None else _normalize(getattr(row, expr.key))
        msg = f"fake 미지원 식: {expr!r}"
        raise AssertionError(msg)

    def _case(self, expr: Case, ctx: Context, group: list[Context] | None) -> object:
        for condition, result in expr.whens:
            if self.value(condition, ctx, group) is True:
                return self.value(result, ctx, group)
        return None if expr.else_ is None else self.value(expr.else_, ctx, group)

    def _binary(
        self,
        expr: BinaryExpression,
        ctx: Context,
        group: list[Context] | None,
    ) -> object:
        left = self.value(expr.left, ctx, group)
        right = self.value(expr.right, ctx, group)
        if expr.operator in {operators.is_, operators.is_not}:
            return (left is right) == (expr.operator is operators.is_)
        if expr.operator not in _BINARY_OPERATORS:
            msg = f"fake 미지원 연산자: {expr.operator}"
            raise AssertionError(msg)
        if left is None or right is None:
            return None  # SQL: NULL 과의 비교·연산은 UNKNOWN(NULL)
        return _BINARY_OPERATORS[expr.operator](left, right)

    def _function(
        self,
        expr: FunctionElement,
        ctx: Context,
        group: list[Context] | None,
    ) -> object:
        args = list(expr.clauses)
        if expr.name == "coalesce":
            values = (self.value(arg, ctx, group) for arg in args)
            return next((v for v in values if v is not None), None)
        if expr.name not in _AGGREGATES:
            msg = f"fake 미지원 함수: {expr.name}"
            raise AssertionError(msg)
        if group is None:
            msg = f"집계 함수 {expr.name} 는 GROUP 문맥에서만 평가할 수 있다"
            raise AssertionError(msg)
        (arg,) = args
        if expr.name == "count" and isinstance(arg, ColumnClause) and arg.name == "*":
            return len(group)
        distinct = False
        if isinstance(arg, FunctionElement) and arg.name == "distinct":
            (arg,) = arg.clauses
            distinct = True
        elif isinstance(arg, UnaryExpression) and arg.operator is operators.distinct_op:
            arg = arg.element
            distinct = True
        values = [v for member in group if (v := self.value(arg, member)) is not None]
        if distinct:
            values = list(dict.fromkeys(values))
        if expr.name == "count":
            return len(values)
        return sum(values) if values else None
