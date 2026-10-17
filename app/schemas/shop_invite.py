from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, Field

from app.schemas.mixin.base import BaseResponseModel

# 초대코드 만료 기간 범위(초). 앱 선택지(1시간~30일)를 모두 포함한다. 범위가 없으면
# 0 · 음수(만들자마자 만료된 코드)나 거대값(만료 일시 계산 범위 초과 → 500)이
# 들어왔다.
INVITE_EXPIRE_IN_MIN = 60
INVITE_EXPIRE_IN_MAX = 60 * 60 * 24 * 30
INVITE_EXPIRE_IN_DEFAULT = 60 * 60 * 24 * 7


# BaseResponseModel: DB 의 naive UTC 일시에 UTC 를 붙여 "...Z" 로 직렬화한다
# (오프셋이 없으면 앱이 현지 시각으로 읽어 9시간 어긋난다).
class ShopInviteResponse(BaseResponseModel):
    shop_id: int
    invite_code: str
    expired_at: datetime
    model_config: ClassVar[dict] = {
        "from_attributes": True,
    }


class ShopInviteCreateRequest(BaseModel):
    expire_in: int | None = Field(
        default=INVITE_EXPIRE_IN_DEFAULT,
        description="초대코드 만료 기간(초 단위, 60초 이상 30일 이하)",
        ge=INVITE_EXPIRE_IN_MIN,
        le=INVITE_EXPIRE_IN_MAX,
    )
