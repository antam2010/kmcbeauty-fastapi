from datetime import datetime
from typing import Annotated

from pydantic import ConfigDict, Field, field_validator

from app.schemas.mixin.base import BaseResponseModel
from app.utils.phone import is_valid_korean_phone_number, normalize_korean_phone_number


# 전화번호 유효성 검사 + 포맷 통일 Mixin
class PhoneNumberValidatorMixin:
    INVALID_PHONE_MESSAGE = "유효하지 않은 전화번호입니다."

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not is_valid_korean_phone_number(v):
            message = cls.INVALID_PHONE_MESSAGE
            raise ValueError(message)
        return normalize_korean_phone_number(v)


# 그룹명 정규화 Mixin: "그룹 없음"은 항상 None(NULL) 으로 저장한다
class GroupNameNormalizerMixin:
    @field_validator("group_name", mode="before")
    @classmethod
    def normalize_group_name(cls, v: object) -> object:
        # 앱은 "그룹 없음"을 '' 로 보낸다. ''·공백뿐인 값은 None 으로 바꾸고 실제 이름은
        # 앞뒤 공백을 지운다. 그래야 "그룹 없음"이 NULL 한 버킷으로만 묶인다.
        # mode="before" 라 길이 제한(max_length)은 공백을 지운 값에 적용된다.
        if isinstance(v, str):
            return v.strip() or None
        return v


# 전화번호부 생성 요청 스키마
class PhonebookCreate(
    BaseResponseModel,
    PhoneNumberValidatorMixin,
    GroupNameNormalizerMixin,
):
    group_name: str | None = Field(
        default=None,
        max_length=100,
        description="그룹 이름",
    )
    memo: str | None = Field(default=None, description="메모")
    name: str = Field(..., max_length=100, description="이름")
    phone_number: Annotated[str, Field(max_length=20, description="전화번호")]


# 전화번호부 수정 요청 스키마
class PhonebookUpdate(
    BaseResponseModel,
    PhoneNumberValidatorMixin,
    GroupNameNormalizerMixin,
):
    group_name: str | None = Field(
        default=None,
        max_length=100,
        description="그룹 이름",
    )
    memo: str | None = Field(default=None, description="메모")
    name: str | None = Field(default=None, max_length=100, description="이름")
    phone_number: str | None = Field(
        default=None,
        max_length=20,
        description="전화번호",
    )


# 중복 확인 응답 스키마
class DuplicateCheckResponse(BaseResponseModel, PhoneNumberValidatorMixin):
    exists: bool = Field(..., description="중복 여부")
    phone_number: str | None = Field(
        default=None,
        description="입력한 전화번호",
    )


# 전화번호부 목록 요청 (필터링용)
class PhonebookFilter(BaseResponseModel):
    search: str | None = Field(
        default=None,
        description="검색어 (이름, 전화번호, 그룹명, 메모 등)",
    )


# 전화번호부 그룹 별 응답
class PhonebookGroupedByGroupnameResponse(BaseResponseModel):
    # 그룹 없이 저장된 연락처(group_name IS NULL)는 null 버킷 하나로 묶인다.
    group_name: str | None = Field(..., description="그룹 이름 (그룹 없음은 null)")
    count: int = Field(..., description="전화번호부 개수")
    items: list["PhonebookResponse"] = Field(default=[], description="전화번호부 목록")
    model_config = ConfigDict(from_attributes=True)


# 전화번호부 응답 스키마
class PhonebookResponse(BaseResponseModel):
    id: int = Field(..., description="전화번호부 ID")
    shop_id: int = Field(..., description="상점 ID")
    group_name: str | None = Field(None, max_length=100, description="그룹 이름")
    name: str = Field(..., max_length=100, description="이름")
    phone_number: str = Field(..., max_length=20, description="전화번호")
    memo: str | None = Field(None, description="메모")
    created_at: datetime = Field(..., description="생성일")
    updated_at: datetime = Field(..., description="수정일")

    model_config = ConfigDict(from_attributes=True)
