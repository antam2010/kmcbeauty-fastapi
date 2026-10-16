"""시술 예약 교차 테넌트 FK 검증 보안 테스트.

대상:
- app/services/treatment_service.py::upsert_treatment_service
  (_create_treatment / _update_treatment / _upsert_treatment_items)
- app/crud/treatment_crud.py::validate_menu_detail_exists

취약점: 클라이언트가 보낸 phonebook_id / staff_user_id / menu_detail_id 를
현재 상점 기준으로 검증하지 않고 저장했다. GET /treatments 는 phonebook 과
staff_user 를 joinedload 하므로, 순차 id 열거만으로 타 상점 고객(이름/전화/
메모)과 임의 유저 이메일이 유출되었다.

요구 동작:
- phonebook_id: 현재 상점 소속이며 소프트삭제되지 않은 고객만 허용한다.
- staff_user_id: 현재 상점 멤버(shop_user 행 존재)만 허용한다.
- menu_detail_id: 현재 상점 메뉴(TreatmentMenu.shop_id) 소속만 허용한다(항상).
  소프트삭제 검사는 이 예약에 새로 들어오는 참조에만 적용한다.
- 위반 시 필드별 단일 400 메시지를 쓴다. 없음/타 상점/삭제됨을 구분하지 않으므로
  존재 여부 오라클이 없다.
- 수정 시 phonebook/staff 는 값이 지정되고 현재 값과 다를 때만 검증한다
  (기존 예약 편집 비파괴).

DB 없이 실제 CRUD 함수를 그대로 실행한다. db.query(...) 의 join/filter/filter_by
체인을 in-memory 행 위에서 SQL 의미로 평가하는 최소 fake 세션을 쓴다(기존
hand-rolled fake/spy 패턴의 확장). CRUD 를 스텁하지 않으므로 서비스와 CRUD 의
실제 상점 스코핑 동작을 함께 관찰한다.
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
from app.crud import treatment_crud
from app.enum.treatment_status import TreatmentStatus
from app.exceptions import CustomException
from app.models.phonebook import Phonebook
from app.models.shop_user import ShopUser
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from app.models.treatment_menu import TreatmentMenu
from app.models.treatment_menu_detail import TreatmentMenuDetail
from app.schemas.treatment import (
    TreatmentCreate,
    TreatmentSimpleResponse,
    TreatmentUpdate,
)
from app.services import treatment_service

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
    """db.query(Model) 체인(join/filter/filter_by/first/all)을 행 위에서 평가한다."""

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

    def filter_by(self, **kwargs: object) -> "FakeQuery":
        return self.filter(
            *(getattr(self.model, key) == value for key, value in kwargs.items()),
        )

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
        return result

    def first(self) -> object | None:
        rows = self.all()
        return rows[0] if rows else None


class FakeSession:
    """CRUD/서비스가 쓰는 Session API(query/add/flush/delete/commit/rollback)."""

    def __init__(self, *rows: object) -> None:
        self.tables: dict[str, list] = defaultdict(list)
        for row in rows:
            self.tables[row.__tablename__].append(row)
        self.queries: list[FakeQuery] = []
        self.added: list = []
        self.deleted: list = []
        self.commit_count = 0
        self.rollback_count = 0
        self._ids = itertools.count(90000)

    def rows(self, table_name: str) -> list:
        return [row for row in self.tables[table_name] if row not in self.deleted]

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

    def delete(self, obj: object) -> None:
        self.deleted.append(obj)

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
CURRENT_SHOP = SimpleNamespace(id=SHOP_A, user_id=1)  # 원장 1 이 선택한 상점 A


def _detail(detail_id: int, menu_id: int, deleted_at: datetime | None = None) -> object:
    return TreatmentMenuDetail(
        id=detail_id,
        menu_id=menu_id,
        name=f"상세{detail_id}",
        duration_min=30,
        base_price=10000,
        deleted_at=deleted_at,
        created_at=NOW,
        updated_at=NOW,
    )


def _treatment(treatment_id: int, phonebook_id: int, staff_user_id: int) -> object:
    return Treatment(
        id=treatment_id,
        shop_id=SHOP_A,
        phonebook_id=phonebook_id,
        staff_user_id=staff_user_id,
        reserved_at=NOW,
        status=TreatmentStatus.RESERVED,
        created_at=NOW,
        updated_at=NOW,
    )


def _existing_item(item_id: int, treatment_id: int, menu_detail_id: int) -> object:
    return TreatmentItem(
        id=item_id,
        treatment_id=treatment_id,
        menu_detail_id=menu_detail_id,
        base_price=10000,
        duration_min=30,
        session_no=1,
    )


def _world() -> FakeSession:
    return FakeSession(
        # 멤버십: A = 원장 1 + 직원 3, B = 원장 2 + 직원 4 (유저 5 는 A 를 떠난 전 직원)
        ShopUser(id=1, shop_id=SHOP_A, user_id=1, is_primary_owner=1),
        ShopUser(id=2, shop_id=SHOP_A, user_id=3, is_primary_owner=0),
        ShopUser(id=3, shop_id=SHOP_B, user_id=2, is_primary_owner=1),
        ShopUser(id=4, shop_id=SHOP_B, user_id=4, is_primary_owner=0),
        # 고객
        Phonebook(id=100, shop_id=SHOP_A, name="A고객", phone_number="01000000100"),
        Phonebook(
            id=101,
            shop_id=SHOP_A,
            name="A삭제고객",
            phone_number="01000000101",
            deleted_at=NOW,
        ),
        Phonebook(
            id=200,
            shop_id=SHOP_B,
            name="B고객",
            phone_number="01000000200",
            memo="B 상점 비공개 메모",
        ),
        # 메뉴와 상세
        TreatmentMenu(id=1000, shop_id=SHOP_A, name="A메뉴"),
        TreatmentMenu(id=1001, shop_id=SHOP_A, name="A삭제메뉴", deleted_at=NOW),
        TreatmentMenu(id=2000, shop_id=SHOP_B, name="B메뉴"),
        _detail(1100, 1000),
        _detail(1101, 1000, deleted_at=NOW),  # 예약 500 이 참조한 뒤 삭제된 상세
        _detail(1102, 1001),  # 삭제된 메뉴의 (자체는 미삭제) 상세
        _detail(1103, 1000, deleted_at=NOW),  # 어떤 예약도 참조하지 않는 삭제 상세
        _detail(2100, 2000),  # 타 상점 상세
        # 예약 500: 이후 삭제된 고객 101 / 떠난 담당자 5 / 이후 삭제된 상세 1101
        _treatment(500, phonebook_id=101, staff_user_id=5),
        _existing_item(5001, 500, 1100),
        _existing_item(5002, 500, 1101),
        # 예약 600: 취약 시절 저장된 타 상점 상세(2100) 참조(소유권 상시 강제 확인용)
        _treatment(600, phonebook_id=100, staff_user_id=3),
        _existing_item(6001, 600, 2100),
    )


def _item(menu_detail_id: int, item_id: int | None = None) -> dict:
    return {
        "id": item_id,
        "menu_detail_id": menu_detail_id,
        "base_price": 10000,
        "duration_min": 30,
        "session_no": 1,
    }


def _create_payload(**overrides: object) -> TreatmentCreate:
    fields = {
        "reserved_at": NOW,
        "status": TreatmentStatus.RESERVED,
        "phonebook_id": 100,
        "staff_user_id": 3,
        "treatment_items": [_item(1100)],
    }
    return TreatmentCreate(**{**fields, **overrides})


# 예약 500 의 현재 참조를 그대로 다시 보내는 수정 payload(항목 id 포함).
UNCHANGED_500 = {
    "phonebook_id": 101,
    "staff_user_id": 5,
    "treatment_items": [_item(1100, item_id=5001), _item(1101, item_id=5002)],
}


def _update_payload(**overrides: object) -> TreatmentUpdate:
    fields = {"reserved_at": NOW, "status": TreatmentStatus.RESERVED, **UNCHANGED_500}
    return TreatmentUpdate(**{**fields, **overrides})


def _upsert(
    db: FakeSession,
    data: TreatmentCreate | TreatmentUpdate,
    treatment_id: int | None = None,
) -> TreatmentSimpleResponse:
    return treatment_service.upsert_treatment_service(
        db=db,
        data=data,
        current_shop=CURRENT_SHOP,
        treatment_id=treatment_id,
    )


def _rejection(
    db: FakeSession,
    data: TreatmentCreate | TreatmentUpdate,
    treatment_id: int | None = None,
) -> dict:
    """400 으로 거절되고 커밋되지 않았음을 확인한 뒤 응답 본문을 돌려준다."""
    with pytest.raises(CustomException) as exc:
        _upsert(db, data, treatment_id)
    assert exc.value.status_code == 400
    assert db.commit_count == 0
    return exc.value.detail


def _added(db: FakeSession, model: type) -> list:
    return [obj for obj in db.added if isinstance(obj, model)]


def _get(db: FakeSession, model: type, row_id: int) -> object:
    return next(row for row in db.tables[model.__tablename__] if row.id == row_id)


# ---------------------------------------------------------------------------
# 생성: 설정된 참조는 모두 검증한다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phonebook_id",
    [200, 101, 999999],
    ids=["other-shop", "soft-deleted", "nonexistent"],
)
def test_create_rejects_invalid_phonebook_without_insert(phonebook_id: int) -> None:
    """타 상점/삭제/없는 고객으로는 생성할 수 없고 INSERT 도 일어나지 않는다."""
    db = _world()
    _rejection(db, _create_payload(phonebook_id=phonebook_id))
    assert _added(db, Treatment) == []


@pytest.mark.parametrize(
    "staff_user_id",
    [4, 2, 999999],
    ids=["other-shop-staff", "other-shop-owner", "nonexistent"],
)
def test_create_rejects_non_member_staff_user(staff_user_id: int) -> None:
    """현재 상점 멤버가 아닌 담당자로는 생성할 수 없다(INSERT 없음)."""
    db = _world()
    _rejection(db, _create_payload(staff_user_id=staff_user_id))
    assert _added(db, Treatment) == []


@pytest.mark.parametrize(
    "menu_detail_id",
    [2100, 1101, 1102, 999999],
    ids=[
        "other-shop-detail",
        "soft-deleted-detail",
        "detail-of-soft-deleted-menu",
        "nonexistent",
    ],
)
def test_create_rejects_invalid_menu_detail(menu_detail_id: int) -> None:
    """타 상점 상세 또는 신규 참조로서 삭제된 상세로는 생성할 수 없다."""
    db = _world()
    payload = _create_payload(treatment_items=[_item(1100), _item(menu_detail_id)])
    _rejection(db, payload)


@pytest.mark.parametrize(
    ("foreign", "missing"),
    [
        ({"phonebook_id": 200}, {"phonebook_id": 999999}),
        ({"staff_user_id": 4}, {"staff_user_id": 999999}),
        ({"treatment_items": [_item(2100)]}, {"treatment_items": [_item(999999)]}),
    ],
    ids=["phonebook", "staff-user", "menu-detail"],
)
def test_other_shop_and_nonexistent_ids_get_identical_400(
    foreign: dict,
    missing: dict,
) -> None:
    """타 상점 id 와 없는 id 는 완전히 같은 응답을 받는다(존재 여부 오라클 없음)."""
    foreign_body = _rejection(_world(), _create_payload(**foreign))
    missing_body = _rejection(_world(), _create_payload(**missing))
    assert foreign_body == missing_body


@pytest.mark.parametrize(
    ("phonebook_id", "staff_user_id"),
    [(100, 1), (100, 3), (None, None)],
    ids=["shop-owner-as-staff", "shop-staff", "walk-in-unassigned"],
)
def test_create_with_same_shop_references_succeeds(
    phonebook_id: int | None,
    staff_user_id: int | None,
) -> None:
    """현재 상점 고객/멤버/상세만 참조하면 정상 생성된다."""
    db = _world()
    payload = _create_payload(
        phonebook_id=phonebook_id,
        staff_user_id=staff_user_id,
        customer_name="방문 고객",
    )

    result = _upsert(db, payload)

    assert isinstance(result, TreatmentSimpleResponse)
    assert db.commit_count == 1
    [treatment] = _added(db, Treatment)
    assert (treatment.shop_id, treatment.phonebook_id, treatment.staff_user_id) == (
        SHOP_A,
        phonebook_id,
        staff_user_id,
    )
    assert [i.menu_detail_id for i in _added(db, TreatmentItem)] == [1100]


# ---------------------------------------------------------------------------
# 수정: 바뀐 참조만 검증한다(기존 예약 편집 비파괴). 상세의 상점 소유권은 항상 강제.
# ---------------------------------------------------------------------------


def test_update_keeping_unchanged_references_succeeds() -> None:
    """이후 삭제된 고객/떠난 담당자/삭제된 상세를 그대로 유지하는 수정은 성공한다."""
    db = _world()

    _upsert(db, _update_payload(memo="메모만 수정"), treatment_id=500)

    assert db.commit_count == 1
    treatment = _get(db, Treatment, 500)
    assert treatment.memo == "메모만 수정"
    assert (treatment.phonebook_id, treatment.staff_user_id) == (101, 5)
    assert db.deleted == []  # 기존 항목(삭제된 상세 1101 포함) 유지
    assert _get(db, TreatmentItem, 5002).menu_detail_id == 1101


def test_update_resending_items_without_ids_keeps_soft_deleted_detail() -> None:
    """항목 id 없이 전체를 재전송하는 수정도 기존 참조(삭제된 상세)를 유지할 수 있다.

    kmcbeauty-native EditTreatmentModal 은 수정 시 treatment_items 를 id 없이 다시
    보낸다. 이 예약이 이미 참조 중인 상세는 신규 참조가 아니므로 삭제 검사를
    하지 않는다.
    """
    db = _world()
    payload = _update_payload(treatment_items=[_item(1100), _item(1101)])

    _upsert(db, payload, treatment_id=500)

    assert db.commit_count == 1
    assert sorted(i.menu_detail_id for i in _added(db, TreatmentItem)) == [1100, 1101]
    assert sorted(i.id for i in db.deleted) == [5001, 5002]  # 기존 항목은 교체됨


def test_update_changing_to_same_shop_references_succeeds() -> None:
    """현재 상점의 유효한 고객/멤버/상세로 바꾸는 수정은 성공한다."""
    db = _world()
    payload = _update_payload(
        phonebook_id=100,
        staff_user_id=3,
        treatment_items=[_item(1100, item_id=5001)],
    )

    _upsert(db, payload, treatment_id=500)

    assert db.commit_count == 1
    treatment = _get(db, Treatment, 500)
    assert (treatment.phonebook_id, treatment.staff_user_id) == (100, 3)
    assert [i.id for i in db.deleted] == [5002]


@pytest.mark.parametrize(
    "overrides",
    [
        {"phonebook_id": 200},
        {"phonebook_id": 999999},
        {"staff_user_id": 4},
        {"treatment_items": [_item(2100, item_id=5001), _item(1101, item_id=5002)]},
        {"treatment_items": [*UNCHANGED_500["treatment_items"], _item(2100)]},
        {"treatment_items": [*UNCHANGED_500["treatment_items"], _item(1103)]},
    ],
    ids=[
        "phonebook-to-other-shop",
        "phonebook-to-nonexistent",
        "staff-to-other-shop",
        "existing-item-to-other-shop-detail",
        "new-item-with-other-shop-detail",
        "new-item-with-soft-deleted-detail",
    ],
)
def test_update_changing_to_invalid_reference_is_rejected(overrides: dict) -> None:
    """수정으로 타 상점/없는/신규 삭제 참조를 넣으면 400 이고 예약은 그대로다."""
    db = _world()

    _rejection(db, _update_payload(**overrides), treatment_id=500)

    treatment = _get(db, Treatment, 500)
    assert (treatment.phonebook_id, treatment.staff_user_id) == (101, 5)


def test_update_rejects_preexisting_other_shop_detail_even_if_unchanged() -> None:
    """상세의 상점 소유권은 값이 그대로여도 항상 강제한다(취약 시절 데이터 포함)."""
    db = _world()
    payload = _update_payload(
        phonebook_id=100,
        staff_user_id=3,
        treatment_items=[_item(2100, item_id=6001)],
    )

    _rejection(db, payload, treatment_id=600)


# ---------------------------------------------------------------------------
# CRUD 계약: 상세 검증 쿼리는 TreatmentMenu 조인으로 상점을 스코핑한다
# ---------------------------------------------------------------------------


def _criteria_sql(query: FakeQuery) -> str:
    return " AND ".join(
        str(c.compile(compile_kwargs={"literal_binds": True})) for c in query.criteria
    )


def test_validate_menu_detail_exists_is_scoped_to_shop() -> None:
    """상세 조회는 treatment_menu 를 조인해 shop_id 와 삭제 상태로 필터한다."""
    db = FakeSession()
    treatment_crud.validate_menu_detail_exists(db, 1100, shop_id=SHOP_A)

    [query] = db.queries
    assert [table for table, _ in query.joins] == ["treatment_menu"]
    sql = _criteria_sql(query)
    assert "treatment_menu_detail.id = 1100" in sql
    assert "treatment_menu.shop_id = 10" in sql
    assert "treatment_menu_detail.deleted_at IS NULL" in sql
    assert "treatment_menu.deleted_at IS NULL" in sql


def test_validate_menu_detail_exists_ownership_only_mode_keeps_shop_filter() -> None:
    """삭제 검사를 끈 모드(기존 참조 유지)에서도 상점 필터는 빠지지 않는다."""
    db = FakeSession()
    treatment_crud.validate_menu_detail_exists(
        db,
        1101,
        shop_id=SHOP_A,
        exclude_deleted=False,
    )

    [query] = db.queries
    sql = _criteria_sql(query)
    assert "treatment_menu.shop_id = 10" in sql
    assert "deleted_at" not in sql
