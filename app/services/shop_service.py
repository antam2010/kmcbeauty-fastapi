from fastapi import status
from fastapi_pagination import Page
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.crud.shop_crud import (
    create_shop,
    get_member_shop_by_id,
    get_member_shops,
    get_user_shop_by_id,
)
from app.crud.shop_user_crud import ShopUser, create_shop_user
from app.exceptions import CustomException
from app.models.shop import Shop
from app.models.user import User
from app.schemas.shop import ShopCreate
from app.utils.redis.shop import (
    clear_selected_shop_redis,
    get_selected_shop_redis,
    set_selected_shop_redis,
)

DOMAIN = "SHOP"


def upsert_shop_service(
    db: Session,
    user: User,
    shop_data: ShopCreate,
    shop_id: int | None = None,
) -> Shop:
    """샵 생성 및 수정 서비스.

    :param shop_id: 샵 ID
    :param db: DB 세션
    :param user: 현재 로그인한 유저
    :param shop_data: 샵 생성 데이터
    :return: 생성된 샵 객체
    """
    if user.role != "MASTER":
        raise CustomException(
            status_code=status.HTTP_403_FORBIDDEN,
            domain=DOMAIN,
            hint="원장만 샵을 생성하거나 수정할 수 있지비",
        )
    try:
        if shop_id is None:
            shop = create_shop(db, shop_data, user.id)
            shop_user_data = ShopUser(
                shop_id=shop.id,
                user_id=user.id,
                is_primary_owner=1,
            )
            shop_user = create_shop_user(db, shop_user_data)

            db.commit()
            db.refresh(shop)
            db.refresh(shop_user)
        else:
            # 샵 수정은 관리 경로다. 멤버십이 아니라 생성자(Shop.user_id)만 허용한다.
            shop = get_user_shop_by_id(db, user.id, shop_id)
            if not shop:
                raise CustomException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    domain=DOMAIN,
                )

            for key, value in shop_data.model_dump().items():
                setattr(shop, key, value)
            db.commit()
            db.refresh(shop)
    except CustomException:
        # 404 등 의도한 거절은 그대로 전파한다.
        # 아래 except Exception 에 잡히면 500 으로 바뀐다.
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
            detail="Unknown Error",
            domain=DOMAIN,
            exception=e,
        ) from e

    return shop


def get_my_shops_service(db: Session, user: User) -> Page[Shop]:
    """샵 목록 조회 서비스.

    생성자 여부와 무관하게 유저가 멤버(shop_user)로 속한 활성 샵을 준다.

    :param db: DB 세션
    :param user: 현재 로그인한 유저
    :return: 샵 목록
    """
    try:
        return get_member_shops(db, user.id)
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


def set_selected_shop_service(db: Session, user: User, shop_id: int) -> None:
    """샵 선택 서비스.

    멤버(shop_user)인 활성 샵만 선택할 수 있다. 비멤버는 404.

    :param db: DB 세션
    :param user: 현재 로그인한 유저
    :param shop_id: 선택할 샵 ID
    :return: 선택된 샵 객체
    """
    try:
        shop = get_member_shop_by_id(db, user.id, shop_id)
        if not shop:
            raise CustomException(status_code=status.HTTP_404_NOT_FOUND, domain=DOMAIN)
        set_selected_shop_redis(user.id, shop.id)
    except CustomException:
        raise
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
            detail="Unknown Error",
            exception=e,
        ) from e


def delete_selected_shop_service(user: User) -> None:
    """샵 선택 삭제 서비스.

    :param user: 현재 로그인한 유저
    :return: 선택된 샵 객체
    """
    try:
        shop_id = get_selected_shop_redis(user.id)
        if not shop_id:
            raise CustomException(
                status_code=status.HTTP_404_NOT_FOUND,
                domain=DOMAIN,
                code="NOT_SELECTED",
            )

        clear_selected_shop_redis(user.id)

    except CustomException:
        raise
    except Exception as e:
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
        ) from e


def get_selected_shop_service(db: Session, user: User) -> Shop:
    """샵 선택 조회 서비스.

    :param db: DB 세션
    :param user: 현재 로그인한 유저
    :return: 선택된 샵 객체
    """
    try:
        shop_id = get_selected_shop_redis(user.id)
        if not shop_id:
            raise CustomException(
                status_code=status.HTTP_404_NOT_FOUND,
                domain=DOMAIN,
                code="NOT_SELECTED",
            )

        # 선택값(Redis)은 신뢰하지 않고 멤버십을 다시 확인한다.
        shop = get_member_shop_by_id(db, user.id, shop_id)
        if shop:
            return shop

        raise CustomException(
            status_code=status.HTTP_404_NOT_FOUND,
            domain=DOMAIN,
        )

    except CustomException:
        raise
    except Exception as e:
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
        ) from e
