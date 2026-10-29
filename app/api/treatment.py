from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.shop import get_current_shop
from app.docs.common_responses import COMMON_ERROR_RESPONSES
from app.models.shop import Shop
from app.models.user import User
from app.schemas.pagination import Page
from app.schemas.treatment import (
    TreatmentCreate,
    TreatmentFilter,
    TreatmentResponse,
    TreatmentSimpleResponse,
    TreatmentUpdate,
)
from app.services.treatment_service import (
    cancel_treatment_service,
    get_treatment_list_service,
    upsert_treatment_service,
)

router = APIRouter(prefix="/treatments", tags=["시술 예약"])


@router.get(
    "",
    response_model=Page[TreatmentResponse],
    summary="시술 예약 목록 조회",
    description="시술 예약 목록을 조회합니다.",
    status_code=status.HTTP_200_OK,
)
def list_treatments_api(
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
    filters: TreatmentFilter = Depends(),
) -> Page[TreatmentResponse]:
    return get_treatment_list_service(
        db=db,
        current_shop=current_shop,
        filters=filters,
    )


@router.post(
    "",
    response_model=TreatmentSimpleResponse,
    summary="시술 예약 생성",
    description="시술 예약을 생성합니다.",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: COMMON_ERROR_RESPONSES[
            status.HTTP_400_BAD_REQUEST
        ],
        status.HTTP_500_INTERNAL_SERVER_ERROR: COMMON_ERROR_RESPONSES[
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ],
    },
)
def create_treatment_api(
    data: TreatmentCreate,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
    current_user: User = Depends(get_current_user),
) -> TreatmentSimpleResponse:
    return upsert_treatment_service(
        data=data,
        db=db,
        current_shop=current_shop,
        current_user=current_user,
    )


@router.put(
    "/{treatment_id}",
    response_model=TreatmentSimpleResponse,
    summary="시술 예약 수정",
    description="시술 예약을 수정합니다.",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: COMMON_ERROR_RESPONSES[
            status.HTTP_400_BAD_REQUEST
        ],
        status.HTTP_500_INTERNAL_SERVER_ERROR: COMMON_ERROR_RESPONSES[
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ],
    },
)
def update_treatment_api(
    treatment_id: int,
    data: TreatmentUpdate,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
    current_user: User = Depends(get_current_user),
) -> TreatmentSimpleResponse:
    return upsert_treatment_service(
        data=data,
        db=db,
        current_shop=current_shop,
        current_user=current_user,
        treatment_id=treatment_id,
    )


@router.delete(
    "/{treatment_id}",
    response_model=None,
    summary="시술 예약 삭제",
    description=(
        "시술 예약을 삭제합니다. Treatment 는 소프트삭제 컬럼이 없어 상태를 "
        "CANCELLED 로 전환해 이력을 보존합니다. 이미 취소된 예약은 멱등하게 204."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
        status.HTTP_500_INTERNAL_SERVER_ERROR: COMMON_ERROR_RESPONSES[
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ],
    },
)
def delete_treatment_api(
    treatment_id: int,
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> None:
    return cancel_treatment_service(
        db=db,
        current_shop=current_shop,
        treatment_id=treatment_id,
    )
