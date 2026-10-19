"""회원 강제 삭제(DELETE /users/me/force) 연쇄 삭제 교정 테스트.

대상: app/models/*.py 의 관계 옵션(passive_deletes / cascade)

버그: 유저 하드 삭제가 ORM 관계를 따라 user → shops → phonebooks/treatments/items/
menus … 를 전부 로딩하고 행 단위로 지웠다(무거운 N+1). 게다가 TreatmentMenu.shop 의
backref "treatment_menus" 에는 delete cascade 가 없어서, 샵을 지우면 ORM 이
UPDATE treatment_menu SET shop_id=NULL 을 내보냈다. shop_id 는 NOT NULL 이므로
IntegrityError → 500 이었다(소프트삭제된 메뉴도 해당).

요구 동작: ON DELETE CASCADE / SET NULL FK 가 있는 관계는 부모 쪽에
passive_deletes=True 를 둔다. 그러면 ORM 은 부모 DELETE 만 내보내고 나머지는 DB 가
연쇄 처리한다. FK 에 ondelete 가 없는 관계(shop.user_id, treatment.phonebook_id)는
ORM 이 계속 직접 처리한다.

DB 없이 두 층위로 확인한다.
- 관계 메타데이터: 부모 삭제 시 NOT NULL FK 를 NULL 로 바꾸는 관계가 없어야 하고,
  passive 관계는 베이스라인 마이그레이션 FK 의 ondelete 와 맞아야 한다. 베이스라인은
  alembic op 대역으로 upgrade() 를 실행해 MetaData 로 재구성한다. 새 마이그레이션이
  FK ondelete 를 바꾸면 이 검증 기준도 함께 갱신한다.
- flush 계획: 바인드 없는 Session 에 영속 상태 객체를 붙이고, persistence 의
  save_obj/delete_obj 를 기록기로 바꿔 SQL 실행 없이 쓰기 계획을 관찰한다. ORM 이
  자식 컬렉션을 지연 로딩하려 하면(SQL 필요) 바인드가 없어 즉시 드러난다.
"""

import importlib.util
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.exc import UnboundExecutionError
from sqlalchemy.orm import (
    RelationshipDirection,
    RelationshipProperty,
    Session,
    make_transient_to_detached,
    persistence,
)
from sqlalchemy.orm.attributes import set_committed_value

# 문자열 관계 참조("User", "Shop" 등)가 해석되도록 매퍼 구성 전에 전체 모델을 로드한다.
import app.models  # noqa: F401
from app.models.base import Base
from app.models.phonebook import Phonebook
from app.models.shop import Shop
from app.models.treatment_menu import TreatmentMenu
from app.models.user import User

BASELINE = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "5506c8cf9e2f_initial_schema_postgresql.py"
)

# ---------------------------------------------------------------------------
# 베이스라인 마이그레이션의 FK ondelete (DB 실제 동작의 기준)
# ---------------------------------------------------------------------------


class _AlembicOpStub:
    """alembic op 대역. create_table 인자를 MetaData 위의 Table 로 재구성한다."""

    def __init__(self) -> None:
        self.metadata = sa.MetaData()

    def create_table(self, name: str, *elements: object, **kwargs: object) -> None:
        sa.Table(name, self.metadata, *elements, **kwargs)

    def create_index(self, *_args: object, **_kwargs: object) -> None:
        """인덱스는 FK 검증과 무관하다."""

    @staticmethod
    def f(name: str) -> str:
        return name


@pytest.fixture(scope="module")
def baseline_ondelete() -> dict[tuple[str, str], str | None]:
    """(FK 테이블, FK 컬럼) → 베이스라인 마이그레이션의 ondelete."""
    spec = importlib.util.spec_from_file_location("baseline_migration", BASELINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = _AlembicOpStub()
    module.op = stub  # sys.modules 에 없는 사본 모듈이라 전역 교체가 새지 않는다
    module.upgrade()
    return {
        (fk.parent.table.name, fk.parent.name): fk.ondelete
        for table in stub.metadata.tables.values()
        for fk in table.foreign_keys
    }


def _one_to_many() -> list[RelationshipProperty]:
    return [
        rel
        for mapper in Base.registry.mappers
        for rel in mapper.relationships
        if rel.direction is RelationshipDirection.ONETOMANY
    ]


def _name(rel: RelationshipProperty) -> str:
    return f"{rel.parent.class_.__name__}.{rel.key}"


def _fk_columns(rel: RelationshipProperty) -> list[sa.Column]:
    """1:N 관계에서 ORM 이 채우거나 NULL 로 비우는 자식 쪽 FK 컬럼.

    remote_side 는 primaryjoin 의 추가 조건 컬럼(예: details 의 deleted_at)까지
    포함하므로 FK 동기화 쌍(synchronize_pairs)을 쓴다.
    """
    return [child for _parent, child in rel.synchronize_pairs]


def _fk_keys(rel: RelationshipProperty) -> list[tuple[str, str]]:
    return sorted((col.table.name, col.name) for col in _fk_columns(rel))


def test_parent_delete_never_nulls_a_not_null_fk() -> None:
    """부모 삭제 시 NOT NULL FK 를 NULL 로 바꾸는 1:N 관계가 없어야 한다.

    delete cascade 가 없는 1:N 관계는 부모 삭제 시 자식 FK 를 NULL 로 UPDATE 한다.
    그 FK 가 NOT NULL 이면 IntegrityError(500)다.
    """
    offenders = [
        f"{_name(rel)} -> {col.table.name}.{col.name}"
        for rel in _one_to_many()
        if not rel.cascade.delete and rel.passive_deletes != "all"
        for col in _fk_columns(rel)
        if not col.nullable
    ]

    assert offenders == []


def test_shop_children_are_deleted_by_db_cascade(
    baseline_ondelete: dict[tuple[str, str], str | None],
) -> None:
    """샵의 모든 자식 관계는 DB 의 ON DELETE CASCADE 에 맡긴다(로딩 없음)."""
    for rel in inspect(Shop).relationships:
        if rel.direction is not RelationshipDirection.ONETOMANY:
            continue
        assert rel.cascade.delete, _name(rel)
        assert rel.passive_deletes is True, _name(rel)
        for fk in _fk_keys(rel):
            assert baseline_ondelete[fk] == "CASCADE", (_name(rel), fk)


def test_passive_relationships_match_baseline_ondelete(
    baseline_ondelete: dict[tuple[str, str], str | None],
) -> None:
    """모든 passive 관계는 DB FK 가 실제로 처리한다.

    삭제 cascade 관계는 ON DELETE CASCADE, 아니면 ON DELETE SET NULL 이어야 한다.
    """
    passive = [rel for rel in _one_to_many() if rel.passive_deletes]

    assert passive  # 비어 있으면 검증이 공허하다
    for rel in passive:
        expected = "CASCADE" if rel.cascade.delete else "SET NULL"
        for fk in _fk_keys(rel):
            assert baseline_ondelete[fk] == expected, (_name(rel), fk)


@pytest.mark.parametrize(
    ("rel", "fk"),
    [
        (User.shops, ("shop", "user_id")),
        (Phonebook.treatments, ("treatment", "phonebook_id")),
    ],
    ids=["user-shops", "phonebook-treatments"],
)
def test_relationships_without_db_ondelete_stay_orm_managed(
    baseline_ondelete: dict[tuple[str, str], str | None],
    rel: object,
    fk: tuple[str, str],
) -> None:
    """베이스라인 FK 에 ondelete 가 없으면 passive 로 두면 안 된다(FK 위반)."""
    assert baseline_ondelete[fk] is None
    assert rel.property.passive_deletes is False


# ---------------------------------------------------------------------------
# flush 계획: 바인드 없는 세션에서 내보낼 쓰기를 기록한다(SQL 미실행)
# ---------------------------------------------------------------------------

PlannedWrite = tuple[str, str, tuple | None, dict]
SOFT_DELETED_AT = datetime(2026, 9, 1, tzinfo=UTC)
_DUMMY_HASH = "not-a-real-hash"


def _ordered(states: list) -> list:
    """같은 매퍼 안의 행 순서를 PK 로 고정한다(실제 persistence 도 PK 로 정렬)."""
    return sorted(states, key=lambda state: state.identity or ())


@pytest.fixture
def planned_writes(monkeypatch: pytest.MonkeyPatch) -> list[PlannedWrite]:
    """UOW 의 INSERT/UPDATE/DELETE 진입점을 기록기로 바꾼다."""
    writes: list[PlannedWrite] = []

    def record_save(
        _base_mapper: object,
        states: list,
        _uow: object,
        single: bool = False,  # noqa: ARG001  # 원 시그니처 유지
    ) -> None:
        for state in _ordered(states):
            changes = {key: state.dict.get(key) for key in state.committed_state}
            op = "UPDATE" if state.has_identity else "INSERT"
            writes.append((op, state.mapper.local_table.name, state.identity, changes))

    def record_delete(_base_mapper: object, states: list, _uow: object) -> None:
        for state in _ordered(states):
            writes.append(("DELETE", state.mapper.local_table.name, state.identity, {}))

    monkeypatch.setattr(persistence, "save_obj", record_save)
    monkeypatch.setattr(persistence, "delete_obj", record_delete)
    return writes


def _persistent(session: Session, obj: object) -> object:
    """DB 에서 읽어 온 것처럼 영속 상태로 붙인다(미설정 속성은 미로딩)."""
    make_transient_to_detached(obj)
    session.add(obj)
    return obj


def _delete_and_flush(build: Callable[[Session], object]) -> None:
    session = Session()  # 바인드 없음: SQL 이 필요한 순간 UnboundExecutionError
    try:
        session.delete(build(session))
        session.flush()
    except UnboundExecutionError as e:
        pytest.fail(f"삭제 중 ORM 이 자식 행을 로딩하려 했다(N+1): {e}")


def _owner(session: Session) -> User:
    """샵 하나를 가진 원장.

    shop.user_id 는 ondelete 가 없어 ORM 이 샵을 먼저 지워야 하므로, 샵 목록 SELECT
    한 번은 정상 경로다(여기서는 이미 로딩된 것으로 둔다).
    """
    user = _persistent(
        session,
        User(id=1, email="owner@test.com", name="원장", password=_DUMMY_HASH),
    )
    shop = _persistent(session, Shop(id=10, user_id=1, name="샵", address="주소"))
    set_committed_value(user, "shops", [shop])
    return user


def test_force_delete_owner_emits_only_parent_deletes(
    planned_writes: list[PlannedWrite],
) -> None:
    """원장 하드 삭제는 DELETE shop → DELETE users 만 내보내고 자식은 DB 가 지운다."""
    _delete_and_flush(_owner)

    assert planned_writes == [
        ("DELETE", "shop", (10,), {}),
        ("DELETE", "users", (1,), {}),
    ]


def test_force_delete_member_without_shops_emits_only_user_delete(
    planned_writes: list[PlannedWrite],
) -> None:
    """샵 없는 직원 삭제는 담당/생성 시술을 로딩·UPDATE 하지 않는다(DB 가 SET NULL)."""

    def manager(session: Session) -> User:
        user = _persistent(
            session,
            User(id=3, email="mgr@test.com", name="매니저", password=_DUMMY_HASH),
        )
        set_committed_value(user, "shops", [])
        return user

    _delete_and_flush(manager)

    assert planned_writes == [("DELETE", "users", (3,), {})]


def test_shop_delete_never_nulls_loaded_menu_shop_id(
    planned_writes: list[PlannedWrite],
) -> None:
    """메뉴가 세션에 로딩돼 있어도 shop_id 를 NULL 로 UPDATE 하지 않는다.

    소프트삭제된 메뉴도 같다. 다른 자식 컬렉션도 로딩된 상태로 두어, 지연 로딩 없이
    메뉴 처리만 관찰한다.
    """

    def shop_with_loaded_menus(session: Session) -> Shop:
        shop = _persistent(session, Shop(id=10, user_id=1, name="샵", address="주소"))
        menus = [
            _persistent(session, TreatmentMenu(id=100, shop_id=10, name="커트")),
            _persistent(
                session,
                TreatmentMenu(
                    id=101,
                    shop_id=10,
                    name="펌",
                    deleted_at=SOFT_DELETED_AT,
                ),
            ),
        ]
        for key in ("phonebook_list", "treatments", "invites"):
            set_committed_value(shop, key, [])
        set_committed_value(shop, "treatment_menus", menus)
        return shop

    _delete_and_flush(shop_with_loaded_menus)

    assert [w for w in planned_writes if w[0] != "DELETE"] == []
    assert planned_writes == [
        ("DELETE", "treatment_menu", (100,), {}),
        ("DELETE", "treatment_menu", (101,), {}),
        ("DELETE", "shop", (10,), {}),
    ]
