from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.shop import get_current_shop
from app.docs.common_responses import COMMON_ERROR_RESPONSES
from app.models.shop import Shop
from app.schemas.dashboard import (
    DashboardFilter,
    DashboardSummaryResponse,
)
from app.services.summary import get_dashboard_summary_service

router = APIRouter(prefix="/summary", tags=["통계"])


@router.get(
    "/dashboard",
    response_model=DashboardSummaryResponse,
    summary="대시보드 요약 정보 조회",
    description=(
        "오늘/이번달의 예약 통계, 시술 매출, 고객 인사이트 등을 포함한 "
        "대시보드 요약 정보를 조회합니다."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
    },
)
def get_dashboard_summary(
    params: DashboardFilter = Depends(),
    db: Session = Depends(get_db),
    current_shop: Shop = Depends(get_current_shop),
) -> DashboardSummaryResponse:
    return get_dashboard_summary_service(db, current_shop, params)
