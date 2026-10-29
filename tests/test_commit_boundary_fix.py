"""SPEC-FIX-001 REQ-FIX-005 — 커밋 경계 서비스 이관 테스트.

대상:
- app/crud/user_crud.py::update_user_db (내부 commit 제거)
- app/crud/treatment_menu_crud.py::create_treatment_menu_detail (내부 commit 제거)

CRUD 는 더 이상 db.commit() 을 호출하지 않고 서비스 계층이 트랜잭션을 확정한다.
DB 없이 commit 호출 여부를 기록하는 스파이 세션으로 관찰한다.
"""

from app.enum.role import UserRole


class SpySession:
    """commit/flush/add/refresh 호출을 기록하는 스파이 세션."""

    def __init__(self) -> None:
        self.commit_count = 0
        self.flush_count = 0
        self.add_count = 0
        self.refresh_count = 0

    def commit(self) -> None:
        self.commit_count += 1

    def flush(self) -> None:
        self.flush_count += 1

    def add(self, _obj: object) -> None:
        self.add_count += 1

    def refresh(self, _obj: object) -> None:
        self.refresh_count += 1


class FakeUserRow:
    def __init__(self) -> None:
        self.name = "old"
        self.email = "old@test.com"
        self.password = "oldhash"  # noqa: S105  # 테스트용 더미 해시
        self.token = None
        self.role = UserRole.MASTER


def test_update_user_db_does_not_commit() -> None:
    """AC-005-3: update_user_db 는 내부에서 커밋하지 않는다(경계 서비스 이관)."""
    from app.crud.user_crud import update_user_db

    db = SpySession()
    user = FakeUserRow()
    updated = update_user_db(db, user, {"name": "new"})

    assert updated.name == "new"  # 변경은 반영
    assert db.commit_count == 0  # 커밋은 서비스가 담당


def test_create_treatment_menu_detail_does_not_commit() -> None:
    """AC-005-4: create_treatment_menu_detail 은 내부에서 커밋하지 않는다.

    add + flush 까지만 수행하고 commit 은 서비스가 담당한다.
    """
    from app.crud.treatment_menu_crud import create_treatment_menu_detail

    db = SpySession()
    detail = create_treatment_menu_detail(
        db=db,
        menu_id=1,
        name="컷트",
        duration_min=30,
        base_price=10000,
    )

    assert detail is not None
    assert db.commit_count == 0  # 커밋은 서비스가 담당
    assert db.add_count == 1  # 영속화용 add 는 수행
    assert db.flush_count == 1  # id 확보용 flush 는 수행
