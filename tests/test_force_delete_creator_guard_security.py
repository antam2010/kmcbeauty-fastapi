"""생성자 강제 탈퇴로 다른 멤버가 쓰는 샵이 지워지는 것 차단 테스트 (보안).

대상:
- app/services/user_service.py::delete_user_service (강제 DELETE /users/me/force)
- app/crud/shop_crud.py::get_created_shop_ids
  (기존 lock_primary_owners · count_other_primary_owners · count_other_active_members)
- app/api/user.py 의 강제 탈퇴 문서(설명 · 409)

버그: 강제 탈퇴는 db.delete(user) 이고, ORM cascade(User.shops, "all, delete-orphan")가
내가 만든 샵(Shop.user_id)을 모두 지운다. DB cascade 는 그 샵의 고객 · 예약 · 메뉴 ·
멤버십 · 초대 · 기기 토큰까지 지운다. 탈퇴 가드는 내가 대표원장인 샵만 봐서, 다음
경우에 다른 멤버가 쓰던 샵이 데이터째 사라졌다: (a) 다른 활성 대표원장이 있다,
(b) 생성자가 직원으로 강등됐다, (c) 다른 대표원장이 생성자의 멤버십을 해제했다
(해제 · 강등에는 생성자 보호가 없다).

요구 동작(사용자 결정, 2026-11-10):
- 강제 탈퇴만: 내가 만든 샵 중 하나라도 다른 활성 멤버(역할 무관, 탈퇴 유저 제외)가
  있으면 409(USER_CONFLICT)이고 아무것도 지우지 않는다(롤백).
- 다른 활성 멤버가 없는 샵은 지금처럼 계정과 함께 지운다. 소프트 탈퇴는 그대로다.
- 대표원장인 샵과 만든 샵을 합쳐 shop_id 오름차순으로 한 번 돌고, 샵마다 대표원장
  행을 잠근 뒤에 센다. 대표원장인 샵이면 마지막 대표원장 규칙(기존 문구)을 먼저 본다.

DB · Redis 없이 실제 서비스 · CRUD 를 실행한다(tests/fake_sql.py). 가짜 세션은 cascade
를 흉내 내지 않으므로 "삭제 없음"은 db.deleted 와 커밋 · 롤백 수로 본다. 커밋 뒤 세션
정리(Redis)는 호출만 기록하는 스텁이다.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.api import user as user_api
from app.enum.role import UserRole
from app.exceptions import CustomException
from app.models.shop import Shop
from app.models.shop_user import ShopUser
from app.models.user import User
from app.services import user_service
from tests.fake_sql import FakeSQLSession

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
SHOP_ID, OTHER_SHOP_ID = 10, 20
# OWNER = 강제 탈퇴하는 유저(샵 생성자), LEFT = 이미 탈퇴한(소프트삭제) 유저
OWNER_ID, CO_OWNER_ID, STAFF_ID, LEFT_ID = 1, 2, 3, 4
CREATOR_DETAIL = (
    "다른 멤버가 있는 샵을 만든 사용자는 강제 탈퇴할 수 없습니다. "
    "일반 탈퇴를 이용하세요."
)
LAST_OWNER_DETAIL = (
    "직원이 있는 샵의 마지막 대표원장은 탈퇴할 수 없습니다. "
    "다른 대표원장을 먼저 지정하세요."
)
FORCE_DELETE_DESCRIPTION = (
    "현재 로그인한 사용자를 강제로 삭제합니다. "
    "내가 만든 샵과 그 데이터(예약 · 고객 · 메뉴 등)도 함께 지워집니다. "
    "내가 만든 샵에 다른 활성 멤버가 있으면 409(USER_CONFLICT)이며, "
    "이때는 일반 탈퇴(DELETE /users/me)를 이용합니다."
)
_DUMMY_HASH = "not-a-real-hash"

# 멤버십 한 건: 샵 ID, 유저 ID, 대표원장 여부(1/0) 순서.
Membership = tuple[int, int, int]


def _user(user_id: int) -> User:
    return User(
        id=user_id,
        email=f"user{user_id}@test.com",
        name=f"유저{user_id}",
        password=_DUMMY_HASH,
        role=UserRole.MASTER if user_id == OWNER_ID else UserRole.MANAGER,
        deleted_at=NOW if user_id == LEFT_ID else None,
        created_at=NOW,
        updated_at=NOW,
    )


def _world(
    *memberships: Membership,
    created: tuple[int, ...] = (SHOP_ID,),
    shop_deleted_at: datetime | None = None,
) -> FakeSQLSession:
    """OWNER 가 만든 샵(created)과 멤버십에 나오는 유저들(LEFT 는 탈퇴 상태).

    created 에 없는 샵은 Shop 행이 없다(OWNER 가 대표원장이어도 생성자가 아니다).
    """
    user_ids = {OWNER_ID} | {user_id for _, user_id, _ in memberships}
    return FakeSQLSession(
        *(_user(user_id) for user_id in sorted(user_ids)),
        *(
            Shop(
                id=shop_id,
                user_id=OWNER_ID,
                name=f"샵{shop_id}",
                address="서울시 강남구",
                deleted_at=shop_deleted_at,
            )
            for shop_id in created
        ),
        *(
            ShopUser(id=row_id, shop_id=shop, user_id=user, is_primary_owner=owner)
            for row_id, (shop, user, owner) in enumerate(memberships, start=1)
        ),
    )


@pytest.fixture
def cleared(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """커밋 뒤 세션 정리(유저 캐시 · 리프레시 토큰 삭제) 호출을 기록한다."""
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        user_service,
        "clear_user_redis",
        lambda user_id: calls.append(("user", user_id)),
    )
    monkeypatch.setattr(
        user_service,
        "clear_refresh_token_redis",
        lambda user_id: calls.append(("refresh", user_id)),
    )
    return calls


def _delete_account(db: FakeSQLSession, *, soft: bool) -> None:
    user_service.delete_user_service(
        db=db,
        current_user=SimpleNamespace(id=OWNER_ID),
        is_soft_delete=soft,
    )


def _owner_deleted(db: FakeSQLSession) -> bool:
    """OWNER 가 지워졌는가(소프트: deleted_at, 강제: 행 삭제)."""
    owner = next((user for user in db.rows("users") if user.id == OWNER_ID), None)
    return owner is None or owner.deleted_at is not None


def _sqls(db: FakeSQLSession) -> list[str]:
    """실행한 문의 PostgreSQL SQL(바인드 값 포함, 공백 정규화)."""
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


# 내가 만든 샵에 다른 활성 멤버가 있는 경우(사용자 결정의 a · b · c).
CREATOR_WITH_OTHER_MEMBERS = [
    pytest.param(
        lambda: _world((SHOP_ID, OWNER_ID, 1), (SHOP_ID, CO_OWNER_ID, 1)),
        id="another-active-owner",
    ),
    pytest.param(
        lambda: _world((SHOP_ID, OWNER_ID, 0), (SHOP_ID, CO_OWNER_ID, 1)),
        id="creator-demoted",
    ),
    pytest.param(
        lambda: _world((SHOP_ID, CO_OWNER_ID, 1), (SHOP_ID, STAFF_ID, 0)),
        id="creator-unlinked",
    ),
]
BLOCKED_WORLDS = [
    *CREATOR_WITH_OTHER_MEMBERS,
    pytest.param(
        lambda: _world(
            (SHOP_ID, OWNER_ID, 1),
            (OTHER_SHOP_ID, OWNER_ID, 1),
            (OTHER_SHOP_ID, CO_OWNER_ID, 1),
            created=(SHOP_ID, OTHER_SHOP_ID),
        ),
        id="second-created-shop-blocks",
    ),
    # 소프트삭제된 샵도 ORM cascade 로 지워지므로 똑같이 막는다.
    pytest.param(
        lambda: _world((SHOP_ID, CO_OWNER_ID, 1), shop_deleted_at=NOW),
        id="soft-deleted-created-shop",
    ),
]


# ---------------------------------------------------------------------------
# 강제 탈퇴: 만든 샵에 다른 활성 멤버가 있으면 409, 삭제 · 커밋 없음
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("world", BLOCKED_WORLDS)
def test_creator_cannot_force_delete_while_created_shop_has_other_members(
    cleared: list[tuple[str, int]],
    world: Callable[[], FakeSQLSession],
) -> None:
    """만든 샵에 다른 활성 멤버가 있으면 강제 탈퇴는 409 이고 아무것도 바뀌지 않는다.

    대표원장 · 강등 · 해제 여부와 상관없다. 롤백 경로를 타고(잠금 해제) 세션도 그대로다.
    """
    db = world()

    with pytest.raises(CustomException) as exc:
        _delete_account(db, soft=False)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "USER_CONFLICT"
    assert exc.value.detail["detail"] == CREATOR_DETAIL
    assert not _owner_deleted(db)
    assert db.deleted == []
    assert (db.commit_count, db.rollback_count) == (0, 1)
    assert cleared == []


def test_last_owner_rule_comes_first_in_created_shop(
    cleared: list[tuple[str, int]],
) -> None:
    """만든 샵의 마지막 대표원장이면 기존 마지막 대표원장 409(문구)가 먼저다."""
    db = _world((SHOP_ID, OWNER_ID, 1), (SHOP_ID, STAFF_ID, 0))

    with pytest.raises(CustomException) as exc:
        _delete_account(db, soft=False)

    assert exc.value.status_code == 409
    assert exc.value.detail["detail"] == LAST_OWNER_DETAIL
    assert (db.commit_count, db.rollback_count) == (0, 1)
    assert cleared == []


# ---------------------------------------------------------------------------
# 막지 않는 경우: 다른 활성 멤버가 없는 만든 샵(강제) · 소프트 탈퇴
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "world",
    [
        pytest.param(lambda: _world((SHOP_ID, OWNER_ID, 1)), id="sole-member"),
        pytest.param(
            lambda: _world((SHOP_ID, OWNER_ID, 1), (SHOP_ID, LEFT_ID, 0)),
            id="only-other-member-left",
        ),
    ],
)
def test_force_delete_proceeds_when_created_shops_have_no_other_active_member(
    cleared: list[tuple[str, int]],
    world: Callable[[], FakeSQLSession],
) -> None:
    """다른 활성 멤버가 없는 만든 샵은 지금처럼 계정과 함께 지운다(강제 탈퇴 커밋)."""
    db = world()

    _delete_account(db, soft=False)

    assert _owner_deleted(db)
    assert db.commit_count == 1
    assert cleared == [("user", OWNER_ID), ("refresh", OWNER_ID)]


@pytest.mark.parametrize("world", BLOCKED_WORLDS)
def test_soft_delete_of_creator_is_unchanged(
    cleared: list[tuple[str, int]],
    world: Callable[[], FakeSQLSession],
) -> None:
    """대조군: 강제 탈퇴를 막는 같은 상황에서도 소프트 탈퇴는 그대로 커밋된다.

    소프트 탈퇴는 샵을 지우지 않으므로 생성자 규칙을 보지 않는다.
    """
    db = world()

    _delete_account(db, soft=True)

    assert _owner_deleted(db)
    assert db.commit_count == 1
    assert cleared == [("user", OWNER_ID), ("refresh", OWNER_ID)]


# ---------------------------------------------------------------------------
# 동시성: 만든 샵도 대표원장 행을 잠근 뒤에 세고, 샵 ID 오름차순으로 잠근다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("world", CREATOR_WITH_OTHER_MEMBERS)
@pytest.mark.usefixtures("cleared")
def test_created_shop_owner_rows_are_locked_before_member_count(
    world: Callable[[], FakeSQLSession],
) -> None:
    """만든 샵의 대표원장 행을 잠근 뒤(FOR UPDATE)에 다른 활성 멤버를 센다.

    생성자가 멤버가 아닐 때(해제)도 같다. 초대 가입도 멤버를 넣기 전에 같은 잠금을
    잡으므로, 잠그지 않고 세면 동시에 가입한 멤버의 샵이 지워질 수 있다.
    """
    db = world()

    with pytest.raises(CustomException):
        _delete_account(db, soft=False)

    sqls = _sqls(db)
    locks = [i for i, sql in enumerate(sqls) if sql.endswith("FOR UPDATE")]
    member_counts = [
        i
        for i, sql in enumerate(sqls)
        if sql.startswith("SELECT count(") and "is_primary_owner" not in sql
    ]
    assert len(locks) == 1, sqls
    assert "shop_user.shop_id = 10" in sqls[locks[0]], sqls
    assert len(member_counts) == 1, sqls
    assert locks[0] < member_counts[0], sqls


@pytest.mark.parametrize(
    "world",
    [
        # 10 은 만든 샵(생성자 강등, 대표원장은 탈퇴), 20 은 대표원장인 샵
        pytest.param(
            lambda: _world(
                (SHOP_ID, OWNER_ID, 0),
                (SHOP_ID, LEFT_ID, 1),
                (OTHER_SHOP_ID, OWNER_ID, 1),
                created=(SHOP_ID,),
            ),
            id="created-10-owned-20",
        ),
        # 10 은 대표원장인 샵, 20 은 만든 샵(생성자 강등, 대표원장은 탈퇴)
        pytest.param(
            lambda: _world(
                (SHOP_ID, OWNER_ID, 1),
                (OTHER_SHOP_ID, OWNER_ID, 0),
                (OTHER_SHOP_ID, LEFT_ID, 1),
                created=(OTHER_SHOP_ID,),
            ),
            id="owned-10-created-20",
        ),
        # 두 샵 모두 대표원장이자 생성자: 합집합이라 샵마다 한 번만 잠근다
        pytest.param(
            lambda: _world(
                (SHOP_ID, OWNER_ID, 1),
                (OTHER_SHOP_ID, OWNER_ID, 1),
                created=(OTHER_SHOP_ID, SHOP_ID),
            ),
            id="both-sets-locked-once",
        ),
    ],
)
@pytest.mark.usefixtures("cleared")
def test_force_delete_locks_shops_in_ascending_id_order(
    world: Callable[[], FakeSQLSession],
) -> None:
    """대표원장인 샵과 만든 샵을 합쳐 shop_id 오름차순으로 한 번씩 잠근다.

    여러 샵을 잠그는 순서를 고정해야 동시에 도는 다른 탈퇴 · 해제 · 강등과 교착하지
    않는다. 두 샵 모두 다른 활성 멤버가 없어 탈퇴는 커밋된다.
    """
    db = world()

    _delete_account(db, soft=False)

    locked = [
        int(re.search(r"shop_user\.shop_id = (\d+)", sql).group(1))
        for sql in _sqls(db)
        if sql.endswith("FOR UPDATE")
    ]
    assert locked == [SHOP_ID, OTHER_SHOP_ID]
    assert db.commit_count == 1


# ---------------------------------------------------------------------------
# API 문서: 강제 탈퇴는 만든 샵과 데이터를 지우고, 다른 멤버가 있으면 409
# ---------------------------------------------------------------------------


def test_force_delete_docs_warn_about_created_shops_and_conflict() -> None:
    """DELETE /users/me/force 설명이 만든 샵 · 데이터 삭제와 409 경우를 알린다."""
    api = FastAPI()
    api.include_router(user_api.router)

    operation = api.openapi()["paths"]["/users/me/force"]["delete"]

    assert operation["description"] == FORCE_DELETE_DESCRIPTION
    assert "409" in operation["responses"]
