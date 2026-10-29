"""시술 메뉴 상세 생성/수정/삭제 샵 소유권 보안 테스트.

대상:
- app/crud/treatment_menu_crud.py::get_menu_detail_by_id
  (shop_id 인자를 받고도 무시 → 타 상점 상세 수정/삭제 가능)
- app/services/treatment_menu_service.py::create_treatment_menu_detail_service
  (생성 시 상위 메뉴의 상점 소유권 미검증 → 타 상점 메뉴에 상세 추가 가능)
- app/services/treatment_menu_service.py::delete_treatment_menu_detail_service

요구 동작:
- get_menu_detail_by_id 는 TreatmentMenu 를 조인해 TreatmentMenu.shop_id 로 스코핑하고,
  읽기 경로(get_treatment_menu_details_by_user)와 같은 삭제 상태 필터를 쓴다.
- 생성은 get_menu_by_id(상점 스코프, 삭제 제외)로 메뉴를 먼저 확인하고 없으면 404.
- 타 상점 상세 수정/삭제는 기존 메시지의 404 이며 대상은 바뀌지 않는다.

DB 없이 실제 CRUD 함수를 그대로 실행한다. db.query(...).join/filter 체인을 in-memory 행
위에서 SQL 의미로 평가하는 최소 fake 세션을 쓴다(기존 hand-rolled fake/spy 패턴의 확장).
"""

import itertools
from collections import defaultdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    Null,
)
from sqlalchemy.sql.util import join_condition

# 문자열 관계 참조("User", "Shop" 등)가 해석되도록 매퍼 구성 전에 전체 모델을 로드한다.
import app.models  # noqa: F401
from app.crud import treatment_menu_crud
from app.exceptions import CustomException
from app.models.treatment_menu import TreatmentMenu
from app.models.treatment_menu_detail import TreatmentMenuDetail
from app.schemas.treatment_menu import (
    TreatmentMenuDetailCreate,
    TreatmentMenuDetailResponse,
)
from app.services import treatment_menu_service

# ---------------------------------------------------------------------------
# in-memory fake 세션: 실제 CRUD 쿼리를 행 위에서 평가한다
# ---------------------------------------------------------------------------


def _operand(element: object, row_ctx: dict[str, object]) -> object:
    """조건식 피연산자를 값으로 바꾼다(바인드 값 / NULL / 컬럼은 행 속성값)."""
    if isinstance(element, BindParameter):
        return element.effective_value
    if isinstance(element, Null):
        return None
    table = getattr(element, "table", None)
    if table is None or table.name not in row_ctx:
        msg = f"fake 가 평가할 수 없는 피연산자(미조인 테이블 등): {element}"
        raise AssertionError(msg)
    return getattr(row_ctx[table.name], element.name)


def _matches(expr: object, row_ctx: dict[str, object]) -> bool:
    """WHERE/ON 조건식(AND, =, IS NULL, IS NOT NULL)을 SQL 의미로 평가한다."""
    if isinstance(expr, BooleanClauseList) and expr.operator is operators.and_:
        return all(_matches(clause, row_ctx) for clause in expr.clauses)
    if not isinstance(expr, BinaryExpression):
        msg = f"fake 미지원 조건식: {expr}"
        raise TypeError(msg)
    left = _operand(expr.left, row_ctx)
    right = _operand(expr.right, row_ctx)
    if expr.operator is operators.eq:
        # SQL 에서 NULL = x 는 참이 아니다.
        return left is not None and left == right
    if expr.operator is operators.is_:
        return left is right
    if expr.operator is operators.is_not:
        return left is not right
    msg = f"fake 미지원 연산자: {expr.operator}"
    raise AssertionError(msg)


class FakeQuery:
    """db.query(Model) 체인(join/filter/first)을 행 위에서 평가한다."""

    def __init__(self, db: "FakeSession", model: type) -> None:
        self.db = db
        self.model = model
        self.joins: list[tuple[str, object]] = []
        self.criteria: list[object] = []
        db.queries.append(self)

    def join(self, target: type, onclause: object | None = None) -> "FakeQuery":
        if onclause is None:
            # 관계 기반 암시적 조인은 FK 메타데이터로 ON 절을 유도한다.
            onclause = join_condition(self.model.__table__, target.__table__)
        self.joins.append((target.__tablename__, onclause))
        return self

    def filter(self, *criteria: object) -> "FakeQuery":
        self.criteria.extend(criteria)
        return self

    def first(self) -> object | None:
        for row in self.db.tables[self.model.__tablename__]:
            contexts = [{self.model.__tablename__: row}]
            for table_name, onclause in self.joins:  # INNER JOIN
                contexts = [
                    {**ctx, table_name: joined}
                    for ctx in contexts
                    for joined in self.db.tables[table_name]
                    if _matches(onclause, {**ctx, table_name: joined})
                ]
            if any(all(_matches(c, ctx) for c in self.criteria) for ctx in contexts):
                return row
        return None


class FakeSession:
    """CRUD/서비스가 쓰는 Session API(query/add/flush/commit/rollback/refresh)."""

    def __init__(self, *rows: object) -> None:
        self.tables: dict[str, list] = defaultdict(list)
        for row in rows:
            self.tables[row.__tablename__].append(row)
        self.queries: list[FakeQuery] = []
        self.added: list = []
        self.commit_count = 0
        self.rollback_count = 0
        self._ids = itertools.count(90000)

    def query(self, model: type) -> FakeQuery:
        return FakeQuery(self, model)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        # INSERT 시뮬레이션: PK/타임스탬프 기본값을 채우고 조회 대상 행으로 등록한다.
        for obj in self.added:
            if obj.id is None:
                obj.id = next(self._ids)
                obj.created_at = obj.updated_at = datetime.now(UTC)
                self.tables[obj.__tablename__].append(obj)

    def commit(self) -> None:
        self.flush()
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def refresh(self, obj: object) -> None:
        """DB 재조회 없음(PK/타임스탬프는 flush 에서 채운다)."""


# ---------------------------------------------------------------------------
# 테스트 월드: 상점 A(현재 상점) / 상점 B(타 상점)
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SHOP_A, SHOP_B = 10, 20
CURRENT_SHOP = SimpleNamespace(id=SHOP_A, user_id=1)
PAYLOAD = TreatmentMenuDetailCreate(name="변경 시도", duration_min=99, base_price=99999)


def _detail(detail_id: int, menu_id: int, name: str) -> object:
    return TreatmentMenuDetail(
        id=detail_id,
        menu_id=menu_id,
        name=name,
        duration_min=30,
        base_price=10000,
        created_at=NOW,
        updated_at=NOW,
    )


def _world() -> FakeSession:
    return FakeSession(
        TreatmentMenu(id=1000, shop_id=SHOP_A, name="A메뉴"),
        TreatmentMenu(id=1001, shop_id=SHOP_A, name="A삭제메뉴", deleted_at=NOW),
        TreatmentMenu(id=2000, shop_id=SHOP_B, name="B메뉴"),
        _detail(1100, 1000, "A컷"),
        _detail(2100, 2000, "B컷"),
    )


def _get_detail(db: FakeSession, detail_id: int) -> object:
    return next(d for d in db.tables["treatment_menu_detail"] if d.id == detail_id)


def _snapshot(detail: object) -> tuple:
    return (
        detail.menu_id,
        detail.name,
        detail.duration_min,
        detail.base_price,
        detail.deleted_at,
    )


def _upsert_detail(
    db: FakeSession,
    menu_id: int,
    detail_id: int | None = None,
) -> TreatmentMenuDetailResponse:
    return treatment_menu_service.create_treatment_menu_detail_service(
        menu_id=menu_id,
        current_shop=CURRENT_SHOP,
        filters=PAYLOAD,
        db=db,
        detail_id=detail_id,
    )


def _delete_detail(db: FakeSession, menu_id: int, detail_id: int) -> None:
    treatment_menu_service.delete_treatment_menu_detail_service(
        db=db,
        current_shop=CURRENT_SHOP,
        menu_id=menu_id,
        detail_id=detail_id,
    )


# ---------------------------------------------------------------------------
# 타 상점 대상: 404 이고 대상은 그대로다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "menu_id",
    [2000, 999999, 1001],
    ids=["other-shop-menu", "nonexistent-menu", "soft-deleted-own-menu"],
)
def test_create_detail_under_menu_not_owned_is_404_and_not_created(
    menu_id: int,
) -> None:
    """현재 상점의 활성 메뉴가 아니면 상세를 만들 수 없다(INSERT 없음)."""
    db = _world()

    with pytest.raises(CustomException) as exc:
        _upsert_detail(db, menu_id)

    assert exc.value.status_code == 404
    assert db.added == []
    assert db.commit_count == 0
    assert len(db.tables["treatment_menu_detail"]) == 2


def test_create_detail_404_does_not_reveal_menu_existence() -> None:
    """타 상점 메뉴와 없는 메뉴는 같은 404 응답을 받는다(존재 여부 오라클 없음)."""
    bodies = []
    for menu_id in (2000, 999999):
        with pytest.raises(CustomException) as exc:
            _upsert_detail(_world(), menu_id)
        bodies.append(exc.value.detail)
    assert bodies[0] == bodies[1]


def test_update_other_shop_detail_is_404_and_unchanged() -> None:
    """타 상점 상세 수정은 기존 메시지의 404 이고 값은 바뀌지 않는다."""
    db = _world()
    before = _snapshot(_get_detail(db, 2100))

    with pytest.raises(CustomException) as exc:
        _upsert_detail(db, menu_id=2000, detail_id=2100)

    assert exc.value.status_code == 404
    assert exc.value.detail["hint"] == (
        "시술 메뉴 상세를 찾을 수 없거나 다른 상점의 메뉴이거나 삭제된 메뉴입니다."
    )
    assert _snapshot(_get_detail(db, 2100)) == before
    assert db.commit_count == 0


def test_delete_other_shop_detail_is_404_and_not_deleted() -> None:
    """타 상점 상세 삭제는 기존 메시지의 404 이고 소프트삭제되지 않는다."""
    db = _world()
    before = _snapshot(_get_detail(db, 2100))

    with pytest.raises(CustomException) as exc:
        _delete_detail(db, menu_id=2000, detail_id=2100)

    assert exc.value.status_code == 404
    assert exc.value.detail["hint"] == (
        "삭제할 시술 메뉴 상세를 찾을 수 없거나 다른 상점의 메뉴 이거나 "
        "삭제된 메뉴입니다."
    )
    assert _snapshot(_get_detail(db, 2100)) == before
    assert db.commit_count == 0


# ---------------------------------------------------------------------------
# 자기 상점 대상: 정상 동작(회귀 방지)
# ---------------------------------------------------------------------------


def test_create_detail_under_own_menu_succeeds() -> None:
    """현재 상점 활성 메뉴에는 상세를 만들 수 있다."""
    db = _world()

    result = _upsert_detail(db, menu_id=1000)

    assert result.menu_id == 1000
    assert result.name == "변경 시도"
    assert db.commit_count == 1
    assert len(db.tables["treatment_menu_detail"]) == 3


def test_update_own_detail_succeeds() -> None:
    """현재 상점 상세는 수정할 수 있다."""
    db = _world()

    result = _upsert_detail(db, menu_id=1000, detail_id=1100)

    assert result.id == 1100
    detail = _get_detail(db, 1100)
    assert (detail.name, detail.duration_min, detail.base_price) == (
        "변경 시도",
        99,
        99999,
    )
    assert db.commit_count == 1


def test_delete_own_detail_soft_deletes() -> None:
    """현재 상점 상세는 소프트삭제할 수 있다."""
    db = _world()

    _delete_detail(db, menu_id=1000, detail_id=1100)

    assert _get_detail(db, 1100).deleted_at is not None
    assert db.commit_count == 1


# ---------------------------------------------------------------------------
# CRUD 계약: 쓰기 경로 조회도 읽기 경로와 같은 상점/삭제 필터를 쓴다
# ---------------------------------------------------------------------------


def test_get_menu_detail_by_id_is_scoped_like_read_path() -> None:
    """treatment_menu 를 조인해 shop_id 로 스코핑하고 삭제된 상세를 제외한다."""
    db = FakeSession()
    treatment_menu_crud.get_menu_detail_by_id(
        db=db,
        menu_id=1000,
        detail_id=1100,
        shop_id=SHOP_A,
    )

    [query] = db.queries
    assert [table for table, _ in query.joins] == ["treatment_menu"]
    sql = " AND ".join(
        str(c.compile(compile_kwargs={"literal_binds": True})) for c in query.criteria
    )
    assert "treatment_menu_detail.id = 1100" in sql
    assert "treatment_menu_detail.menu_id = 1000" in sql
    assert "treatment_menu.shop_id = 10" in sql
    assert "treatment_menu_detail.deleted_at IS NULL" in sql
