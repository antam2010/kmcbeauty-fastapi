from datetime import datetime
from typing import ClassVar

from pydantic import Field

from app.schemas.limits import PG_INT_MAX, PG_SMALLINT_MAX
from app.schemas.mixin.base import BaseResponseModel
from app.schemas.treatment_menu_detail import TreatmentMenuDetailBase


class TreatmentItemBase(BaseResponseModel):
    """시술 항목 기본 스키마."""

    treatment_id: int = Field(..., description="시술 예약 ID")
    menu_detail_id: int | None = Field(None, description="시술 상세 ID")
    base_price: int = Field(0, description="기본 가격")
    duration_min: int = Field(0, description="소요 시간 (분)")
    session_no: int = Field(1, description="시술 회차")

    menu_detail: TreatmentMenuDetailBase = Field(
        None,
        description="시술 항목 상세 정보",
    )

    model_config: ClassVar[dict] = {"from_attributes": True}


class TreatmentItemSimple(BaseResponseModel):
    """시술 항목 간단 정보 스키마."""

    session_no: int = Field(1, description="시술 회차")
    menu_detail: TreatmentMenuDetailBase = Field(
        None,
        description="시술 항목 상세 정보",
    )

    model_config: ClassVar[dict] = {"from_attributes": True}


# 항목 입력 범위. 범위 밖 값은 요청 본문 검증 오류(422)로 거절한다.
# - 소요시간: 단일 항목은 하루(1440분)를 넘지 않는다. 상한이 없으면 거대한 값이
#   자동완료 종료 시각 계산을 넘쳐(OverflowError) 배치 전체가 실패했다.
# - 가격: 음수 금지, 상한은 DB 컬럼(PostgreSQL integer) 범위(넘으면 INSERT 500).
# - 상세 ID · 회차: DB 컬럼(integer · smallint) 범위. 넘으면 조회 · INSERT 가 500.
DURATION_MIN_MAX = 24 * 60
BASE_PRICE_MAX = 2**31 - 1


class TreatmentItemCreate(BaseResponseModel):
    """시술 항목 생성 요청 스키마."""

    menu_detail_id: int = Field(..., description="시술 상세 ID", ge=1, le=PG_INT_MAX)
    base_price: int = Field(..., description="기본 가격", ge=0, le=BASE_PRICE_MAX)
    duration_min: int = Field(
        ...,
        description="소요 시간 (분)",
        ge=0,
        le=DURATION_MIN_MAX,
    )
    session_no: int = Field(..., description="시술 회차", ge=1, le=PG_SMALLINT_MAX)


class TreatmentItemUpdate(BaseResponseModel):
    """시술 항목 수정 요청 스키마."""

    id: int | None = Field(None, description="시술 항목 ID")
    menu_detail_id: int | None = Field(
        None,
        description="시술 상세 ID",
        ge=1,
        le=PG_INT_MAX,
    )
    base_price: int | None = Field(
        None,
        description="기본 가격",
        ge=0,
        le=BASE_PRICE_MAX,
    )
    duration_min: int | None = Field(
        None,
        description="소요 시간 (분)",
        ge=0,
        le=DURATION_MIN_MAX,
    )
    session_no: int | None = Field(
        None,
        description="시술 회차",
        ge=1,
        le=PG_SMALLINT_MAX,
    )


class TreatmentItemInDBBase(TreatmentItemBase):
    """시술 항목 DB 모델 스키마."""

    id: int = Field(..., description="시술 항목 ID")
    created_at: datetime = Field(..., description="생성일시")
    updated_at: datetime = Field(..., description="수정일시")

    model_config: ClassVar[dict] = {"from_attributes": True}


class TreatmentItemResponse(TreatmentItemInDBBase):
    """시술 항목 응답 스키마."""
