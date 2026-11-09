import redis
from fastapi import status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.crud.device_push_token_crud import deactivate_device_tokens_by_user
from app.crud.shop_invite_curd import delete_invite, get_invite_by_code
from app.crud.shop_user_crud import (
    ShopUser,
    count_active_primary_owners,
    count_other_active_members,
    count_other_primary_owners,
    create_shop_user,
    get_primary_owner_shop_ids,
    lock_primary_owners,
)
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
# 탈퇴하면 직원이 있는 샵에 활성 대표원장이 남지 않을 때의 409 문구.
LAST_OWNER_DELETION_DETAIL = (
    "직원이 있는 샵의 마지막 대표원장은 탈퇴할 수 없습니다. "
    "다른 대표원장을 먼저 지정하세요."
)
# 초대한 샵에 활성 대표원장이 없을 때(원장 탈퇴 뒤 남은 초대코드)의 가입 409 문구.
NO_OWNER_INVITE_DETAIL = "초대한 샵에 대표원장이 없어 가입할 수 없습니다."


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


# @MX:WARN: [AUTO] 초대 가입 가드 — 초대 행 → 대표원장 행 순서로 잠근다.
# @MX:REASON: 생성자의 강제 탈퇴는 반대 순서(대표원장 행 → 샵 삭제 → 초대 행 cascade)라
#             같은 샵에서 동시에 일어나면 교착할 수 있다. PostgreSQL 이 deadlock_timeout
#             (약 1초) 뒤 한쪽을 중단해 그 요청은 500 이고, 무결성 영향은 없다
#             (수용한 한계).
def _ensure_invite_shop_has_owner(db: Session, shop_id: int) -> None:
    """초대한 샵에 활성 대표원장이 있는지 확인한다(초대 행을 잠근 뒤 호출).

    그 샵의 대표원장 행을 잠그고(lock_primary_owners) 활성 대표원장(탈퇴 유저 제외)을
    센다. 0명이면 409 다 — 혼자인 원장이 초대코드를 남기고 탈퇴한 샵에 멤버만 생기는
    것을 막는다. 탈퇴 가드와 같은 잠금이라 가입과 탈퇴가 직렬화되고, 기다린 쪽은
    커밋된 상태로 다시 센다(PostgreSQL READ COMMITTED).

    :raises CustomException: 409(초대한 샵에 활성 대표원장이 없음)
    """
    lock_primary_owners(db, shop_id)
    if count_active_primary_owners(db, shop_id) == 0:
        raise CustomException(
            status_code=status.HTTP_409_CONFLICT,
            domain=DOMAIN,
            detail=NO_OWNER_INVITE_DETAIL,
        )


# @MX:NOTE: [AUTO] 초대코드는 1회용이다(첫 성공 가입이 소비). 초대 행 잠금(FOR UPDATE)
#            → 검증 → 대표원장 행 잠금 · 활성 대표원장 확인 → 유저 + MANAGER 멤버십 생성
#            → 초대 삭제를 한 트랜잭션으로 커밋한다. 같은 코드의 동시 가입은 잠금에서
#            기다린 뒤 삭제된 행을 못 찾아 400 이다. 가입이 실패해 롤백되면 삭제도
#            취소되어 코드가 남는다.
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
        if invite is not None:
            # 초대 행 잠금(validate_user_creation) 뒤 그 샵의 대표원장 행을 잠그고 센다.
            # 혼자인 원장이 초대코드를 남기고 탈퇴한 샵에 멤버만 생기는 것을 막는다.
            _ensure_invite_shop_has_owner(db, invite.shop_id)

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
    except CustomException:
        # 409(초대한 샵에 대표원장 없음): 롤백으로 잠금을 풀고(초대코드는 그대로 남는다)
        # 아래 except Exception 의 500 으로 감싸지 않고 올린다.
        db.rollback()
        raise
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
    except redis.RedisError:
        # 커밋 뒤 세션 · 캐시 정리의 Redis 장애: 전역 처리기(app/main.py)가 503 으로
        # 바꾼다(아래 500 으로 감싸지 않는다).
        raise
    except Exception as e:
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


# @MX:WARN: [AUTO] 탈퇴 가드 — 샵마다 대표원장 행을 잠근 뒤에 센다.
# @MX:REASON: 잠그지 않고 세면 탈퇴와 다른 대표원장의 해제 · 강등이 동시에 통과해 직원은
#             있는데 관리할 사람이 없는 샵이 된다. 잠금(lock_primary_owners)이
#             shop_user_service 의 해제 · 강등 가드와 같아 둘이 직렬화된다.
def _ensure_staffed_shops_keep_owner(db: Session, user_id: int) -> None:
    """탈퇴해도 직원이 있는 샵에 활성 대표원장이 남는지 확인한다.

    user_id 가 대표원장인 샵마다(shop_id 순) 대표원장 행을 잠그고 다른 활성 대표원장을
    센다. 없을 때만 다른 활성 멤버(역할 무관, 탈퇴 유저 제외)를 세어 있으면 409 다.
    다른 멤버가 없는 샵은 막지 않는다 — 샵 삭제 API 가 없어 혼자인 원장도 탈퇴할 수
    있어야 한다(연결 해제 · 강등 가드보다 느슨하다).

    :raises CustomException: 409(직원이 있는 샵의 마지막 대표원장)
    """
    for shop_id in get_primary_owner_shop_ids(db, user_id):
        lock_primary_owners(db, shop_id)
        if count_other_primary_owners(db, shop_id, user_id) > 0:
            continue
        if count_other_active_members(db, shop_id, user_id) > 0:
            raise CustomException(
                status_code=status.HTTP_409_CONFLICT,
                domain=DOMAIN,
                detail=LAST_OWNER_DELETION_DETAIL,
            )


def delete_user_service(
    db: Session,
    current_user: User,
    is_soft_delete: bool = False,
) -> None:
    """현재 로그인한 사용자를 삭제합니다.

    직원이 있는 샵의 마지막 활성 대표원장이면 두 모드(소프트 · 강제) 모두 409 다
    (_ensure_staffed_shops_keep_owner, 삭제 전 같은 트랜잭션에서 잠근 뒤 센다).

    세션 무효화(유저 캐시 · 리프레시 토큰 삭제)는 커밋이 성공한 뒤에만 한다.
    - 커밋 전에 지우면, 커밋 실패 시 유저는 남았는데 세션만 사라진다.
    - 유저 캐시를 남기면 캐시에 deleted_at 이 없어 삭제된 유저가 캐시 TTL 동안
      get_current_user 를 통과한다.

    소프트삭제는 디바이스 푸시 토큰을 같은 트랜잭션에서(커밋 전) 비활성화한다.
    강제 삭제는 device_push_tokens.user_id FK(ON DELETE CASCADE)로 토큰도 지워진다.

    :raises CustomException: 404(유저 없음), 409(직원이 있는 샵의 마지막 대표원장), 500
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
        _ensure_staffed_shops_keep_owner(db, user_id)
        delete_user_db(
            db=db,
            user=user,
            is_soft_delete=is_soft_delete,
        )
        if is_soft_delete:
            # 소프트삭제는 토큰 행이 남으므로 탈퇴 유저에게 푸시가 가지 않게 끈다.
            deactivate_device_tokens_by_user(db, user_id)
        db.commit()
    except CustomException:
        # 409(마지막 대표원장): 롤백으로 잠금을 바로 풀고 그대로 올린다.
        db.rollback()
        raise
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
