"""대표원장이 없는 샵으로의 초대 가입 차단 테스트 (보안).

사용자 결정(PR #6 재리뷰 N1): 초대 가입(MANAGER)은 초대한 샵에 활성 대표원장이 있을
때만 한다. 없으면 409 이고 유저 · 멤버십을 만들지 않으며 초대코드도 소비하지 않는다.

대상:
- app/services/user_service.py::create_user_service (POST /users, invite_code 경로)
- app/crud/shop_user_crud.py::count_active_primary_owners (+ 기존 lock_primary_owners)

버그: 혼자인 원장은 초대코드(최대 30일)를 발급한 뒤 탈퇴할 수 있다(직원이 없으면 탈퇴
허용 정책). 그 뒤 초대받은 사람이 가입하면 get_invite_by_code 는 만료만 보므로 MANAGER
멤버십이 생겨, 멤버는 있는데 활성 대표원장이 없는 샵이 됐다. 초대 가입은 활성 대표원장
없이 샵에 들어오는 유일한 길이다(이메일 연결은 활성 대표원장 요청자가 필요하다).

요구 동작: 초대 행 잠금(FOR UPDATE) 뒤 같은 트랜잭션에서 그 샵의 대표원장 행을 잠그고
활성 대표원장(users.deleted_at IS NULL)을 센다. 0명이면 409 다. 초대코드 없는 가입은
그대로다.

DB 없이 실제 서비스 · CRUD 를 실행한다(tests/fake_sql.py). 가짜 세션의 flush 는 행을
바로 등록하므로, "생성 없음"은 가드가 유저 생성보다 먼저 도는지도 함께 확인한다.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.enum.role import UserRole
from app.exceptions import CustomException
from app.models.shop_invite import ShopInvite
from app.models.shop_user import ShopUser
from app.models.user import User
from app.schemas.user import UserCreate
from app.services import user_service
from tests.fake_sql import FakeSQLSession

SHOP_ID = 10
OWNER_ID = 1
INVITE_CODE = "AbCdEfGhIjK"
NEW_EMAIL = "new-staff@test.com"
NO_OWNER_DETAIL = "초대한 샵에 대표원장이 없어 가입할 수 없습니다."
_PLAINTEXT = "pass1234"  # 테스트용 더미 비밀번호
_DUMMY_HASH = "not-a-real-hash"


def _owner(deleted_at: datetime | None = None) -> list[object]:
    """샵의 대표원장 유저와 그 매핑."""
    now = datetime.now(UTC)
    return [
        User(
            id=OWNER_ID,
            email="owner@test.com",
            name="원장님",
            password=_DUMMY_HASH,
            role=UserRole.MASTER,
            deleted_at=deleted_at,
            created_at=now,
            updated_at=now,
        ),
        ShopUser(id=1, shop_id=SHOP_ID, user_id=OWNER_ID, is_primary_owner=1),
    ]


def _invite() -> ShopInvite:
    return ShopInvite(
        id=1,
        shop_id=SHOP_ID,
        invite_code=INVITE_CODE,
        expired_at=datetime.now(UTC) + timedelta(days=7),
    )


def _signup(db: FakeSQLSession, invite_code: str | None = INVITE_CODE) -> object:
    return user_service.create_user_service(
        db,
        UserCreate(
            name="새 직원",
            email=NEW_EMAIL,
            password=_PLAINTEXT,
            invite_code=invite_code,
        ),
    )


def _new_user_created(db: FakeSQLSession) -> bool:
    return any(user.email == NEW_EMAIL for user in db.rows("users"))


# ---------------------------------------------------------------------------
# 활성 대표원장이 없는 샵: 409, 생성 없음, 초대코드 미소비
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(
            lambda: _owner(deleted_at=datetime.now(UTC)),
            id="only-owner-soft-deleted",
        ),
        pytest.param(list, id="owner-hard-deleted"),  # 대표원장 행이 하나도 없다
    ],
)
def test_invite_signup_to_shop_without_active_owner_is_409(
    rows: object,
) -> None:
    """초대한 샵에 활성 대표원장이 없으면 409 이고 유저 · 멤버십 · 초대 소비가 없다."""
    db = FakeSQLSession(*rows(), _invite())
    memberships_before = list(db.rows("shop_user"))

    with pytest.raises(CustomException) as exc:
        _signup(db)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "USER_CONFLICT"
    assert exc.value.detail["detail"] == NO_OWNER_DETAIL
    assert not _new_user_created(db)
    assert db.rows("shop_user") == memberships_before
    assert [invite.invite_code for invite in db.rows("shop_invite")] == [INVITE_CODE]
    assert (db.commit_count, db.rollback_count) == (0, 1)


# ---------------------------------------------------------------------------
# 대조군: 활성 대표원장이 있는 샵 · 초대코드 없는 가입은 그대로다
# ---------------------------------------------------------------------------


def test_invite_signup_to_shop_with_active_owner_still_works() -> None:
    """활성 대표원장이 있으면 지금처럼 MANAGER 멤버십을 만들고 초대코드를 소비한다."""
    db = FakeSQLSession(*_owner(), _invite())

    created = _signup(db)

    assert created.role == UserRole.MANAGER
    assert sorted(
        (row.user_id, row.is_primary_owner) for row in db.rows("shop_user")
    ) == [(OWNER_ID, 1), (created.id, 0)]
    assert db.rows("shop_invite") == []
    assert db.commit_count == 1


def test_signup_without_invite_code_is_unaffected() -> None:
    """초대코드 없는 가입(MASTER)은 대표원장 확인을 하지 않는다(샵 행을 읽지 않는다)."""
    db = FakeSQLSession(*_owner(deleted_at=datetime.now(UTC)), _invite())

    created = _signup(db, invite_code=None)

    assert created.role == UserRole.MASTER
    assert db.commit_count == 1
    touched = {table.name for stmt in db.statements for table in stmt.get_final_froms()}
    assert "shop_user" not in touched


# ---------------------------------------------------------------------------
# 동시성: 초대 행 → 대표원장 행 순서로 잠근 뒤 센다
# ---------------------------------------------------------------------------


def test_owner_rows_are_locked_before_counting_active_owners() -> None:
    """초대 행 잠금 → 그 샵의 대표원장 행 잠금 → 활성 대표원장 집계 순서다.

    탈퇴 가드와 같은 대표원장 행 잠금이라, 가입과 탈퇴가 직렬화되고 기다린 쪽은 커밋된
    상태로 다시 센다(PostgreSQL READ COMMITTED).
    """
    db = FakeSQLSession(*_owner(deleted_at=datetime.now(UTC)), _invite())

    with pytest.raises(CustomException):
        _signup(db)

    sqls = [
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
    invite_locks = [
        i
        for i, sql in enumerate(sqls)
        if "FROM shop_invite" in sql and sql.endswith("FOR UPDATE")
    ]
    owner_locks = [
        i
        for i, sql in enumerate(sqls)
        if "FROM shop_user" in sql and sql.endswith("FOR UPDATE")
    ]
    counts = [i for i, sql in enumerate(sqls) if sql.startswith("SELECT count(")]
    assert (len(invite_locks), len(owner_locks), len(counts)) == (1, 1, 1), sqls
    assert invite_locks[0] < owner_locks[0] < counts[0], sqls
    assert "shop_user.shop_id = 10" in sqls[owner_locks[0]]
