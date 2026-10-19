"""초대코드 1회용(첫 가입이 소비) 테스트 (보안).

사용자 결정: 초대코드는 그 코드로 처음 성공한 가입이 소비한다.

대상:
- app/services/user_service.py::create_user_service / validate_user_creation
  - 초대 행을 잠그고(SELECT ... FOR UPDATE) 검증한 뒤, 유저 + MANAGER 멤버십 생성과
    초대 삭제를 같은 트랜잭션에서 커밋한다.
  - 같은 코드의 두 번째 가입은 "유효하지 않거나 만료된 초대 코드"(400)다. 동시에
    들어온 두 번째 가입은 첫 가입이 커밋될 때까지 잠금에서 기다린 뒤 삭제된 행을
    못 찾아 같은 400 이 된다(PostgreSQL READ COMMITTED).
  - 실패한 가입(이메일 중복 → 롤백)은 코드를 소비하지 않는다.
- app/crud/shop_invite_curd.py::get_invite_by_code(for_update=True), delete_invite
- 소비 후 GET /shops/{shop_id}/invites(get_invite_code_service)는 404 다.

DB 없이 실제 서비스 · CRUD 를 실행한다(tests/fake_sql.py). 커밋 전 삭제는 커밋 때
반영되고 롤백하면 버려진다(트랜잭션 의미).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.enum.role import UserRole
from app.exceptions import CustomException
from app.models.shop_invite import ShopInvite
from app.models.shop_user import ShopUser
from app.models.user import User
from app.schemas.user import UserCreate
from app.services import shop_invite_service, user_service
from tests.fake_sql import FakeSQLSession

SHOP_ID = 10
OWNER_ID = 1
INVITE_CODE = "AbCdEfGhIjK"
_PLAINTEXT = "pass1234"  # 테스트용 더미 비밀번호
_DUMMY_HASH = "not-a-real-hash"
_INVALID_INVITE = "유효하지 않거나 만료된 초대 코드입니다."
# 커밋 때 드러난 이메일 unique 위반. statement · params · orig 는 표시용이다.
_DUPLICATE_EMAIL = IntegrityError("INSERT INTO users", {}, Exception("duplicate"))


class FlakyCommitSession(FakeSQLSession):
    """첫 커밋만 IntegrityError 로 실패한다(이메일 중복이 커밋 때 드러난 경우)."""

    def __init__(self, *rows: object) -> None:
        super().__init__(*rows)
        self.fail_next_commit = True

    def commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise _DUPLICATE_EMAIL
        super().commit()


def _world(session_cls: type[FakeSQLSession] = FakeSQLSession) -> FakeSQLSession:
    """원장 1명과 만료 전 초대코드 1개가 있는 샵."""
    now = datetime.now(UTC)
    return session_cls(
        User(
            id=OWNER_ID,
            email="owner@test.com",
            name="원장님",
            password=_DUMMY_HASH,
            role=UserRole.MASTER,
            created_at=now,
            updated_at=now,
        ),
        ShopUser(id=1, shop_id=SHOP_ID, user_id=OWNER_ID, is_primary_owner=1),
        ShopInvite(
            id=1,
            shop_id=SHOP_ID,
            invite_code=INVITE_CODE,
            expired_at=now + timedelta(days=7),
        ),
    )


def _signup(db: FakeSQLSession, email: str) -> object:
    return user_service.create_user_service(
        db,
        UserCreate(
            name="새 직원",
            email=email,
            password=_PLAINTEXT,
            invite_code=INVITE_CODE,
        ),
    )


def _members(db: FakeSQLSession) -> list[int]:
    return sorted(row.user_id for row in db.rows("shop_user"))


def test_first_signup_consumes_invite_and_second_is_rejected() -> None:
    """첫 가입은 MANAGER 멤버십을 만들고 코드를 지운다. 같은 코드의 다음 가입은 400."""
    db = _world()

    first = _signup(db, "first@test.com")

    assert first.role == UserRole.MANAGER
    assert _members(db) == [OWNER_ID, first.id]
    assert db.rows("shop_invite") == []

    with pytest.raises(CustomException) as exc:
        _signup(db, "second@test.com")

    assert exc.value.status_code == 400
    assert exc.value.detail["detail"] == _INVALID_INVITE
    assert _members(db) == [OWNER_ID, first.id]
    assert sorted(user.email for user in db.rows("users")) == [
        "first@test.com",
        "owner@test.com",
    ]


def test_failed_signup_does_not_consume_invite() -> None:
    """가입 커밋이 실패하면(이메일 중복 409) 롤백되어 코드가 남고, 재시도는 성공한다."""
    db = _world(FlakyCommitSession)

    with pytest.raises(CustomException) as exc:
        _signup(db, "dup@test.com")

    assert exc.value.status_code == 409
    assert db.rollback_count == 1
    assert [invite.invite_code for invite in db.rows("shop_invite")] == [INVITE_CODE]

    retried = _signup(db, "retry@test.com")

    assert retried.role == UserRole.MANAGER
    assert db.rows("shop_invite") == []


def test_signup_locks_invite_row_for_update() -> None:
    """가입의 초대코드 조회는 행 잠금(SELECT ... FOR UPDATE)을 요청한다."""
    db = _world()

    _signup(db, "first@test.com")

    invite_selects = [
        " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())
        for stmt in db.statements
        if "shop_invite" in {table.name for table in stmt.get_final_froms()}
    ]
    assert invite_selects
    assert all(sql.endswith("FOR UPDATE") for sql in invite_selects)


def test_consumed_invite_is_not_found_for_owner() -> None:
    """소비된 뒤 원장의 초대코드 조회(GET /shops/{shop_id}/invites)는 404 다."""
    db = _world()
    _signup(db, "first@test.com")

    with pytest.raises(CustomException) as exc:
        shop_invite_service.get_invite_code_service(
            db=db,
            shop_id=SHOP_ID,
            user=SimpleNamespace(id=OWNER_ID),
        )

    assert exc.value.status_code == 404
