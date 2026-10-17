from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.limits import FILTER_DATE_MAX, FILTER_DATE_MIN
from app.schemas.mixin.base import BaseResponseModel
from app.schemas.treatment_item import TreatmentItemBase
from app.schemas.user import UserBaseResponse
from app.utils.datetime import now_kst_today

# 응답 스키마는 BaseResponseModel 이다. DB 의 naive UTC 일시(reserved_at 등)에 UTC 를
# 붙여 "...Z" 로 직렬화한다(오프셋이 없으면 앱이 현지 시각으로 읽어 9시간 어긋난다).
# 서비스는 dict(model_dump)를 돌려주고 FastAPI 가 응답 모델로 다시 검증하므로, 배포 전
# 캐시에 남은 오프셋 없는 문자열도 응답에서는 UTC 로 나간다.


class DashboardFilter(BaseModel):
    # 월초 · 월말(+1일) 계산이 넘치지 않는 범위만 받는다(밖은 422).
    target_date: date = Field(
        default_factory=now_kst_today,
        description="조회 날짜 (YYYY-MM-DD)",
        ge=FILTER_DATE_MIN,
        le=FILTER_DATE_MAX,
    )
    force_refresh: bool = Field(
        default=False,
        description="캐시 무효화 여부. True인 경우 캐시를 무시하고 DB에서 직접 조회",
    )


class TreatmentSummarySchema(BaseResponseModel):
    total_reservations: int = Field(
        ...,
        description="전체 예약 건수 (조회기간 내)",
    )
    completed: int = Field(
        ...,
        description="완료된 시술 수",
    )
    reserved: int = Field(
        ...,
        description="예약 상태(미방문) 건수",
    )
    visited: int = Field(
        ...,
        description="방문하여 시술한 고객 수",
    )
    canceled: int = Field(
        ...,
        description="취소된 예약 건수",
    )
    no_show: int = Field(
        ...,
        description="노쇼(예약 후 미방문) 건수",
    )
    expected_sales: int = Field(
        ...,
        description="예상 매출 (RESERVED, VISITED, COMPLETED 합계)",
    )
    actual_sales: int = Field(
        ...,
        description="실매출 (COMPLETED 상태 중 결제 완료만 집계)",
    )
    unpaid_total: int = Field(
        ...,
        description="외상 결제 합계 (COMPLETED 중 결제 미수금)",
    )


class TreatmentSalesItem(BaseResponseModel):
    menu_detail_id: int = Field(..., description="시술 항목 ID")
    name: str = Field(..., description="시술명")
    count: int = Field(..., description="예약 건수")
    expected_price: int = Field(..., description="총 예상 매출")
    actual_price: int = Field(..., description="실제 시술 완료된 금액")


class DashboardSalesSummary(BaseResponseModel):
    target_date: list[TreatmentSalesItem] = Field(
        ...,
        description="조회 날짜에 시술 항목별 매출/건수 요약",
    )
    month: list[TreatmentSalesItem] = Field(
        ...,
        description="월간 시술 항목별 매출/건수 요약",
    )


class DashboardSummary(BaseResponseModel):
    target_date: TreatmentSummarySchema = Field(
        ...,
        description="조회 날짜에 대한 시술 통계",
    )
    month: TreatmentSummarySchema = Field(
        ...,
        description="해당 월의 시술 통계 (1일부터 조회 날짜까지)",
    )


class DashboardStaffSummaryItem(BaseResponseModel):
    staff_id: int
    staff_name: str
    count: int


class DashboardStaffSummary(BaseResponseModel):
    target_date: list[DashboardStaffSummaryItem]
    month: list[DashboardStaffSummaryItem]


class DashboardCustomerInsight(BaseResponseModel):
    id: int = Field(..., description="예약 ID")
    reserved_at: datetime = Field(..., description="예약 일시")
    customer_name: str | None = Field(None, description="고객 이름")
    phone_number: str | None = Field(None, description="전화번호")
    status: str = Field(..., description="예약 상태 (예: RESERVED, COMPLETED 등)")
    treatments: list[TreatmentItemBase] = Field(
        ...,
        description="시술 항목 이름 목록",
    )
    total_duration_min: int = Field(..., description="총 시술 소요 시간 (분)")
    total_price: int = Field(..., description="총 시술 금액 (원)")
    memo: str | None = Field(None, description="시술 메모")
    payment_method: str = Field(
        ...,
        description="결제 수단 (CARD, CASH, UNPAID 등)",
    )
    staff: str | None = Field(None, description="담당 직원 이름")
    staff_user: UserBaseResponse | None = Field(
        None,
        description="담당 직원 정보",
    )
    total_reservations: int = Field(..., description="고객의 총 예약 횟수")
    no_show_count: int = Field(..., description="노쇼 횟수")
    no_show_rate: float = Field(..., description="노쇼 비율 (%)")
    unpaid_amount: int = Field(..., description="외상 금액 (미결제)")
    total_spent: int = Field(..., description="총 결제 금액")


class DashboardSummaryResponse(BaseResponseModel):
    target_date: date = Field(
        default_factory=now_kst_today,
        description="조회 날짜 (YYYY-MM-DD)",
    )
    summary: DashboardSummary = Field(
        ...,
        description="대시보드 요약 정보",
    )
    sales: DashboardSalesSummary = Field(
        ...,
        description="시술 항목별 매출/건수 요약",
    )
    customer_insights: list[DashboardCustomerInsight] = Field(
        ...,
        description="고객 인사이트 정보",
    )
    staff_summary: DashboardStaffSummary = Field(
        ...,
        description="직원별 시술 통계 요약",
    )
