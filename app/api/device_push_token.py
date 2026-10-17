from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from app.core.rate_limit import limiter
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.docs.common_responses import COMMON_ERROR_RESPONSES
from app.models.user import User
from app.schemas.device_push_token import (
    DevicePushTokenCreate,
    DevicePushTokenResponse,
    DevicePushTokenUpdate,
    FCMMessageRequest,
    FCMMessageResponse,
)
from app.services.device_push_token_service import (
    delete_device_token_service,
    get_my_device_tokens_service,
    register_device_token_service,
    send_fcm_notification_service,
    update_device_token_service,
)

# 디바이스 푸시 토큰 관련 API 그룹 지정
router = APIRouter(prefix="/device-tokens", tags=["디바이스 푸시 토큰"])


@router.post(
    "",
    response_model=DevicePushTokenResponse,
    summary="디바이스 푸시 토큰 등록",
    description=(
        "디바이스의 FCM 토큰을 등록합니다. 이미 존재하는 토큰이면 업데이트합니다. "
        "토큰 소유자는 항상 로그인 사용자이며(본문 user_id 는 무시), "
        "shop_id 는 사용자가 멤버인 샵만 지정할 수 있습니다."
    ),
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_409_CONFLICT: COMMON_ERROR_RESPONSES[status.HTTP_409_CONFLICT],
        status.HTTP_500_INTERNAL_SERVER_ERROR: COMMON_ERROR_RESPONSES[
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ],
    },
)
def register_device_token(
    token_data: DevicePushTokenCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DevicePushTokenResponse:
    """디바이스 푸시 토큰을 등록합니다."""
    return register_device_token_service(
        db=db,
        token_data=token_data,
        current_user=current_user,
    )


@router.get(
    "/me",
    response_model=list[DevicePushTokenResponse],
    summary="내 디바이스 토큰 목록 조회",
    description="현재 로그인한 사용자의 활성화된 디바이스 토큰 목록을 조회합니다.",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_500_INTERNAL_SERVER_ERROR: COMMON_ERROR_RESPONSES[
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ],
    },
)
def get_my_device_tokens(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DevicePushTokenResponse]:
    """내 디바이스 토큰 목록을 조회합니다."""
    return get_my_device_tokens_service(db=db, current_user=current_user)


@router.patch(
    "/{token_id}",
    response_model=DevicePushTokenResponse,
    summary="디바이스 토큰 수정",
    description=(
        "디바이스 푸시 토큰 정보를 수정합니다. 본인 토큰만 대상이며, "
        "다른 사용자의 토큰은 404 로 응답합니다."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
        status.HTTP_500_INTERNAL_SERVER_ERROR: COMMON_ERROR_RESPONSES[
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ],
    },
)
def update_device_token(
    token_id: int,
    token_update: DevicePushTokenUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DevicePushTokenResponse:
    """디바이스 토큰을 수정합니다."""
    return update_device_token_service(
        db=db,
        token_id=token_id,
        token_update=token_update,
        current_user=current_user,
    )


@router.delete(
    "/{token_id}",
    summary="디바이스 토큰 삭제",
    description=(
        "디바이스 푸시 토큰을 삭제합니다. 본인 토큰만 대상이며, "
        "다른 사용자의 토큰은 404 로 응답합니다."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
        status.HTTP_500_INTERNAL_SERVER_ERROR: COMMON_ERROR_RESPONSES[
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ],
    },
)
def delete_device_token(
    token_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """디바이스 토큰을 삭제합니다."""
    return delete_device_token_service(
        db=db,
        token_id=token_id,
        current_user=current_user,
    )


@router.post(
    "/send-fcm",
    response_model=FCMMessageResponse,
    summary="FCM 푸시 알림 전송",
    description=(
        "특정 유저 또는 샵의 디바이스에 FCM 푸시 알림을 전송합니다. "
        "같은 샵 멤버에게만 보낼 수 있습니다: user_id 는 본인이거나 샵을 공유하는 "
        "유저, shop_id 는 본인이 멤버인 샵(현재 멤버의 디바이스로만 전송)."
    ),
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: COMMON_ERROR_RESPONSES[
            status.HTTP_400_BAD_REQUEST
        ],
        status.HTTP_403_FORBIDDEN: COMMON_ERROR_RESPONSES[status.HTTP_403_FORBIDDEN],
        status.HTTP_404_NOT_FOUND: COMMON_ERROR_RESPONSES[status.HTTP_404_NOT_FOUND],
        status.HTTP_500_INTERNAL_SERVER_ERROR: COMMON_ERROR_RESPONSES[
            status.HTTP_500_INTERNAL_SERVER_ERROR
        ],
    },
)
# 푸시 스팸 방지: IP 당 분당 10회
@limiter.limit("10/minute")
def send_fcm_notification(
    request: Request,  # noqa: ARG001  # slowapi limiter 가 IP 추출을 위해 request 를 요구한다
    fcm_request: FCMMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FCMMessageResponse:
    """FCM 푸시 알림을 전송합니다."""
    return send_fcm_notification_service(
        db=db,
        fcm_request=fcm_request,
        current_user=current_user,
    )
