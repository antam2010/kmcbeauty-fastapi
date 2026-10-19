from sqlalchemy.orm import Session

from app.models.user import User
from app.utils.email import normalize_email


def create_user(db: Session, user_data: dict) -> User:
    """사용자 생성."""
    new_user = User(**user_data)
    db.add(new_user)
    db.flush()
    return new_user


# @MX:NOTE: [AUTO] 활성 유저 조회 불변식 — 인증/활성 조회 경로는 소프트삭제
#            유저를 제외한다. (SPEC-FIX-001 REQ-FIX-001)
#            auth_service.refresh_access_token 의 is_deleted() 체크(SECURITY-001)와
#            정합하도록 로그인/재발급 양 경로에서 삭제 유저를 차단한다.
def get_user_by_id(db: Session, user_id: int) -> User | None:
    """user_id 기준 사용자 단순 조회 (권한 체크 없음, 소프트삭제 제외)."""
    return db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()


# 권한 상승 방지: 자기 정보 수정으로 변경 가능한 필드 화이트리스트.
# role, id, 타임스탬프, 소프트삭제 필드 등 권한/불변 필드는 제외한다.
_UPDATABLE_USER_FIELDS = frozenset({"name", "email", "password", "token"})


def update_user_db(_db: Session, user: User, user_data: dict) -> User:
    # 트랜잭션 경계는 서비스 계층이 소유한다(SPEC-FIX-001 REQ-FIX-005).
    # CRUD 는 영속 상태 변경(setattr)까지만 수행하고 commit 은 하지 않는다.
    # 커밋/refresh 는 호출부(user_service.update_user_service)가 담당한다.
    for key, value in user_data.items():
        # 화이트리스트에 없는 필드(role 등 권한 필드)는 무시한다(권한 상승 차단).
        if key in _UPDATABLE_USER_FIELDS and hasattr(user, key):
            setattr(user, key, value)
    return user


# @MX:ANCHOR: [AUTO] 이메일 기준 유저 조회 단일 진입점(로그인·중복 체크·샵 유저 연결)
# @MX:REASON: PostgreSQL 은 대소문자를 구분하므로 여기서 normalize_email 로 정규화해야
#             소문자 저장 규약(NormalizedEmailStr)과 일치한다. 호출부는 원본 입력을
#             그대로 넘긴다. func.lower(User.email) 로 바꾸면 unique 인덱스를 못 탄다.
def get_user_by_email(db: Session, email: str) -> User | None:
    """이메일 기준 사용자 단순 조회 (권한 체크 없음, 소프트삭제 제외).

    소프트삭제된(deleted_at IS NOT NULL) 유저는 로그인 인증 대상에서 제외한다.
    (SPEC-FIX-001 REQ-FIX-001: 삭제 유저 재로그인 차단)

    이메일은 저장 규약과 같은 소문자 정규형으로 바꾼 뒤 비교한다.
    """
    email = normalize_email(email)
    return db.query(User).filter(User.email == email, User.deleted_at.is_(None)).first()


def delete_user_db(db: Session, user: User, is_soft_delete: bool = True) -> None:
    """사용자 삭제 (소프트 삭제 또는 하드 삭제)."""
    if is_soft_delete:
        user.soft_delete()
    else:
        db.delete(user)
