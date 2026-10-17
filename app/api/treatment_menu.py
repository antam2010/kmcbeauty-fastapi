from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.shop import get_current_shop
from app.docs.common_responses import COMMON_ERROR_RESPONSES
from app.models.shop import Shop
from app.schemas.pagination import Page
from app.schemas.treatment_menu import (
    TreatmentMenuCreate,
    TreatmentMenuCreateResponse,
    TreatmentMenuDetailCreate,
    TreatmentMenuDetailResponse,
    TreatmentMenuFilter,
    TreatmentMenuResponse,
)
from app.services.treatment_menu_service import (
    create_treatment_menu_detail_service,
    create_treatment_menu_service,
    delete_treatment_menu_detail_service,
    delete_treatment_menu_service,
    get_treatment_menu_detail_service,
    get_treatment_menus_service,
    restore_treatment_menu_service,
)

router = APIRouter(prefix="/treatment-menus", tags=["시술 메뉴"])


# 시술 메뉴 조회
@router.get(
    "",
    response_model=Page[TreatmentMenuResponse],
    summary="시술 메뉴 목록 조회",
    description="시술 메뉴 목록을 조회합니다.",
    status_code=status.HTTP_200_OK,
)
def get_menus(
    filters: TreatmentMenuFilter = Depends(),
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> Page[TreatmentMenuResponse]:
    return get_treatment_menus_service(
        db=db,
        current_shop=current_shop,
        filters=filters,
    )


# 시술 메뉴 생성
@router.post(
    "",
    response_model=TreatmentMenuCreateResponse,
    summary="시술 메뉴 생성",
    description="시술 메뉴를 생성합니다.",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
        status.HTTP_409_CONFLICT: COMMON_ERROR_RESPONSES[status.HTTP_409_CONFLICT],
    },
)
def create_menu(
    params: TreatmentMenuCreate,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> TreatmentMenuCreateResponse:
    return create_treatment_menu_service(
        db=db,
        params=params,
        current_shop=current_shop,
    )


# 시술 메뉴 수정
@router.put(
    "/{menu_id}",
    response_model=TreatmentMenuCreateResponse,
    summary="시술 메뉴 수정",
    description="시술 메뉴를 수정합니다.",
    status_code=status.HTTP_200_OK,
)
def update_menu(
    menu_id: int,
    params: TreatmentMenuCreate,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> TreatmentMenuCreateResponse:
    return create_treatment_menu_service(
        db=db,
        params=params,
        current_shop=current_shop,
        menu_id=menu_id,
    )


# 시술 메뉴 삭제
@router.delete(
    "/{menu_id}",
    response_model=None,
    summary="시술 메뉴 삭제",
    description="시술 메뉴를 삭제합니다.",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_menu(
    menu_id: int,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> None:
    return delete_treatment_menu_service(
        db=db,
        current_shop=current_shop,
        menu_id=menu_id,
    )


# 시술 메뉴 복구
@router.post(
    "/{menu_id}/restore",
    response_model=None,
    summary="시술 메뉴 복구",
    description="시술 메뉴를 복구합니다.",
    status_code=status.HTTP_204_NO_CONTENT,
)
def restore_menu(
    menu_id: int,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> None:
    return restore_treatment_menu_service(
        db=db,
        current_shop=current_shop,
        menu_id=menu_id,
    )


# 시술 메뉴 상세 조회
@router.get(
    "/{menu_id}/details",
    response_model=list[TreatmentMenuDetailResponse],
    summary="시술 메뉴 상세 조회",
    description="시술 메뉴 상세를 조회합니다.",
    status_code=status.HTTP_200_OK,
)
def get_menu_detail(
    menu_id: int,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> list[TreatmentMenuDetailResponse]:
    return get_treatment_menu_detail_service(
        menu_id=menu_id,
        current_shop=current_shop,
        db=db,
    )


# 시술 메뉴 상세 생성
@router.post(
    "/{menu_id}/details",
    response_model=TreatmentMenuDetailResponse,
    summary="시술 메뉴 상세 생성",
    description="시술 메뉴 상세를 생성합니다.",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def create_menu_detail(
    menu_id: int,
    filters: TreatmentMenuDetailCreate,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> TreatmentMenuDetailResponse:
    return create_treatment_menu_detail_service(
        menu_id=menu_id,
        current_shop=current_shop,
        filters=filters,
        db=db,
    )


# 시술 메뉴 상세 수정
@router.put(
    "/{menu_id}/details/{detail_id}",
    response_model=TreatmentMenuDetailResponse,
    summary="시술 메뉴 상세 수정",
    description="시술 메뉴 상세를 수정합니다.",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def update_menu_detail(
    menu_id: int,
    detail_id: int,
    params: TreatmentMenuDetailCreate,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> TreatmentMenuDetailResponse:
    return create_treatment_menu_detail_service(
        menu_id=menu_id,
        current_shop=current_shop,
        filters=params,
        db=db,
        detail_id=detail_id,
    )


# 시술 메뉴 상세 삭제
@router.delete(
    "/{menu_id}/details/{detail_id}",
    response_model=None,
    summary="시술 메뉴 상세 삭제",
    description="시술 메뉴 상세를 삭제합니다.",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_menu_detail(
    menu_id: int,
    detail_id: int,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> None:
    return delete_treatment_menu_detail_service(
        menu_id=menu_id,
        detail_id=detail_id,
        current_shop=current_shop,
        db=db,
    )
