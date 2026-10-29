from fastapi import status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.crud.shop_user_crud import (
    ShopUser,
    create_shop_user,
    delete_shop_user,
    get_shop_user,
    get_shop_user_with_user,
    get_shop_users_by_shop_id,
)
from app.crud.user_crud import get_user_by_email
from app.exceptions import CustomException
from app.models.user import User
from app.schemas.shop_user import (
    ShopUserAssociateRequest,
    ShopUserAssociateUpdateRequest,
    ShopUserUserResponse,
)

DOMAIN = "SHOP_USER"


def _require_primary_owner(db: Session, shop_id: int, user_id: int) -> ShopUser:
    """쓰기(연결/수정/삭제) 권한 게이트.

    shop_invite_service 와 동일한 관례를 재사용한다: 요청자가 해당 샵의 멤버이며
    대표원장(is_primary_owner)일 때만 통과. 새로운 인가 로직을 발명하지 않는다
    (권한 정책 자체는 SPEC-SECURITY-001 소관, 본 SPEC 은 계약 형태만 다룸).

    :raises CustomException: 403(멤버 아님 또는 대표원장 아님)
    :return: 요청자의 ShopUser 매핑
    """
    requester = get_shop_user(db, shop_id, user_id)
    if not requester or not requester.is_primary_owner:
        raise CustomException(
            status_code=status.HTTP_403_FORBIDDEN,
            domain=DOMAIN,
        )
    return requester


def get_shop_users_service(
    db: Session,
    shop_id: int,
    current_user: User,
) -> list[ShopUserUserResponse]:
    """샵 목록 조회 서비스.

    :param db: DB 세션
    :param user: 현재 로그인한 유저
    :return: 샵 목록
    """
    shop_user = get_shop_user(
        db=db,
        shop_id=shop_id,
        user_id=current_user.id,
    )
    if not shop_user:
        raise CustomException(
            status_code=status.HTTP_403_FORBIDDEN,
            domain=DOMAIN,
            detail="Access Denied",
        )
    try:
        return get_shop_users_by_shop_id(db=db, shop_id=shop_id)
    except SQLAlchemyError as e:
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            detail="DB Error",
            exception=e,
        ) from e
    except Exception as e:
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


# @MX:WARN: [AUTO] 직원(샵 유저) 쓰기 계약 — 권한 경계 인접 지점.
# @MX:REASON: 이 3개 서비스(연결/수정/삭제)는 SPEC-SECURITY-001 의 인가 정책과
#             맞닿아 있다. 여기서는 기존 관례(_require_primary_owner = 멤버십 +
#             대표원장)만 재사용하고 role/권한 상승 관련 로직은 절대 추가하지 않는다.
#             요청 스키마도 role 을 받지 않는다(권한 상승 차단).
def create_shop_user_service(
    db: Session,
    shop_id: int,
    current_user: User,
    payload: ShopUserAssociateRequest,
) -> ShopUserUserResponse:
    """샵에 기존 유저를 연결(멤버십 부여)하는 서비스.

    email 로 대상 유저를 식별해 shop-user 매핑을 생성한다. 유저 계정 생성이나
    role 결정은 하지 않는다(SECURITY-001 소관). 이미 연결된 유저는 409.

    :raises CustomException: 403(권한 없음), 404(대상 유저 없음), 409(이미 연결), 500
    """
    _require_primary_owner(db, shop_id, current_user.id)

    target = get_user_by_email(db, payload.email)
    if not target:
        raise CustomException(
            status_code=status.HTTP_404_NOT_FOUND,
            domain=DOMAIN,
            detail="연결할 유저를 찾을 수 없습니다.",
        )

    if get_shop_user(db, shop_id, target.id):
        raise CustomException(
            status_code=status.HTTP_409_CONFLICT,
            domain=DOMAIN,
            detail="이미 샵에 연결된 유저입니다.",
        )

    try:
        create_shop_user(
            db,
            ShopUser(
                shop_id=shop_id,
                user_id=target.id,
                is_primary_owner=payload.is_primary_owner,
            ),
        )
        db.commit()
        created = get_shop_user_with_user(db, shop_id, target.id)
        return ShopUserUserResponse.model_validate(created)
    except CustomException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            detail="DB Error",
            exception=e,
        ) from e
    except Exception as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


def update_shop_user_service(
    db: Session,
    shop_id: int,
    user_id: int,
    current_user: User,
    payload: ShopUserAssociateUpdateRequest,
) -> ShopUserUserResponse:
    """샵-유저 연결(멤버십) 수정 서비스.

    현재는 대표원장 여부(is_primary_owner)만 갱신한다. role 등 권한 필드는 다루지
    않는다(SECURITY-001 소관).

    :raises CustomException: 403(권한 없음), 404(연결 없음), 500
    """
    _require_primary_owner(db, shop_id, current_user.id)

    shop_user = get_shop_user_with_user(db, shop_id, user_id)
    if not shop_user:
        raise CustomException(
            status_code=status.HTTP_404_NOT_FOUND,
            domain=DOMAIN,
            detail="샵에 연결된 유저를 찾을 수 없습니다.",
        )

    try:
        shop_user.is_primary_owner = payload.is_primary_owner
        db.commit()
        db.refresh(shop_user)
        return ShopUserUserResponse.model_validate(shop_user)
    except CustomException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            detail="DB Error",
            exception=e,
        ) from e
    except Exception as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


def delete_shop_user_service(
    db: Session,
    shop_id: int,
    user_id: int,
    current_user: User,
) -> None:
    """샵-유저 연결(멤버십) 삭제 서비스.

    ShopUser 는 연결 테이블(SoftDeleteMixin 없음)이므로 매핑만 제거한다. 유저 계정은
    유지된다. 자기 자신의 마지막 대표원장 연결 해제 등 정책 판단은 SECURITY-001 소관.

    :raises CustomException: 403(권한 없음), 404(연결 없음), 500
    """
    _require_primary_owner(db, shop_id, current_user.id)

    shop_user = get_shop_user(db, shop_id, user_id)
    if not shop_user:
        raise CustomException(
            status_code=status.HTTP_404_NOT_FOUND,
            domain=DOMAIN,
            detail="샵에 연결된 유저를 찾을 수 없습니다.",
        )

    try:
        delete_shop_user(db, shop_user)
        db.commit()
    except CustomException:
        db.rollback()
        raise
    except SQLAlchemyError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            detail="DB Error",
            exception=e,
        ) from e
    except Exception as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e
