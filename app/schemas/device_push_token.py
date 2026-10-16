from typing import ClassVar

from pydantic import Field

from app.schemas.mixin.base import BaseResponseModel


class DevicePushTokenBase(BaseResponseModel):
    shop_id: int | None = Field(None, description="샵 ID")
    device_id: str | None = Field(None, description="디바이스 고유 ID")
    token: str = Field(..., description="FCM 토큰")
    platform: str = Field(..., description="플랫폼 (iOS/Android 등)")
    is_active: bool | None = Field(True, description="활성화 여부")


class DevicePushTokenCreate(DevicePushTokenBase):
    """디바이스 푸시 토큰 생성 요청 스키마.

    user_id 는 받지 않는다. 토큰 소유자는 서버가 current_user.id 로 강제한다
    (IDOR 방지). 구버전 클라이언트가 user_id 를 보내도 거부(422)하지 않고
    무시한다(pydantic 기본 extra="ignore") — 하위 호환 유지.
    """


class DevicePushTokenUpdate(BaseResponseModel):
    """디바이스 푸시 토큰 수정 요청 스키마."""

    device_id: str | None = Field(None, description="디바이스 고유 ID")
    token: str | None = Field(None, description="FCM 토큰")
    platform: str | None = Field(None, description="플랫폼 (iOS/Android 등)")
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
    )
    shop_id: int | None = Field(
        None,
        description="샵 ID (user_id 또는 shop_id 중 하나 필수)",
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
