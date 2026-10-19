from typing import ClassVar

from pydantic import Field

from app.schemas.mixin.base import BaseResponseModel
from app.schemas.user import NormalizedEmailStr, UserResponse


class ShopUserBase(BaseResponseModel):
    shop_id: int = Field(..., description="샵 ID")
    user_id: int = Field(..., description="유저 ID")
    is_primary_owner: int = Field(..., description="대표 원장 여부 (1=대표, 0=아님)")


class ShopUserCreate(ShopUserBase):
    pass


class ShopUserUpdate(BaseResponseModel):
    is_primary_owner: int | None = Field(
        None,
        description="대표 원장 여부 (1=대표, 0=아님)",
    )


# 권한 상승 방지(SPEC-SECURITY-001): 샵 유저 연결 요청 본문은 role 을 받지 않는다.
# 유저의 role 은 서버가 결정하며(초대코드 유무 등), 이 연결 API 는 오직 shop-user
# 매핑(멤버십)과 대표원장 여부만 다룬다.
class ShopUserAssociateRequest(BaseResponseModel):
    """샵에 기존 유저를 연결(멤버십 부여)하는 요청 스키마.

    email 로 대상 유저를 식별한다. role/password 등 유저 계정 필드는 받지 않는다
    (계정 생성/권한 결정은 이 계약의 범위가 아니며 SECURITY-001 소관).
    """

    email: NormalizedEmailStr = Field(..., description="연결할 대상 유저의 이메일")
    is_primary_owner: int = Field(
        0,
        ge=0,
        le=1,
        description="대표 원장 여부 (1=대표, 0=아님)",
    )


class ShopUserAssociateUpdateRequest(BaseResponseModel):
    """샵-유저 연결(멤버십)을 수정하는 요청 스키마.

    현재는 대표원장 여부만 갱신 대상이다. role 등 권한 필드는 받지 않는다.
    """

    is_primary_owner: int = Field(
        ...,
        ge=0,
        le=1,
        description="대표 원장 여부 (1=대표, 0=아님)",
    )


class ShopUserInDBBase(ShopUserBase):
    id: int = Field(..., description="PK")

    model_config: ClassVar[dict] = {
        "from_attributes": True,
    }


class ShopUserResponse(ShopUserInDBBase):
    pass


class ShopUserUserResponse(ShopUserBase):
    user: UserResponse = Field(..., description="유저 정보")
    model_config: ClassVar[dict] = {
        "from_attributes": True,
    }
