"""마지막 대표원장 보호 테스트 (보안).

사용자 결정: 샵에는 활성 대표원장이 1명 이상 남아야 한다. 대표원장 연결을 해제하거나
대표원장에서 빼는(1→0) 요청이 샵에 다른 활성 대표원장을 남기지 않으면 409 다.

대상:
- app/services/shop_user_service.py::delete_shop_user_service ·
  update_shop_user_service
- app/crud/shop_user_crud.py::lock_primary_owners · count_other_primary_owners

버그: 대표원장은 자기 자신을 포함한 어떤 연결도 해제 · 강등할 수 있었다. 마지막
대표원장이 스스로를 빼면 직원 · 초대를 관리할 사람이 없어지고, DB 를 손으로 고쳐야만
복구된다.

요구 동작:
- 다른 활성 대표원장이 없으면 해제 · 강등은 409 이고 아무것도 바뀌지 않는다.
- 탈퇴(소프트삭제)한 대표원장 · 다른 샵의 대표원장은 "다른 대표원장"으로 세지 않는다.
- 동시에 들어온 두 강등이 둘 다 통과하지 않게 대표원장 행을 잠근(FOR UPDATE) 뒤 센다.
- 승격과 대표원장이 아닌 연결의 해제 · 수정은 그대로다.
- API 문서(app/api/shop.py)의 PUT · DELETE /shops/{shop_id}/users/{user_id} 에 409 가
  있다.

DB 없이 실제 서비스 · CRUD 를 실행한다(tests/fake_sql.py). 잠금 · 집계 순서는 실행된
SELECT 문을 PostgreSQL 방언으로 컴파일해 확인한다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, status
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.api import shop as shop_api
from app.docs.common_responses import COMMON_ERROR_RESPONSES
from app.enum.role import UserRole
from app.exceptions import CustomException
from app.models.shop_user import ShopUser
from app.models.user import User
from app.schemas.shop_user import ShopUserAssociateUpdateRequest
from app.services import shop_user_service
from tests.fake_sql import FakeSQLSession

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
SHOP_ID, OTHER_SHOP_ID = 10, 20
# LEFT_OWNER = 탈퇴한(소프트삭제) 대표원장, OTHER_OWNER = 다른 샵의 대표원장
OWNER_ID, CO_OWNER_ID, MEMBER_ID, LEFT_OWNER_ID, OTHER_OWNER_ID = 1, 2, 3, 4, 5
LAST_OWNER_DETAIL = (
    "마지막 대표원장은 연결을 해제하거나 대표원장에서 뺄 수 없습니다. "
    "다른 대표원장을 먼저 지정하세요."
)
_DUMMY_HASH = "not-a-real-hash"

# 대상 user_id 의 연결을 바꾸는 요청(요청자 user_id 는 키워드로 바꿀 수 있다).
Change = Callable[..., None]


def _user(user_id: int, deleted_at: datetime | None = None) -> User:
    return User(
        id=user_id,
        email=f"user{user_id}@test.com",
        name=f"유저{user_id}",
        password=_DUMMY_HASH,
        role=UserRole.MASTER,
        deleted_at=deleted_at,
        created_at=NOW,
        updated_at=NOW,
    )


def _mapping(row_id: int, shop_id: int, user: User, is_primary_owner: int) -> ShopUser:
    # user 관계를 채워 둔다(수정 응답 ShopUserUserResponse 직렬화용).
    return ShopUser(
        id=row_id,
        shop_id=shop_id,
        user_id=user.id,
        is_primary_owner=is_primary_owner,
        user=user,
    )


def _world(*, co_owner: bool = False) -> FakeSQLSession:
    """활성 대표원장 OWNER 와 직원 MEMBER 가 있는 샵.

    "다른 대표원장"으로 세면 안 되는 행도 둔다: 탈퇴한 대표원장(매핑은 이력으로
    남는다)과 다른 샵의 대표원장. co_owner 면 활성 대표원장 CO_OWNER 가 하나 더 있다.
    """
    owner, member = _user(OWNER_ID), _user(MEMBER_ID)
    left_owner = _user(LEFT_OWNER_ID, deleted_at=NOW)
    other_owner = _user(OTHER_OWNER_ID)
    rows: list[object] = [
        owner,
        member,
        left_owner,
        other_owner,
        _mapping(1, SHOP_ID, owner, 1),
        _mapping(3, SHOP_ID, member, 0),
        _mapping(4, SHOP_ID, left_owner, 1),
        _mapping(5, OTHER_SHOP_ID, other_owner, 1),
    ]
    if co_owner:
        co = _user(CO_OWNER_ID)
        rows += [co, _mapping(2, SHOP_ID, co, 1)]
    return FakeSQLSession(*rows)


def _owner_flag(db: FakeSQLSession, user_id: int) -> int | None:
    """샵 연결의 대표원장 여부(연결이 없으면 None)."""
    return next(
        (
            row.is_primary_owner
            for row in db.rows("shop_user")
            if row.shop_id == SHOP_ID and row.user_id == user_id
        ),
        None,
    )


def _leave(db: FakeSQLSession, user_id: int, requester: int = OWNER_ID) -> None:
    """DELETE /shops/{shop_id}/users/{user_id} — 연결 해제."""
    shop_user_service.delete_shop_user_service(
        db=db,
        shop_id=SHOP_ID,
        user_id=user_id,
        current_user=SimpleNamespace(id=requester),
    )


def _update(
    db: FakeSQLSession,
    user_id: int,
    is_primary_owner: int,
    requester: int,
) -> None:
    shop_user_service.update_shop_user_service(
        db=db,
        shop_id=SHOP_ID,
        user_id=user_id,
        current_user=SimpleNamespace(id=requester),
        payload=ShopUserAssociateUpdateRequest(is_primary_owner=is_primary_owner),
    )


def _step_down(db: FakeSQLSession, user_id: int, requester: int = OWNER_ID) -> None:
    """PUT /shops/{shop_id}/users/{user_id} (is_primary_owner=0) — 대표원장 빼기."""
    _update(db, user_id, 0, requester)


def _promote(db: FakeSQLSession, user_id: int, requester: int = OWNER_ID) -> None:
    """PUT /shops/{shop_id}/users/{user_id} (is_primary_owner=1) — 대표원장 지정."""
    _update(db, user_id, 1, requester)


# 대표원장을 빼는 두 요청과, 성공했을 때 대상의 대표원장 여부(None = 연결 없음).
REMOVALS = [
    pytest.param(_leave, None, id="leave"),
    pytest.param(_step_down, 0, id="step-down"),
]


# ---------------------------------------------------------------------------
# 마지막 대표원장: 해제 · 강등은 409 이고 아무것도 바뀌지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("remove", "_after"), REMOVALS)
def test_last_owner_cannot_remove_own_ownership(
    remove: Change,
    _after: int | None,
) -> None:
    """마지막 활성 대표원장의 자기 해제 · 강등은 409 다(삭제 · 커밋 없음).

    탈퇴한 대표원장(매핑은 남아 있다)과 다른 샵의 대표원장은 다른 대표원장이 아니다.
    """
    db = _world()

    with pytest.raises(CustomException) as exc:
        remove(db, OWNER_ID)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "SHOP_USER_CONFLICT"
    assert exc.value.detail["detail"] == LAST_OWNER_DETAIL
    assert db.commit_count == 0
    assert db.deleted == []
    assert _owner_flag(db, OWNER_ID) == 1


@pytest.mark.parametrize(("remove", "after"), REMOVALS)
def test_owner_may_remove_own_ownership_while_another_owner_remains(
    remove: Change,
    after: int | None,
) -> None:
    """다른 활성 대표원장이 있으면 자기 해제 · 강등은 그대로 커밋된다."""
    db = _world(co_owner=True)

    remove(db, OWNER_ID)

    assert db.commit_count == 1
    assert _owner_flag(db, OWNER_ID) == after
    assert _owner_flag(db, CO_OWNER_ID) == 1


def test_second_owner_cannot_step_down_after_the_first() -> None:
    """두 대표원장이 스스로 강등하면 먼저 커밋된 쪽만 성공하고 다음은 409 다.

    동시 요청은 대표원장 행 잠금에서 이 순서로 직렬화된다. 기다린 쪽은 앞 트랜잭션이
    커밋된 뒤의 상태로 센다(PostgreSQL READ COMMITTED).
    """
    db = _world(co_owner=True)
    _step_down(db, OWNER_ID)

    with pytest.raises(CustomException) as exc:
        _step_down(db, CO_OWNER_ID, requester=CO_OWNER_ID)

    assert exc.value.status_code == 409
    assert db.commit_count == 1
    assert (_owner_flag(db, OWNER_ID), _owner_flag(db, CO_OWNER_ID)) == (0, 1)


# ---------------------------------------------------------------------------
# 동시성: 대표원장 행을 잠근 뒤에 센다
# ---------------------------------------------------------------------------


def _compiled(db: FakeSQLSession) -> list[str]:
    """실행된 SELECT 문(PostgreSQL, 값 인라인)을 실행 순서대로."""
    return [
        " ".join(
            str(
                stmt.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                ),
            ).split(),
        )
        for stmt in db.statements
    ]


@pytest.mark.parametrize(("remove", "_after"), REMOVALS)
def test_owner_rows_are_locked_before_other_owners_are_counted(
    remove: Change,
    _after: int | None,
) -> None:
    """남은 대표원장을 세기 전에 그 샵의 대표원장 행을 잠근다(SELECT ... FOR UPDATE).

    잠금 없이 세면 동시에 스스로 강등하는 두 대표원장이 서로를 "다른 대표원장"으로
    보고 둘 다 통과해 대표원장이 0명이 된다.
    """
    db = _world(co_owner=True)

    remove(db, OWNER_ID)

    sqls = _compiled(db)
    locks = [i for i, sql in enumerate(sqls) if sql.endswith("FOR UPDATE")]
    counts = [i for i, sql in enumerate(sqls) if sql.startswith("SELECT count(")]
    assert len(locks) == 1, sqls
    assert len(counts) == 1, sqls
    assert locks[0] < counts[0], sqls
    lock = sqls[locks[0]]
    assert "FROM shop_user" in lock
    assert "shop_user.shop_id = 10" in lock
    assert "shop_user.is_primary_owner = 1" in lock


# ---------------------------------------------------------------------------
# 대조군: 승격 · 대표원장이 아닌 연결의 변경은 그대로다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "user_id", "after"),
    [
        pytest.param(_leave, MEMBER_ID, None, id="remove-member"),
        pytest.param(_step_down, MEMBER_ID, 0, id="member-stays-member"),
        pytest.param(_promote, MEMBER_ID, 1, id="promote-member"),
        pytest.param(_promote, OWNER_ID, 1, id="owner-stays-owner"),
    ],
)
def test_member_changes_and_promotions_are_unaffected(
    change: Change,
    user_id: int,
    after: int | None,
) -> None:
    """마지막 대표원장이 있는 샵이어도 직원 연결 변경 · 승격은 그대로 커밋된다."""
    db = _world()

    change(db, user_id)

    assert db.commit_count == 1
    assert _owner_flag(db, user_id) == after
    assert _owner_flag(db, OWNER_ID) == 1


# ---------------------------------------------------------------------------
# API 문서: 직원 연결 수정 · 해제의 409
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["put", "delete"])
def test_last_owner_conflict_is_documented(method: str) -> None:
    """PUT · DELETE /shops/{shop_id}/users/{user_id} 문서에 409 응답이 있다."""
    api = FastAPI()
    api.include_router(shop_api.router)

    operation = api.openapi()["paths"]["/shops/{shop_id}/users/{user_id}"][method]

    conflict = COMMON_ERROR_RESPONSES[status.HTTP_409_CONFLICT]
    assert operation["responses"]["409"]["description"] == conflict["description"]
