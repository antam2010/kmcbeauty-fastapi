from datetime import datetime

from pydantic import ConfigDict, Field

from app.schemas.limits import PG_INT_MAX
from app.schemas.mixin.base import BaseResponseModel


# 시술 메뉴 상세 생성 요청
class TreatmentMenuDetailCreate(BaseResponseModel):
    name: str = Field(..., description="시술 메뉴 상세 이름", max_length=255)
    # 상한은 DB 컬럼(integer) 범위다(넘으면 INSERT/UPDATE 500).
    duration_min: int = Field(
        ...,
        description="시술 소요 시간(분)",
        gt=0,
        le=PG_INT_MAX,
    )
    base_price: int = Field(..., description="기본 가격", gt=0, le=PG_INT_MAX)

    model_config = ConfigDict(from_attributes=True)


# 시술 메뉴 상세 응답
class TreatmentMenuDetailResponse(BaseResponseModel):
    id: int = Field(..., description="시술 메뉴 상세 ID")
    menu_id: int = Field(..., description="시술 메뉴 ID")
    name: str = Field(..., description="시술 메뉴 상세 이름", max_length=255)
    duration_min: int = Field(..., description="시술 소요 시간(분)", gt=0)
    base_price: int = Field(..., description="기본 가격", gt=0)
    created_at: datetime = Field(..., description="생성일")
    updated_at: datetime = Field(..., description="수정일")

    model_config = ConfigDict(from_attributes=True)


# 시술 메뉴 생성 요청
class TreatmentMenuCreate(BaseResponseModel):
    name: str = Field(..., description="시술 메뉴 이름", max_length=255)

    model_config = ConfigDict(from_attributes=True)


# 시술 메뉴 생성 응답
class TreatmentMenuCreateResponse(BaseResponseModel):
    id: int = Field(..., description="시술 메뉴 ID")
    name: str = Field(..., description="시술 메뉴 이름", max_length=255)
    created_at: datetime = Field(..., description="생성일")
    updated_at: datetime = Field(..., description="수정일")

    model_config = ConfigDict(from_attributes=True)


# 시술 메뉴 목록 필터 요청
class TreatmentMenuFilter(BaseResponseModel):
    search: str | None = Field(default=None, description="검색어 (시술 메뉴 이름)")
    model_config = ConfigDict(from_attributes=True)


# 시술 메뉴 상세 응답 (하위 상세 리스트 포함)
class TreatmentMenuResponse(BaseResponseModel):
    id: int = Field(..., description="시술 메뉴 ID")
    shop_id: int = Field(..., description="시술 메뉴 가게 ID")
    name: str = Field(..., description="시술 메뉴 이름", max_length=255)
    created_at: datetime = Field(..., description="생성일")
    updated_at: datetime = Field(..., description="수정일")
    details: list[TreatmentMenuDetailResponse] = Field(
        [],
        description="시술 메뉴 상세 목록",
    )

    model_config = ConfigDict(from_attributes=True)
