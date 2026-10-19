"""탈퇴(소프트삭제) 유저 멤버십 제외 테스트 (보안).

사용자 결정: 탈퇴해도 shop_user 행은 이력으로 남긴다. 대신 탈퇴 유저는 어디서도
멤버로 보지 않는다.

대상:
- app/crud/shop_user_crud.py
  - get_shop_user: 멤버십 판정(대표원장 게이트 · 직원 관리 · 초대 · 푸시 인가 ·
    시술 담당자 검증)
  - users_share_shop: 유저 대상 푸시 인가(양쪽 모두 활성 유저여야 공유로 본다)
  - get_shop_users_by_shop_id: 직원 목록(GET /shops/{shop_id}/users)
  - get_shop_user_with_user: 직원 연결 수정 대상 조회
- app/crud/device_push_token_crud.py::get_device_tokens_by_shop (샵 푸시 수신자)
- app/services/user_service.py::delete_user_service
  (소프트삭제 시 같은 트랜잭션에서 디바이스 토큰 비활성화)

버그: 멤버십 판정이 shop_user 행만 봤다. 탈퇴해도 행이 남으므로 탈퇴 유저가 계속
시술 담당자로 지정되고, 샵 푸시를 받고, 직원 목록에 보였다. 탈퇴 시 토큰도 활성으로
남았다.

DB 없이 실제 서비스 · CRUD 를 실행한다. CRUD 가 만든 SELECT 를 in-memory 행 위에서
SQL 의미로 평가한다(tests/fake_sql.py). FCM · Redis 는 스텁한다(네트워크 호출 없음).
"""

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.crud import shop_user_crud
from app.enum.role import UserRole
from app.enum.treatment_status import TreatmentStatus
from app.exceptions import CustomException
from app.models.device_push_token import DevicePushToken
from app.models.shop_user import ShopUser
from app.models.user import User
from app.schemas.device_push_token import FCMMessageRequest
from app.schemas.shop_user import ShopUserAssociateUpdateRequest
from app.schemas.treatment import TreatmentCreate
from app.services import (
    device_push_token_service,
    shop_user_service,
    treatment_service,
    user_service,
)
from tests.fake_sql import FakeSQLSession

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SHOP_ID = 10
OWNER_ID, MEMBER_ID, LEFT_ID = 1, 2, 3  # LEFT = 탈퇴한(소프트삭제) 직원
CURRENT_SHOP = SimpleNamespace(id=SHOP_ID, user_id=OWNER_ID)
OWNER = SimpleNamespace(id=OWNER_ID)
FCM_OWNER, FCM_MEMBER, FCM_LEFT = "fcm-owner", "fcm-member", "fcm-left"
_DUMMY_HASH = "not-a-real-hash"
_INVALID_STAFF = "시술 담당자(staff_user_id)가 유효하지 않습니다."


def _user(user_id: int, deleted_at: datetime | None = None) -> User:
    return User(
        id=user_id,
        email=f"user{user_id}@test.com",
        name=f"유저{user_id}",
        password=_DUMMY_HASH,
        role=UserRole.MANAGER,
        deleted_at=deleted_at,
        created_at=NOW,
        updated_at=NOW,
    )


def _token(token_id: int, user_id: int, value: str) -> DevicePushToken:
    return DevicePushToken(
        id=token_id,
        user_id=user_id,
        shop_id=SHOP_ID,
        token=value,
        platform="android",
        is_active=True,
    )


def _world(session_cls: type[FakeSQLSession] = FakeSQLSession) -> FakeSQLSession:
    """원장 · 직원 · 탈퇴한 직원이 있는 샵. 탈퇴 직원의 매핑과 토큰은 남아 있다."""
    return session_cls(
        _user(OWNER_ID),
        _user(MEMBER_ID),
        _user(LEFT_ID, deleted_at=NOW),
        ShopUser(id=1, shop_id=SHOP_ID, user_id=OWNER_ID, is_primary_owner=1),
        ShopUser(id=2, shop_id=SHOP_ID, user_id=MEMBER_ID, is_primary_owner=0),
        ShopUser(id=3, shop_id=SHOP_ID, user_id=LEFT_ID, is_primary_owner=0),
        _token(1, OWNER_ID, FCM_OWNER),
        _token(2, MEMBER_ID, FCM_MEMBER),
        _token(3, LEFT_ID, FCM_LEFT),
    )


# ---------------------------------------------------------------------------
# 시술 담당자: 탈퇴 유저는 유효한 담당자가 아니다
# ---------------------------------------------------------------------------


def _create_treatment(db: FakeSQLSession, staff_user_id: int) -> object:
    return treatment_service.upsert_treatment_service(
        db=db,
        data=TreatmentCreate(
            reserved_at=NOW,
            status=TreatmentStatus.RESERVED,
            staff_user_id=staff_user_id,
        ),
        current_shop=CURRENT_SHOP,
        current_user=OWNER,
    )


def test_left_member_is_not_a_valid_staff() -> None:
    """매핑이 남은 탈퇴 직원을 담당자로 지정하면 400 이고 예약은 생기지 않는다."""
    db = _world()

    with pytest.raises(CustomException) as exc:
        _create_treatment(db, LEFT_ID)

    assert exc.value.status_code == 400
    assert exc.value.detail["detail"] == _INVALID_STAFF
    assert db.commit_count == 0
    assert db.rows("treatment") == []


def test_active_member_is_a_valid_staff() -> None:
    """대조군: 같은 샵의 활성 직원은 그대로 담당자로 지정된다."""
    db = _world()

    _create_treatment(db, MEMBER_ID)

    assert db.commit_count == 1
    [treatment] = db.rows("treatment")
    assert treatment.staff_user_id == MEMBER_ID


# ---------------------------------------------------------------------------
# 푸시: 탈퇴 유저는 샵 수신자도, 유저 대상도 아니다
# ---------------------------------------------------------------------------


@pytest.fixture
def fcm_spy(monkeypatch: pytest.MonkeyPatch) -> list:
    """실제 FCM 전송을 막고, 전송 시도된 토큰 목록(정렬)을 기록한다."""
    sent: list = []

    def fake_send_one(**kwargs: object) -> dict:
        sent.append([kwargs["token"]])
        return {"success": True, "message_id": "projects/test/messages/1"}

    def fake_send_many(**kwargs: object) -> dict:
        tokens = sorted(kwargs["tokens"])
        sent.append(tokens)
        return {"success": True, "success_count": len(tokens), "failure_count": 0}

    monkeypatch.setattr(device_push_token_service, "send_fcm_message", fake_send_one)
    monkeypatch.setattr(
        device_push_token_service,
        "send_fcm_multicast",
        fake_send_many,
    )
    return sent


def _send(db: FakeSQLSession, **target: int) -> object:
    return device_push_token_service.send_fcm_notification_service(
        db=db,
        fcm_request=FCMMessageRequest(
            title="예약 알림",
            body="10시 예약이 있습니다.",
            **target,
        ),
        current_user=OWNER,
    )


def test_shop_push_skips_left_member(fcm_spy: list) -> None:
    """샵 대상 푸시는 탈퇴 직원에게 가지 않는다(매핑 · 활성 토큰이 남아 있어도)."""
    _send(_world(), shop_id=SHOP_ID)

    assert fcm_spy == [sorted([FCM_OWNER, FCM_MEMBER])]


def test_user_push_to_left_member_is_403(fcm_spy: list) -> None:
    """탈퇴 직원은 샵을 공유하는 유저로 보지 않는다(403, FCM 호출 없음)."""
    with pytest.raises(CustomException) as exc:
        _send(_world(), user_id=LEFT_ID)

    assert exc.value.status_code == 403
    assert fcm_spy == []


def test_user_push_to_active_member_still_works(fcm_spy: list) -> None:
    """대조군: 같은 샵의 활성 직원에게는 그대로 전송된다."""
    _send(_world(), user_id=MEMBER_ID)

    assert fcm_spy == [[FCM_MEMBER]]


@pytest.mark.parametrize(
    ("user_id", "other_user_id", "expected"),
    [
        (OWNER_ID, MEMBER_ID, True),
        (OWNER_ID, LEFT_ID, False),
        (LEFT_ID, OWNER_ID, False),
    ],
    ids=["both-active", "left-target", "left-requester"],
)
def test_users_share_shop_requires_both_users_active(
    user_id: int,
    other_user_id: int,
    expected: bool,
) -> None:
    """공유 샵 판정은 두 유저가 모두 활성일 때만 참이다(어느 쪽이 탈퇴해도 거짓)."""
    assert shop_user_crud.users_share_shop(_world(), user_id, other_user_id) is expected


# ---------------------------------------------------------------------------
# 직원 목록 · 직원 관리: 탈퇴 유저의 매핑은 이력으로만 남는다
# ---------------------------------------------------------------------------


def test_member_list_excludes_left_member() -> None:
    """직원 목록(GET /shops/{shop_id}/users)에 탈퇴 직원은 보이지 않는다."""
    members = shop_user_service.get_shop_users_service(
        db=_world(),
        shop_id=SHOP_ID,
        current_user=OWNER,
    )

    assert [member.user_id for member in members] == [MEMBER_ID, OWNER_ID]


def test_left_member_mapping_is_hidden_from_staff_management() -> None:
    """직원 연결 수정 · 해제도 탈퇴 직원의 매핑을 없음(404)으로 본다. 행은 그대로다."""
    db = _world()

    with pytest.raises(CustomException) as update_exc:
        shop_user_service.update_shop_user_service(
            db=db,
            shop_id=SHOP_ID,
            user_id=LEFT_ID,
            current_user=OWNER,
            payload=ShopUserAssociateUpdateRequest(is_primary_owner=1),
        )
    with pytest.raises(CustomException) as delete_exc:
        shop_user_service.delete_shop_user_service(
            db=db,
            shop_id=SHOP_ID,
            user_id=LEFT_ID,
            current_user=OWNER,
        )

    assert (update_exc.value.status_code, delete_exc.value.status_code) == (404, 404)
    assert db.commit_count == 0
    [left_row] = [row for row in db.rows("shop_user") if row.user_id == LEFT_ID]
    assert left_row.is_primary_owner == 0


class RecordingQuery(Query):
    """세션 없는 실제 Query. all()/first() 는 실행 대신 SELECT 문을 기록한다."""

    def all(self) -> list:
        self.recorded.append(self.statement)
        return []

    def first(self) -> None:
        self.recorded.append(self.statement)


class CompileSession:
    def __init__(self) -> None:
        self.statements: list = []

    def query(self, *entities: type) -> RecordingQuery:
        query = RecordingQuery(entities)
        query.recorded = self.statements  # 생성형 체인 복사본에도 전달된다
        return query


@pytest.mark.parametrize(
    "lookup",
    [
        lambda db: shop_user_crud.get_shop_users_by_shop_id(db, SHOP_ID),
        lambda db: shop_user_crud.get_shop_user_with_user(db, SHOP_ID, MEMBER_ID),
    ],
    ids=["member-list", "single-member"],
)
def test_member_lookups_load_user_from_filtered_join(
    lookup: Callable[[CompileSession], object],
) -> None:
    """응답용 user 관계는 탈퇴 필터를 건 그 조인으로 채운다(eager 조인 중복 없음)."""
    db = CompileSession()

    lookup(db)

    [statement] = db.statements
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = " ".join(str(compiled).split())
    assert "JOIN users ON users.id = shop_user.user_id" in sql
    assert "users.deleted_at IS NULL" in sql
    assert "LEFT OUTER JOIN" not in sql


# ---------------------------------------------------------------------------
# 탈퇴: 소프트삭제는 같은 트랜잭션에서 토큰을 비활성화한다
# ---------------------------------------------------------------------------


class CommitProbeSession(FakeSQLSession):
    """커밋 시점의 토큰 활성 상태를 기록한다(커밋 전에 같은 트랜잭션에서 바뀌었는지)."""

    def commit(self) -> None:
        self.active_at_commit = {
            token.token: token.is_active for token in self.rows("device_push_tokens")
        }
        super().commit()


def test_soft_delete_deactivates_device_tokens_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """탈퇴하면 그 유저의 토큰만 커밋 전에 비활성화되고, Redis 는 커밋 뒤에 비운다."""
    db = _world(CommitProbeSession)
    cleared: set = set()
    monkeypatch.setattr(
        user_service,
        "clear_user_redis",
        lambda user_id: cleared.add(("user", user_id, db.commit_count)),
    )
    monkeypatch.setattr(
        user_service,
        "clear_refresh_token_redis",
        lambda user_id: cleared.add(("refresh", user_id, db.commit_count)),
    )

    user_service.delete_user_service(
        db=db,
        current_user=SimpleNamespace(id=MEMBER_ID),
        is_soft_delete=True,
    )

    assert db.commit_count == 1
    assert db.active_at_commit == {
        FCM_OWNER: True,
        FCM_MEMBER: False,
        FCM_LEFT: True,
    }
    [member] = [user for user in db.rows("users") if user.id == MEMBER_ID]
    assert member.deleted_at is not None
    assert cleared == {("user", MEMBER_ID, 1), ("refresh", MEMBER_ID, 1)}


def test_force_delete_removes_tokens_through_fk_cascade() -> None:
    """강제 삭제(하드)는 비활성화가 필요 없다 — 토큰 FK 가 users 삭제를 CASCADE 한다."""
    [fk] = DevicePushToken.__table__.c.user_id.foreign_keys

    assert (fk.column.table.name, fk.ondelete) == ("users", "CASCADE")
