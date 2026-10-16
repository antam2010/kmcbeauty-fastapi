import logging
from datetime import UTC, datetime

from fastapi import status
from fastapi_pagination import Page
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crud.treatment_menu_crud import (
    create_treatment_menu,
    create_treatment_menu_detail,
    get_menu_by_id,
    get_menu_detail_by_id,
    get_treatment_menu_details_by_user,
    get_treatment_menus_by_user,
)
from app.exceptions import CustomException
from app.models.shop import Shop
from app.models.treatment_menu import TreatmentMenu
from app.models.treatment_menu_detail import TreatmentMenuDetail
from app.schemas.treatment_menu import (
    TreatmentMenuCreate,
    TreatmentMenuCreateResponse,
    TreatmentMenuDetailCreate,
    TreatmentMenuDetailResponse,
    TreatmentMenuFilter,
)

DOMAIN = "TREATMENT_MENU"


def get_treatment_menus_service(
    db: Session,
    current_shop: Shop,
    filters: TreatmentMenuFilter,
) -> Page[TreatmentMenu]:
    """시술 메뉴 목록 조회 서비스."""
    return get_treatment_menus_by_user(
        db=db,
        shop_id=current_shop.id,
        search=filters.search,
    )


# 시술 메뉴 생성 및 수정 서비스
def create_treatment_menu_service(
    db: Session,
    current_shop: Shop,
    params: TreatmentMenuCreate,
    menu_id: int | None = None,
) -> TreatmentMenuCreateResponse:
    try:
        if menu_id:
            # 시술 메뉴 수정
            menu = get_menu_by_id(
                db=db,
                menu_id=menu_id,
                shop_id=current_shop.id,
                exclude_deleted=True,
            )
            if not menu:
                raise CustomException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    domain=DOMAIN,
                    hint=(
                        "시술 메뉴를 찾을 수 없거나 다른 상점의 메뉴이거나 "
                        "삭제된 메뉴입니다."
                    ),
                )

            menu.name = params.name
            menu.updated_at = datetime.now(UTC)

        else:
            # 시술 메뉴 생성
            menu = create_treatment_menu(
                db=db,
                name=params.name,
                shop_id=current_shop.id,
            )
            db.add(menu)

        db.commit()
        db.refresh(menu)

    except IntegrityError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_409_CONFLICT,
            domain=DOMAIN,
            hint="이미 존재하는 시술 메뉴입니다.",
            exception=e,
        ) from e
    except CustomException:
        db.rollback()
        raise

    except Exception as e:
        db.rollback()
        logging.exception("SQLAlchemyError")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e

    return TreatmentMenuCreateResponse.model_validate(menu)


# 시술 메뉴 삭제 서비스


def delete_treatment_menu_service(
    db: Session,
    current_shop: Shop,
    menu_id: int,
) -> None:
    """시술 메뉴 삭제 서비스."""
    try:
        menu = get_menu_by_id(
            db=db,
            menu_id=menu_id,
            shop_id=current_shop.id,
            exclude_deleted=True,
        )

        if not menu:
            raise CustomException(
                status_code=status.HTTP_404_NOT_FOUND,
                domain=DOMAIN,
                hint="삭제할 시술 메뉴를 찾을 수 없거나 다른 상점의 메뉴입니다.",
            )

        menu.deleted_at = datetime.now(UTC)

        db.commit()

    except CustomException:
        db.rollback()
        raise

    except Exception as e:
        db.rollback()
        logging.exception("Treatment menu 삭제 중 오류")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
        ) from e


# 시술 메뉴 복구 서비스
def restore_treatment_menu_service(
    db: Session,
    current_shop: Shop,
    menu_id: int,
) -> None:
    """시술 메뉴 복구 서비스."""
    try:
        menu = get_menu_by_id(
            db=db,
            menu_id=menu_id,
            shop_id=current_shop.id,
            exclude_deleted=False,
        )

        if not menu:
            raise CustomException(
                status_code=status.HTTP_404_NOT_FOUND,
                domain=DOMAIN,
                hint="복구할 시술 메뉴를 찾을 수 없거나 다른 상점의 메뉴입니다.",
            )

        menu.deleted_at = None

        db.commit()

    except CustomException:
        db.rollback()
        raise

    except Exception as e:
        db.rollback()
        logging.exception("Treatment menu 복구 중 오류")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
        ) from e


# 시술 메뉴 상세 조회 서비스
def get_treatment_menu_detail_service(
    menu_id: int,
    current_shop: Shop,
    db: Session,
) -> TreatmentMenuDetailResponse:
    return get_treatment_menu_details_by_user(
        db=db,
        menu_id=menu_id,
        shop_id=current_shop.id,
    )


# 시술 메뉴 상세 항목 생성 서비스
def create_treatment_menu_detail_service(
    menu_id: int,
    current_shop: Shop,
    filters: TreatmentMenuDetailCreate,
    db: Session,
    detail_id: int | None = None,
) -> TreatmentMenuDetail:
    try:
        if detail_id:
            # 시술 메뉴 상세 수정
            menu_detail = get_menu_detail_by_id(
                db=db,
                menu_id=menu_id,
                detail_id=detail_id,
                shop_id=current_shop.id,
            )
            if not menu_detail:
                raise CustomException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    domain=DOMAIN,
                    hint=(
                        "시술 메뉴 상세를 찾을 수 없거나 다른 상점의 메뉴이거나 "
                        "삭제된 메뉴입니다."
                    ),
                )

            menu_detail.name = filters.name
            menu_detail.duration_min = filters.duration_min
            menu_detail.base_price = filters.base_price
        else:
            # 상위 메뉴 소유권 검증: 현재 샵의 삭제되지 않은 메뉴에만 상세를 추가한다.
            # 타 샵 메뉴/없는 메뉴/삭제된 메뉴를 구분하지 않고 같은 404 를 준다.
            menu = get_menu_by_id(
                db=db,
                menu_id=menu_id,
                shop_id=current_shop.id,
                exclude_deleted=True,
            )
            if not menu:
                raise CustomException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    domain=DOMAIN,
                    hint=(
                        "시술 메뉴를 찾을 수 없거나 다른 상점의 메뉴이거나 "
                        "삭제된 메뉴입니다."
                    ),
                )

            # 시술 메뉴 상세 생성 (CRUD 가 add/flush 까지 수행; 커밋 경계는 서비스 소유)
            # SPEC-FIX-001 REQ-FIX-005: CRUD 내부 db.commit() 을 서비스로 이관.
            menu_detail = create_treatment_menu_detail(
                db=db,
                menu_id=menu_id,
                name=filters.name,
                duration_min=filters.duration_min,
                base_price=filters.base_price,
            )

        db.commit()
        db.refresh(menu_detail)

    except CustomException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logging.exception("SQLAlchemyError")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e
    return TreatmentMenuDetailResponse.model_validate(menu_detail)


# 시술 메뉴 상세 항목 삭제 서비스
def delete_treatment_menu_detail_service(
    db: Session,
    current_shop: Shop,
    menu_id: int,
    detail_id: int,
) -> None:
    """시술 메뉴 상세 삭제 서비스."""
    try:
        menu_detail = get_menu_detail_by_id(
            db=db,
            menu_id=menu_id,
            detail_id=detail_id,
            shop_id=current_shop.id,
        )

        if not menu_detail:
            raise CustomException(
                status_code=status.HTTP_404_NOT_FOUND,
                domain=DOMAIN,
                hint=(
                    "삭제할 시술 메뉴 상세를 찾을 수 없거나 다른 상점의 메뉴 이거나 "
                    "삭제된 메뉴입니다."
                ),
            )

        menu_detail.deleted_at = datetime.now(UTC)
        db.commit()

    except CustomException:
        db.rollback()
        raise

    except Exception as e:
        db.rollback()
        logging.exception("Treatment menu 상세 삭제 중 오류")
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
        ) from e
