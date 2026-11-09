"""탈퇴로 직원이 있는 샵의 마지막 대표원장이 사라지는 것 차단 테스트 (보안).

사용자 결정(PR #6 리뷰 H1): 활성 대표원장이 자기뿐인 샵에 다른 활성 멤버(역할 무관,
탈퇴 유저 제외)가 있으면 탈퇴(소프트 DELETE /users/me · 강제 DELETE /users/me/force)는
409 다. 다른 활성 멤버가 없는 샵은 막지 않는다 — 샵 삭제 API 가 없어 혼자인 원장도
탈퇴할 수 있어야 한다(연결 해제 · 강등 가드보다 느슨하다).

대상:
- app/services/user_service.py::delete_user_service
- app/crud/shop_user_crud.py::get_primary_owner_shop_ids · count_other_active_members
  (기존 lock_primary_owners · count_other_primary_owners 재사용)
- app/api/user.py 의 두 DELETE 문서(409)

버그: 연결 해제 · 강등은 409 인데 탈퇴에는 대표원장 확인이 없었다. 마지막 대표원장이
탈퇴하면 직원은 남고 관리할 사람은 없는 샵이 된다(리뷰 재현: 해제 409, 소프트 탈퇴
204 → 활성 대표원장 0명).

DB · Redis 없이 실제 서비스 · CRUD 를 실행한다(tests/fake_sql.py). 커밋 뒤 세션
정리(Redis)는 호출만 기록하는 스텁이다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, status
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.api import user as user_api
from app.docs.common_responses import COMMON_ERROR_RESPONSES
from app.enum.role import UserRole
from app.exceptions import CustomException
from app.models.shop_user import ShopUser
from app.models.user import User
from app.services import user_service
from tests.fake_sql import FakeSQLSession

NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
SHOP_ID, SECOND_SHOP_ID, STAFF_SHOP_ID = 10, 20, 30
# OWNER = 탈퇴하는 유저, LEFT = 이미 탈퇴한(소프트삭제) 유저
OWNER_ID, CO_OWNER_ID, STAFF_ID, LEFT_ID = 1, 2, 3, 4
DELETION_DETAIL = (
    "직원이 있는 샵의 마지막 대표원장은 탈퇴할 수 없습니다. "
    "다른 대표원장을 먼저 지정하세요."
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


def _world(*memberships: Membership) -> FakeSQLSession:
    """OWNER 와 멤버십에 나오는 유저들(LEFT 는 탈퇴 상태)."""
    user_ids = {OWNER_ID} | {user_id for _, user_id, _ in memberships}
    return FakeSQLSession(
        *(_user(user_id) for user_id in sorted(user_ids)),
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


MODES = [pytest.param(True, id="soft"), pytest.param(False, id="force")]


# ---------------------------------------------------------------------------
# 직원이 있는 샵의 마지막 대표원장: 두 탈퇴 모두 409, 삭제 · 커밋 없음
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("soft", MODES)
@pytest.mark.parametrize(
    "world",
    [
        pytest.param(
            lambda: _world((SHOP_ID, OWNER_ID, 1), (SHOP_ID, STAFF_ID, 0)),
            id="with-staff",
        ),
        pytest.param(
            lambda: _world(
                (SHOP_ID, OWNER_ID, 1),
                (SHOP_ID, LEFT_ID, 1),
                (SHOP_ID, STAFF_ID, 0),
            ),
            id="left-co-owner-does-not-count",
        ),
        pytest.param(
            lambda: _world(
                (SHOP_ID, OWNER_ID, 1),
                (SECOND_SHOP_ID, OWNER_ID, 1),
                (SECOND_SHOP_ID, STAFF_ID, 0),
            ),
            id="second-shop-blocks",
        ),
    ],
)
def test_last_owner_of_staffed_shop_cannot_delete_account(
    cleared: list[tuple[str, int]],
    world: Callable[[], FakeSQLSession],
    soft: bool,
) -> None:
    """직원이 있는 샵의 유일한 활성 대표원장이면 탈퇴는 409 이고 아무것도 바뀌지 않는다.

    롤백 경로를 탄다(잠금 해제). 탈퇴한 대표원장은 다른 대표원장으로 세지 않는다.
    """
    db = world()

    with pytest.raises(CustomException) as exc:
        _delete_account(db, soft=soft)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "USER_CONFLICT"
    assert exc.value.detail["detail"] == DELETION_DETAIL
    assert not _owner_deleted(db)
    assert db.deleted == []
    assert (db.commit_count, db.rollback_count) == (0, 1)
    assert cleared == []


# ---------------------------------------------------------------------------
# 막지 않는 경우: 혼자인 샵 · 다른 활성 대표원장 · 직원으로만 속한 샵
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("soft", MODES)
@pytest.mark.parametrize(
    "world",
    [
        pytest.param(lambda: _world((SHOP_ID, OWNER_ID, 1)), id="sole-member"),
        pytest.param(
            lambda: _world((SHOP_ID, OWNER_ID, 1), (SHOP_ID, LEFT_ID, 0)),
            id="left-staff-does-not-count",
        ),
        pytest.param(
            lambda: _world(
                (SHOP_ID, OWNER_ID, 1),
                (SHOP_ID, CO_OWNER_ID, 1),
                (SHOP_ID, STAFF_ID, 0),
            ),
            id="another-active-owner",
        ),
        pytest.param(
            lambda: _world((STAFF_SHOP_ID, OWNER_ID, 0), (STAFF_SHOP_ID, STAFF_ID, 0)),
            id="staff-only-membership-is-not-checked",
        ),
    ],
)
def test_account_deletion_proceeds_when_no_staffed_shop_is_left_without_owner(
    cleared: list[tuple[str, int]],
    world: Callable[[], FakeSQLSession],
    soft: bool,
) -> None:
    """다른 활성 멤버가 없거나 다른 활성 대표원장이 있으면 탈퇴는 그대로 커밋된다."""
    db = world()

    _delete_account(db, soft=soft)

    assert _owner_deleted(db)
    assert db.commit_count == 1
    assert cleared == [("user", OWNER_ID), ("refresh", OWNER_ID)]


# ---------------------------------------------------------------------------
# 동시성: 대표원장 행을 잠근 뒤에 센다(연결 해제 · 강등 가드와 같은 잠금)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("soft", MODES)
@pytest.mark.usefixtures("cleared")
def test_owner_rows_are_locked_before_counting(soft: bool) -> None:
    """세기 전에 그 샵의 대표원장 행을 잠근다(SELECT ... FOR UPDATE).

    잠금 없이 세면 탈퇴와 다른 대표원장의 강등 · 해제가 동시에 통과해 직원만 남은
    샵이 될 수 있다. 같은 잠금이라 두 가드가 직렬화된다.
    """
    db = _world((SHOP_ID, OWNER_ID, 1), (SHOP_ID, STAFF_ID, 0))

    with pytest.raises(CustomException):
        _delete_account(db, soft=soft)

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
    locks = [i for i, sql in enumerate(sqls) if sql.endswith("FOR UPDATE")]
    counts = [i for i, sql in enumerate(sqls) if sql.startswith("SELECT count(")]
    assert len(locks) == 1, sqls
    assert len(counts) == 2, sqls  # 다른 대표원장 → 다른 활성 멤버
    assert locks[0] < min(counts), sqls
    assert "shop_user.shop_id = 10" in sqls[locks[0]]


# ---------------------------------------------------------------------------
# API 문서: 두 탈퇴 엔드포인트의 409
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/users/me", "/users/me/force"])
def test_account_deletion_conflict_is_documented(path: str) -> None:
    """DELETE /users/me · /users/me/force 문서에 409 응답이 있다."""
    api = FastAPI()
    api.include_router(user_api.router)

    operation = api.openapi()["paths"][path]["delete"]

    conflict = COMMON_ERROR_RESPONSES[status.HTTP_409_CONFLICT]
    assert operation["responses"]["409"]["description"] == conflict["description"]
