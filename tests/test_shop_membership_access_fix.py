"""샵 멤버십 기반 데이터 접근 교정 테스트.

대상:
- app/crud/shop_crud.py::get_member_shop_by_id, get_member_shops (신규)
- app/dependencies/shop.py::get_current_shop
- app/services/shop_service.py::get_my_shops_service, set_selected_shop_service,
  get_selected_shop_service, upsert_shop_service(수정 경로)

버그: 샵 데이터 접근을 Shop.user_id == user.id(생성자)로만 판정했다. 초대로 가입한
MANAGER 는 shop_user 멤버십이 있어도 샵 목록이 비어 있고 샵 선택은 404 였다. 그래서
shop-scoped API(시술/전화번호부/메뉴/대시보드)를 하나도 쓸 수 없었다. 반대로
멤버십을 해제해도 생성자의 접근은 회수되지 않았다.

요구 동작(사용자 결정):
- 데이터 접근은 멤버십(shop_user) 기준이다. MASTER/MANAGER 구분 없이 멤버면 허용한다.
- 샵 관리(샵 수정, 초대, 직원 관리)는 지금처럼 원장 전용이다.
- get_current_shop 은 요청마다 재검증한다. 멤버십이 해제되면 다음 요청부터
  404 SHOP_NOT_FOUND(기존과 같은 응답)다.

DB 없이 실제 CRUD/서비스를 실행한다. db.query(...) 의 join/filter/filter_by/order_by
체인을 in-memory 행 위에서 SQL 의미로 평가하는 최소 fake 세션을 쓴다
(tests/test_treatment_cross_tenant_fk_security.py 와 같은 패턴). SQL 계약은 세션 없는
실제 Query 를 PostgreSQL 방언으로 컴파일해 확인한다.
"""

from collections import defaultdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import (
    BinaryExpression,
    BindParameter,
    BooleanClauseList,
    Null,
    UnaryExpression,
)

# 문자열 관계 참조("User" 등)가 해석되도록 매퍼 구성 전에 전체 모델을 로드한다.
import app.models  # noqa: F401
from app.crud import shop_crud
from app.dependencies import shop as shop_dependency
from app.dependencies.shop import get_current_shop
from app.exceptions import CustomException
from app.models.shop import Shop
from app.models.shop_user import ShopUser
from app.schemas.shop import ShopUpdate
from app.services import shop_service, shop_user_service

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
    """WHERE/ON 조건식(AND, =, IS NULL)을 SQL 의미로 평가한다."""
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
    msg = f"fake 미지원 연산자: {expr.operator}"
    raise AssertionError(msg)


class FakeQuery:
    """db.query(Model) 체인(join/filter/filter_by/order_by/first/all)을 평가한다."""

    def __init__(self, db: "FakeSession", model: type) -> None:
        self.db = db
        self.model = model
        self.joins: list[tuple[str, object]] = []
        self.criteria: list[object] = []
        self.ordering: list[object] = []

    def join(self, target: type, onclause: object) -> "FakeQuery":
        self.joins.append((target.__tablename__, onclause))
        return self

    def filter(self, *criteria: object) -> "FakeQuery":
        self.criteria.extend(criteria)
        return self

    def filter_by(self, **kwargs: object) -> "FakeQuery":
        return self.filter(
            *(getattr(self.model, key) == value for key, value in kwargs.items()),
        )

    def order_by(self, *clauses: object) -> "FakeQuery":
        self.ordering.extend(clauses)
        return self

    def all(self) -> list:
        result = []
        for row in self.db.rows(self.model.__tablename__):
            contexts = [{self.model.__tablename__: row}]
            for table_name, onclause in self.joins:  # INNER JOIN
                contexts = [
                    {**ctx, table_name: joined}
                    for ctx in contexts
                    for joined in self.db.rows(table_name)
                    if _matches(onclause, {**ctx, table_name: joined})
                ]
            if any(all(_matches(c, ctx) for c in self.criteria) for ctx in contexts):
                result.append(row)
        for clause in reversed(self.ordering):  # 안정 정렬: 뒤 키부터 적용
            descending = False
            column = clause
            if isinstance(clause, UnaryExpression):
                descending = clause.modifier is operators.desc_op
                column = clause.element
            result.sort(key=lambda row: getattr(row, column.name), reverse=descending)
        return result

    def first(self) -> object | None:
        rows = self.all()
        return rows[0] if rows else None


class FakeSession:
    """CRUD/서비스가 쓰는 Session API(query/delete/commit/rollback/refresh)."""

    def __init__(self, *rows: object) -> None:
        self.tables: dict[str, list] = defaultdict(list)
        for row in rows:
            self.tables[row.__tablename__].append(row)
        self.deleted: list = []
        self.commit_count = 0
        self.rollback_count = 0

    def rows(self, table_name: str) -> list:
        return [row for row in self.tables[table_name] if row not in self.deleted]

    def query(self, model: type) -> FakeQuery:
        return FakeQuery(self, model)

    def delete(self, obj: object) -> None:
        self.deleted.append(obj)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def refresh(self, obj: object) -> None:
        """DB 재조회 없음."""


# ---------------------------------------------------------------------------
# 테스트 월드
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SHOP_A, SHOP_GONE, SHOP_B, SHOP_C = 10, 11, 20, 30

OWNER = SimpleNamespace(id=1, role="MASTER")  # 샵 A 생성자이자 대표원장
OUTSIDER = SimpleNamespace(id=2, role="MASTER")  # 샵 B 원장(샵 A 비멤버)
MANAGER = SimpleNamespace(id=3, role="MANAGER")  # 초대 가입 매니저(샵 A·C 멤버)
CO_MASTER = SimpleNamespace(id=4, role="MASTER")  # 샵 A 에 연결된 비생성자 MASTER
STRANGER = SimpleNamespace(id=7, role="MANAGER")  # 어떤 샵의 멤버도 아님
OTHER_OWNER_ID = 6  # 샵 C 생성자


def _shop(shop_id: int, user_id: int, deleted_at: datetime | None = None) -> Shop:
    return Shop(
        id=shop_id,
        user_id=user_id,
        name=f"샵{shop_id}",
        address=f"주소{shop_id}",
        deleted_at=deleted_at,
        created_at=NOW,
        updated_at=NOW,
    )


def _world() -> FakeSession:
    links = [
        (SHOP_A, OWNER.id, 1),
        (SHOP_A, MANAGER.id, 0),
        (SHOP_A, CO_MASTER.id, 0),
        (SHOP_B, OUTSIDER.id, 1),
        (SHOP_C, OTHER_OWNER_ID, 1),
        (SHOP_C, MANAGER.id, 0),
        (SHOP_GONE, OWNER.id, 1),
        (SHOP_GONE, MANAGER.id, 0),
    ]
    return FakeSession(
        _shop(SHOP_A, OWNER.id),
        _shop(SHOP_GONE, OWNER.id, deleted_at=NOW),  # 소프트삭제된 샵
        _shop(SHOP_B, OUTSIDER.id),
        _shop(SHOP_C, OTHER_OWNER_ID),
        *(
            ShopUser(id=i, shop_id=shop_id, user_id=user_id, is_primary_owner=primary)
            for i, (shop_id, user_id, primary) in enumerate(links, start=1)
        ),
    )


def _membership(db: FakeSession, shop_id: int, user_id: int) -> ShopUser:
    return next(
        row
        for row in db.rows(ShopUser.__tablename__)
        if (row.shop_id, row.user_id) == (shop_id, user_id)
    )


def _get_shop(db: FakeSession, shop_id: int) -> Shop:
    return next(row for row in db.rows(Shop.__tablename__) if row.id == shop_id)


def _assert_shop_not_found(db: FakeSession, user: SimpleNamespace) -> None:
    """get_current_shop 이 기존과 같은 404 SHOP_NOT_FOUND 로 거절하는지 확인한다."""
    with pytest.raises(CustomException) as exc:
        get_current_shop(db=db, user=user)
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "SHOP_NOT_FOUND"


@pytest.fixture
def db() -> FakeSession:
    return _world()


@pytest.fixture
def selected(monkeypatch: pytest.MonkeyPatch) -> dict[int, int]:
    """선택 샵 Redis 를 dict(user_id -> shop_id)로 대체한다."""
    store: dict[int, int] = {}
    for module in (shop_dependency, shop_service):
        monkeypatch.setattr(module, "get_selected_shop_redis", store.get)
        monkeypatch.setattr(module, "set_selected_shop_redis", store.__setitem__)
    return store


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> None:
    """실제 Query 가 필요한 paginate 를 목록 평가(all)로 대체한다."""
    monkeypatch.setattr(shop_crud, "paginate", lambda query: query.all())


# ---------------------------------------------------------------------------
# 멤버 데이터 접근: 목록 · 선택 · get_current_shop
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("listing")
@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (MANAGER, [SHOP_C, SHOP_A]),
        (OWNER, [SHOP_A]),
        (OUTSIDER, [SHOP_B]),
        (STRANGER, []),
    ],
    ids=["member-manager", "owner", "other-shop-owner", "no-membership"],
)
def test_shop_list_is_membership_based(
    db: FakeSession,
    user: SimpleNamespace,
    expected: list[int],
) -> None:
    """샵 목록은 멤버인 활성 샵만 id 내림차순으로 준다(소프트삭제 샵 제외)."""
    shops = shop_service.get_my_shops_service(db, user)

    assert [shop.id for shop in shops] == expected


def test_member_manager_selects_and_uses_shop(
    db: FakeSession,
    selected: dict[int, int],
) -> None:
    """초대 가입 MANAGER 도 멤버인 샵을 선택하고 shop-scoped API 를 쓸 수 있다."""
    shop_service.set_selected_shop_service(db, MANAGER, SHOP_A)

    assert selected[MANAGER.id] == SHOP_A
    assert shop_service.get_selected_shop_service(db, MANAGER).id == SHOP_A
    assert get_current_shop(db=db, user=MANAGER).id == SHOP_A


@pytest.mark.parametrize(
    "user",
    [OUTSIDER, STRANGER],
    ids=["other-shop-owner", "no-membership"],
)
def test_non_member_cannot_select_shop(
    db: FakeSession,
    selected: dict[int, int],
    user: SimpleNamespace,
) -> None:
    """비멤버는 다른 샵을 선택할 수 없다(404, 선택값 미저장)."""
    with pytest.raises(CustomException) as exc:
        shop_service.set_selected_shop_service(db, user, SHOP_A)

    assert exc.value.status_code == 404
    assert user.id not in selected


@pytest.mark.parametrize(
    "user",
    [OUTSIDER, STRANGER],
    ids=["other-shop-owner", "no-membership"],
)
def test_non_member_with_planted_selection_is_denied(
    db: FakeSession,
    selected: dict[int, int],
    user: SimpleNamespace,
) -> None:
    """Redis 에 남거나 위조된 선택값이 있어도 비멤버는 샵 데이터에 접근할 수 없다."""
    selected[user.id] = SHOP_A

    _assert_shop_not_found(db, user)
    with pytest.raises(CustomException) as exc:
        shop_service.get_selected_shop_service(db, user)
    assert exc.value.status_code == 404


def test_member_cannot_select_soft_deleted_shop(
    db: FakeSession,
    selected: dict[int, int],
) -> None:
    """멤버십이 있어도 소프트삭제된 샵은 선택할 수 없다."""
    with pytest.raises(CustomException) as exc:
        shop_service.set_selected_shop_service(db, MANAGER, SHOP_GONE)

    assert exc.value.status_code == 404
    assert MANAGER.id not in selected


# ---------------------------------------------------------------------------
# 멤버십 해제 = 다음 요청부터 접근 회수
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("listing")
def test_removed_member_loses_access_on_next_request(
    db: FakeSession,
    selected: dict[int, int],
) -> None:
    """원장이 멤버십을 해제하면 남은 선택값이 있어도 다음 요청부터 404 다."""
    shop_service.set_selected_shop_service(db, MANAGER, SHOP_A)
    assert get_current_shop(db=db, user=MANAGER).id == SHOP_A

    shop_user_service.delete_shop_user_service(
        db,
        shop_id=SHOP_A,
        user_id=MANAGER.id,
        current_user=OWNER,
    )

    assert selected[MANAGER.id] == SHOP_A  # Redis 선택값은 그대로 남아 있다
    _assert_shop_not_found(db, MANAGER)
    with pytest.raises(CustomException) as exc:
        shop_service.get_selected_shop_service(db, MANAGER)
    assert exc.value.status_code == 404
    assert [shop.id for shop in shop_service.get_my_shops_service(db, MANAGER)] == [
        SHOP_C,
    ]


@pytest.mark.usefixtures("selected")
def test_membership_removal_also_revokes_creator_data_access(db: FakeSession) -> None:
    """접근 기준은 생성자 여부가 아니라 멤버십이다(생성자도 해제되면 회수)."""
    shop_service.set_selected_shop_service(db, OWNER, SHOP_A)

    db.delete(_membership(db, SHOP_A, OWNER.id))

    _assert_shop_not_found(db, OWNER)


# ---------------------------------------------------------------------------
# 샵 관리는 원장 전용 그대로
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "status_code"),
    [(MANAGER, 403), (CO_MASTER, 404)],
    ids=["member-manager", "non-creator-master-member"],
)
def test_shop_update_by_non_owner_member_is_denied(
    db: FakeSession,
    user: SimpleNamespace,
    status_code: int,
) -> None:
    """멤버라도 생성자가 아니면 샵을 수정할 수 없다(커밋/변경 없음)."""
    payload = ShopUpdate(name="탈취한 샵", address="어딘가")

    with pytest.raises(CustomException) as exc:
        shop_service.upsert_shop_service(db, user, payload, shop_id=SHOP_A)

    assert exc.value.status_code == status_code
    assert db.commit_count == 0
    assert _get_shop(db, SHOP_A).name == f"샵{SHOP_A}"


def test_shop_update_by_owner_still_works(db: FakeSession) -> None:
    """생성자 원장의 샵 수정은 그대로 동작한다."""
    payload = ShopUpdate(name="리뉴얼 샵", address="새 주소")

    shop = shop_service.upsert_shop_service(db, OWNER, payload, shop_id=SHOP_A)

    assert (shop.id, shop.name) == (SHOP_A, "리뉴얼 샵")
    assert db.commit_count == 1


# ---------------------------------------------------------------------------
# CRUD SQL 계약: shop_user 를 (shop_id, user_id) 로 조인하고 삭제 샵을 제외한다
# ---------------------------------------------------------------------------


class RecordingQuery(Query):
    """세션 없는 실제 Query. first() 는 실행 대신 SELECT 문을 기록한다."""

    def first(self) -> None:
        self.recorded.append(self.statement)


class CompileSession:
    def __init__(self) -> None:
        self.statements: list = []

    def query(self, *entities: type) -> RecordingQuery:
        query = RecordingQuery(entities)
        query.recorded = self.statements  # 생성형 체인 복사본에도 전달된다
        return query


def _sql(statement: object) -> str:
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return " ".join(str(compiled).split())


MEMBER_JOIN = "JOIN shop_user ON shop_user.shop_id = shop.id AND shop_user.user_id = 3"


def test_member_shop_lookup_sql_joins_membership() -> None:
    """단건 조회는 멤버십 조인 + 삭제 샵 제외다. 생성자(user_id)로 거르지 않는다."""
    db = CompileSession()

    shop_crud.get_member_shop_by_id(db, user_id=3, shop_id=10)

    [statement] = db.statements
    sql = _sql(statement)
    assert MEMBER_JOIN in sql
    assert "shop.id = 10" in sql
    assert "shop.deleted_at IS NULL" in sql
    assert "shop.user_id = 3" not in sql


def test_member_shop_list_sql_joins_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    """목록 조회도 같은 멤버십 조인 + 삭제 샵 제외 + id 내림차순이다."""
    monkeypatch.setattr(shop_crud, "paginate", lambda query: query)

    query = shop_crud.get_member_shops(CompileSession(), user_id=3)

    sql = _sql(query.statement)
    assert MEMBER_JOIN in sql
    assert "shop.deleted_at IS NULL" in sql
    assert "shop.user_id = 3" not in sql
    assert sql.endswith("ORDER BY shop.id DESC")
