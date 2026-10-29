from typing import ClassVar

from pydantic import Field

from app.schemas.limits import PG_INT_MAX
from app.schemas.mixin.base import BaseResponseModel

# FCM 토큰 길이 상한. 컬럼은 Text 지만 UNIQUE · 일반 btree 인덱스가 있어 항목이
# 2704바이트를 넘으면 INSERT/UPDATE 가 500 이다(실 PG 확인). 4바이트 문자만으로
# 채워도 2048바이트라 한도 안이다. 실제 토큰(FCM ~163자, APNs 64자)보다 넉넉하다.
# 요청 스키마에만 둔다(응답은 기존 저장값을 그대로 돌려줘야 한다).
PUSH_TOKEN_MAX_LENGTH = 512


class DevicePushTokenBase(BaseResponseModel):
    shop_id: int | None = Field(None, description="샵 ID", ge=1, le=PG_INT_MAX)
    device_id: str | None = Field(
        None,
        description="디바이스 고유 ID",
        max_length=255,
    )
    token: str = Field(..., description="FCM 토큰")
    platform: str = Field(..., description="플랫폼 (iOS/Android 등)", max_length=10)
    is_active: bool | None = Field(True, description="활성화 여부")


class DevicePushTokenCreate(DevicePushTokenBase):
    """디바이스 푸시 토큰 생성 요청 스키마.

    user_id 는 받지 않는다. 토큰 소유자는 서버가 current_user.id 로 강제한다
    (IDOR 방지). 구버전 클라이언트가 user_id 를 보내도 거부(422)하지 않고
    무시한다(pydantic 기본 extra="ignore") — 하위 호환 유지.
    """

    token: str = Field(
        ...,
        description="FCM 토큰",
        max_length=PUSH_TOKEN_MAX_LENGTH,
    )


class DevicePushTokenUpdate(BaseResponseModel):
    """디바이스 푸시 토큰 수정 요청 스키마."""

    device_id: str | None = Field(
        None,
        description="디바이스 고유 ID",
        max_length=255,
    )
    token: str | None = Field(
        None,
        description="FCM 토큰",
        max_length=PUSH_TOKEN_MAX_LENGTH,
    )
    platform: str | None = Field(
        None,
        description="플랫폼 (iOS/Android 등)",
        max_length=10,
    )
    is_active: bool | None = Field(None, description="활성화 여부")


class DevicePushTokenResponse(DevicePushTokenBase):
    """디바이스 푸시 토큰 응답 스키마."""

    id: int = Field(..., description="디바이스 푸시 토큰 ID")
    user_id: int | None = Field(None, description="유저 ID")

    model_config: ClassVar[dict] = {
        "from_attributes": True,
    }


class FCMMessageRequest(BaseResponseModel):
    """FCM 메시지 전송 요청 스키마."""

    user_id: int | None = Field(
        None,
        description="유저 ID (user_id 또는 shop_id 중 하나 필수)",
        ge=1,
        le=PG_INT_MAX,
    )
    shop_id: int | None = Field(
        None,
        description="샵 ID (user_id 또는 shop_id 중 하나 필수)",
        ge=1,
        le=PG_INT_MAX,
    )
    title: str = Field(..., description="푸시 알림 제목")
    body: str = Field(..., description="푸시 알림 내용")
    data: dict[str, str] | None = Field(None, description="추가 데이터")


class FCMMessageResponse(BaseResponseModel):
    """FCM 메시지 전송 응답 스키마."""

    success: bool = Field(..., description="성공 여부")
    message_id: str | None = Field(None, description="FCM 메시지 ID")
    success_count: int | None = Field(None, description="성공한 메시지 수")
    failure_count: int | None = Field(None, description="실패한 메시지 수")
