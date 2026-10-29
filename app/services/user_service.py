from fastapi import status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.crud.device_push_token_crud import deactivate_device_tokens_by_user
from app.crud.shop_invite_curd import delete_invite, get_invite_by_code
from app.crud.shop_user_crud import ShopUser, create_shop_user
from app.crud.user_crud import (
    create_user,
    delete_user_db,
    get_user_by_email,
    get_user_by_id,
    update_user_db,
)
from app.enum.role import UserRole
from app.exceptions import CustomException
from app.models.shop_invite import ShopInvite
from app.models.user import User
from app.schemas.user import (
    UserCreate,
    UserEmailCheckResponse,
    UserResponse,
    UserUpdate,
)
from app.utils.redis.auth import clear_refresh_token_redis
from app.utils.redis.user import clear_user_redis

DOMAIN = "USER"


def get_user_service(db: Session, current_user: User) -> UserResponse:
    """현재 로그인한 사용자의 정보를 조회합니다."""
    user = get_user_by_id(db, current_user.id)
    if not user:
        raise CustomException(status_code=status.HTTP_404_NOT_FOUND, domain=DOMAIN)

    return UserResponse.model_validate(user)


def resolve_signup_role(data: UserCreate) -> UserRole:
    """가입 요청에서 서버가 role 을 결정한다(권한 상승 방지).

    - 초대 코드가 있으면 MANAGER
    - 없으면 기본값 MASTER(원장, 비특권 소유자)

    ADMIN 은 어떤 경우에도 가입 경로로 부여되지 않는다.
    """
    if data.invite_code:
        return UserRole.MANAGER
    return UserRole.MASTER


def validate_user_creation(
    role: UserRole,
    invite_code: str | None,
    db: Session,
) -> ShopInvite | None:
    """서버가 결정한 role 기준으로 가입 유효성을 검사한다.

    role 은 클라이언트 입력이 아니라 resolve_signup_role 로 서버가 결정한 값이다.
    MANAGER 가입의 초대 행은 잠근다(FOR UPDATE). 잠금은 가입 트랜잭션이 끝날 때까지
    유지된다.
    """
    if role == UserRole.ADMIN:
        raise CustomException(
            status_code=status.HTTP_403_FORBIDDEN,
            domain=DOMAIN,
            hint="ADMIN 권한은 가입할 수 없습니다.",
        )

    invite = None
    if role == UserRole.MANAGER:
        if not invite_code:
            raise CustomException(
                status_code=status.HTTP_400_BAD_REQUEST,
                domain=DOMAIN,
                detail="MANAGER 권한은 초대 코드가 필요합니다.",
            )

        invite = get_invite_by_code(db, invite_code, for_update=True)
        if not invite:
            raise CustomException(
                status_code=status.HTTP_400_BAD_REQUEST,
                domain=DOMAIN,
                detail="유효하지 않거나 만료된 초대 코드입니다.",
            )
    return invite


# @MX:NOTE: [AUTO] 초대코드는 1회용이다(첫 성공 가입이 소비). 초대 행 잠금(FOR UPDATE)
#            → 검증 → 유저 + MANAGER 멤버십 생성 → 초대 삭제를 한 트랜잭션으로 커밋한다.
#            같은 코드의 동시 가입은 잠금에서 기다린 뒤 삭제된 행을 못 찾아 400 이다.
#            가입이 실패해 롤백되면 삭제도 취소되어 코드가 남는다.
# 회원 생성
def create_user_service(db: Session, user_create: UserCreate) -> UserResponse:
    # 권한 상승 방지: role 은 클라이언트가 아니라 서버가 결정한다.
    role = resolve_signup_role(user_create)

    # 1. 유저 생성 유효성 검사
    invite = validate_user_creation(role, user_create.invite_code, db)

    # 3. 유저 생성 준비 (클라이언트 페이로드에 role 이 없으므로 서버가 주입)
    user_data = user_create.model_dump(exclude={"invite_code"})
    user_data["role"] = role
    user_data["password"] = hash_password(user_create.password)

    try:
        user = create_user(db, user_data)

        # 4. MANAGER라면 shop_user 등록
        if role == UserRole.MANAGER:
            shop_user_data = ShopUser(
                shop_id=invite.shop_id,
                user_id=user.id,
                is_primary_owner=0,
            )
            create_shop_user(
                db,
                shop_user_data,
            )
            # 초대코드는 1회용: 가입과 같은 트랜잭션에서 잠근 초대 행을 지운다.
            delete_invite(db, invite)

        db.commit()
        db.refresh(user)

    except IntegrityError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_409_CONFLICT,
            domain=DOMAIN,
            exception=e,
        ) from e
    except Exception as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e

    return UserResponse.model_validate(user)


# 회원 수정
def update_user_service(
    db: Session,
    user_update: UserUpdate,
    current_user: User,
) -> UserResponse:
    try:
        # 권한 상승 방지: 자기 정보 수정 경로에서는 role 을 변경할 수 없다.
        # UserUpdate 스키마에 role 이 없고, update_user_db 화이트리스트로도 이중
        # 차단된다.
        user = get_user_by_id(db, current_user.id)
        if not user or user.is_deleted():
            raise CustomException(status_code=status.HTTP_404_NOT_FOUND, domain=DOMAIN)

        # 커밋 뒤 만료된 인스턴스를 다시 읽지 않도록 id 를 먼저 보관한다.
        user_id = user.id
        user_data = user_update.model_dump(exclude_unset=True)

        # 비밀번호 해시 처리
        if user_data.get("password"):
            user_data["password"] = hash_password(user_data["password"])

        # 사용자 정보 업데이트 (CRUD 는 setattr 까지만; 커밋 경계는 서비스가 소유)
        # SPEC-FIX-001 REQ-FIX-005: update_user_db 내부 db.commit() 을 서비스로 이관.
        updated_user = update_user_db(db, user, user_data)
        db.commit()

        # 세션 · 캐시 정리는 커밋이 확정된 뒤에만 한다(실패 · 롤백 시 그대로 둔다).
        # - 리프레시 토큰: 기존처럼 정보를 수정하면 항상 폐기한다.
        # - 유저 캐시: 남기면 바뀐 role/email/name 이 캐시 TTL(24h) 동안 옛 값이다.
        clear_refresh_token_redis(user_id)
        clear_user_redis(user_id)

        db.refresh(updated_user)
        return UserResponse.model_validate(updated_user)
    except IntegrityError as e:
        raise CustomException(
            status_code=status.HTTP_409_CONFLICT,
            domain=DOMAIN,
            exception=e,
        ) from e
    except CustomException:
        raise
    except Exception as e:
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


def delete_user_service(
    db: Session,
    current_user: User,
    is_soft_delete: bool = False,
) -> None:
    """현재 로그인한 사용자를 삭제합니다.

    세션 무효화(유저 캐시 · 리프레시 토큰 삭제)는 커밋이 성공한 뒤에만 한다.
    - 커밋 전에 지우면, 커밋 실패 시 유저는 남았는데 세션만 사라진다.
    - 유저 캐시를 남기면 캐시에 deleted_at 이 없어 삭제된 유저가 캐시 TTL 동안
      get_current_user 를 통과한다.

    소프트삭제는 디바이스 푸시 토큰을 같은 트랜잭션에서(커밋 전) 비활성화한다.
    강제 삭제는 device_push_tokens.user_id FK(ON DELETE CASCADE)로 토큰도 지워진다.
    """
    user = get_user_by_id(db, current_user.id)
    if not user:
        raise CustomException(
            status_code=status.HTTP_404_NOT_FOUND,
            domain=DOMAIN,
            hint="없슈.",
        )

    # 삭제·커밋 뒤 만료/분리된 인스턴스를 다시 읽지 않도록 id 를 먼저 보관한다.
    user_id = user.id

    try:
        delete_user_db(
            db=db,
            user=user,
            is_soft_delete=is_soft_delete,
        )
        if is_soft_delete:
            # 소프트삭제는 토큰 행이 남으므로 탈퇴 유저에게 푸시가 가지 않게 끈다.
            deactivate_device_tokens_by_user(db, user_id)
        db.commit()
    except SQLAlchemyError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e

    # 커밋이 확정된 뒤에만 세션을 끊는다. 유저 캐시를 먼저 지워 get_current_user 가
    # 곧바로 DB 폴백(deleted_at 필터)으로 401 을 내게 한다.
    clear_user_redis(user_id)
    clear_refresh_token_redis(user_id)


def check_user_email_service(db: Session, email: str) -> UserEmailCheckResponse:
    """이메일 중복 체크 서비스."""
    exists = None
    message = None
    user = get_user_by_email(db, email=email)
    if user:
        exists = True
        message = "이미 존재하는 이메일입니다."
    else:
        exists = False
        message = "사용 가능한 이메일입니다."
    return UserEmailCheckResponse(exists=exists, message=message)
