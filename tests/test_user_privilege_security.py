"""REQ-SEC-003 권한 상승 방지 테스트.

대상: app/schemas/user.py (UserCreate), app/crud/user_crud.py (update_user_db),
      app/services/user_service.py (resolve_signup_role)
- 가입 요청에 role 필드를 넣어도 클라이언트 role 이 수용되지 않음 (AC-003-1)
- 수정 시 role 등 권한 필드가 화이트리스트로 차단됨 (AC-003-2)
"""

from app.enum.role import UserRole
from app.schemas.user import UserCreate
from app.services.user_service import resolve_signup_role


def test_usercreate_rejects_client_role() -> None:
    """AC-003-1: UserCreate 는 클라이언트가 보낸 role 을 필드로 수용하지 않는다.

    BaseResponseModel 이 extra 를 무시/금지하든, role 은 모델 필드가 아니므로
    설령 전달돼도 인스턴스 속성으로 노출되지 않는다.
    """
    payload = {
        "name": "홍길동",
        "email": "hong@test.com",
        "password": "pass1234",
        "role": "ADMIN",  # 악의적 권한 상승 시도
    }
    user = UserCreate(**payload)
    # role 은 UserCreate 의 필드가 아니어야 한다.
    assert "role" not in user.model_fields
    assert not hasattr(user, "role") or "role" not in user.model_dump()


def test_resolve_signup_role_defaults_to_master() -> None:
    """AC-003-1: 초대 코드 없으면 서버가 기본 비특권 role(MASTER)로 결정."""
    # password 는 테스트용 더미 비밀번호로 실제 시크릿이 아니다.
    user = UserCreate(name="김원장", email="master@test.com", password="pass1234")  # noqa: S106
    assert resolve_signup_role(user) == UserRole.MASTER


def test_resolve_signup_role_manager_with_invite() -> None:
    """AC-003-1: 초대 코드가 있으면 MANAGER 로 결정 (ADMIN 은 절대 부여 안됨)."""
    user = UserCreate(
        name="매니저",
        email="manager@test.com",
        password="pass1234",  # noqa: S106  # 테스트용 더미 비밀번호
        invite_code="ABCDEFGHIJ",
    )
    role = resolve_signup_role(user)
    assert role == UserRole.MANAGER
    assert role != UserRole.ADMIN


class FakeUserRow:
    """update_user_db 대상 User 스텁."""

    def __init__(self) -> None:
        self.name = "old"
        self.email = "old@test.com"
        self.password = "oldhash"  # noqa: S105  # 테스트용 더미 해시
        self.token = None
        self.role = UserRole.MASTER


class FakeSession:
    def commit(self) -> None:
        pass

    def refresh(self, _obj: object) -> None:
        pass


def test_update_user_db_whitelists_out_role() -> None:
    """AC-003-2: 화이트리스트에 없는 role 은 무시되어 변경되지 않는다."""
    from app.crud.user_crud import update_user_db

    user = FakeUserRow()
    payload = {"name": "new", "role": UserRole.ADMIN}  # role 상승 시도

    updated = update_user_db(FakeSession(), user, payload)

    assert updated.name == "new"  # 허용 필드는 반영
    assert updated.role == UserRole.MASTER  # role 은 그대로 (상승 차단)


def test_update_user_db_ignores_id() -> None:
    """AC-003-2: id 등 불변 필드도 화이트리스트에서 제외되어 무시된다."""
    from app.crud.user_crud import update_user_db

    user = FakeUserRow()
    user.id = 100
    payload = {"id": 999, "email": "new@test.com"}

    updated = update_user_db(FakeSession(), user, payload)
    assert updated.id == 100  # 변경 안됨
    assert updated.email == "new@test.com"  # 허용 필드는 반영
