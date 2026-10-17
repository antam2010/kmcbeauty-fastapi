from fastapi import status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.firebase import send_fcm_message, send_fcm_multicast
from app.crud.device_push_token_crud import (
    delete_device_token,
    get_device_tokens_by_shop,
    get_device_tokens_by_user,
    get_or_create_device_token,
    get_user_device_token_by_id,
    update_device_token,
)
from app.crud.shop_user_crud import get_shop_user, users_share_shop
from app.exceptions import CustomException
from app.models.user import User
from app.schemas.device_push_token import (
    DevicePushTokenCreate,
    DevicePushTokenResponse,
    DevicePushTokenUpdate,
    FCMMessageRequest,
    FCMMessageResponse,
)

DOMAIN = "DEVICE_PUSH_TOKEN"


def _require_shop_member(db: Session, shop_id: int, user_id: int) -> None:
    """user_id 가 shop_id 샵의 멤버(shop_user)가 아니면 403.

    존재하지 않는 샵도 같은 403 으로 응답해 샵 존재 여부를 노출하지 않는다.
    """
    if not get_shop_user(db, shop_id, user_id):
        raise CustomException(
            status_code=status.HTTP_403_FORBIDDEN,
            domain=DOMAIN,
            detail="Not a member of the shop",
        )


def _require_push_target_user(
    db: Session,
    requester_id: int,
    target_user_id: int,
) -> None:
    """유저 대상 전송 인가: 본인이거나 요청자와 샵을 하나 이상 공유해야 한다(403)."""
    if target_user_id == requester_id:
        return
    if not users_share_shop(db, requester_id, target_user_id):
        raise CustomException(
            status_code=status.HTTP_403_FORBIDDEN,
            domain=DOMAIN,
            detail="Access denied",
        )


def register_device_token_service(
    db: Session,
    token_data: DevicePushTokenCreate,
    current_user: User,
) -> DevicePushTokenResponse:
    """디바이스 푸시 토큰 등록 (생성 또는 업데이트).

    소유자는 항상 요청자(current_user)다. shop_id 를 지정하면 요청자가 그 샵의
    멤버여야 한다(타 샵 수신자 목록에 토큰을 심는 것을 차단).
    """
    try:
        if token_data.shop_id is not None:
            _require_shop_member(db, token_data.shop_id, current_user.id)

        # 기존 토큰이 있으면 업데이트, 없으면 생성
        device_token = get_or_create_device_token(
            db=db,
            user_id=current_user.id,
            shop_id=token_data.shop_id,
            device_id=token_data.device_id,
            token=token_data.token,
            platform=token_data.platform,
        )
        db.commit()
        db.refresh(device_token)
        return DevicePushTokenResponse.model_validate(device_token)
    except IntegrityError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_409_CONFLICT,
            domain=DOMAIN,
            detail="Token already exists",
            exception=e,
        ) from e
    except SQLAlchemyError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


def get_my_device_tokens_service(
    db: Session,
    current_user: User,
) -> list[DevicePushTokenResponse]:
    """내 디바이스 푸시 토큰 목록 조회."""
    try:
        tokens = get_device_tokens_by_user(
            db=db,
            user_id=current_user.id,
            is_active=True,
        )
        return [DevicePushTokenResponse.model_validate(t) for t in tokens]
    except SQLAlchemyError as e:
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


def update_device_token_service(
    db: Session,
    token_id: int,
    token_update: DevicePushTokenUpdate,
    current_user: User,
) -> DevicePushTokenResponse:
    """디바이스 푸시 토큰 업데이트 (본인 토큰만, 타인 토큰은 404)."""
    try:
        device_token = get_user_device_token_by_id(db, current_user.id, token_id)
        if not device_token:
            raise CustomException(
                status_code=status.HTTP_404_NOT_FOUND,
                domain=DOMAIN,
                detail="Device token not found",
            )

        update_data = token_update.model_dump(exclude_unset=True)
        updated_token = update_device_token(db, device_token, update_data)
        db.commit()
        db.refresh(updated_token)
        return DevicePushTokenResponse.model_validate(updated_token)
    except CustomException:
        raise
    except IntegrityError as e:
        # token 은 UNIQUE 다. 다른 행이 쓰는 토큰으로 바꾸면 등록과 같은 409 다.
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_409_CONFLICT,
            domain=DOMAIN,
            detail="Token already exists",
            exception=e,
        ) from e
    except SQLAlchemyError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


def delete_device_token_service(
    db: Session,
    token_id: int,
    current_user: User,
) -> None:
    """디바이스 푸시 토큰 삭제 (본인 토큰만, 타인 토큰은 404)."""
    try:
        device_token = get_user_device_token_by_id(db, current_user.id, token_id)
        if not device_token:
            raise CustomException(
                status_code=status.HTTP_404_NOT_FOUND,
                domain=DOMAIN,
                detail="Device token not found",
            )

        delete_device_token(db, device_token)
        db.commit()
    except CustomException:
        raise
    except SQLAlchemyError as e:
        db.rollback()
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            exception=e,
        ) from e


# @MX:ANCHOR: [AUTO] 푸시 전송 인가 경계 — 같은 샵 멤버에게만 FCM 을 보낸다.
# @MX:REASON: 외부 시스템(FCM) 연동 지점이자 공개 API 경계. user_id 대상은 본인/공유 샵,
#             shop_id 대상은 요청자 멤버십 + 현재 멤버 토큰(조인)만 허용한다.
#             이 불변식이 깨지면 임의 유저·샵으로 임의 문구 푸시(피싱)가 가능해진다.
def send_fcm_notification_service(
    db: Session,
    fcm_request: FCMMessageRequest,
    current_user: User,
) -> FCMMessageResponse:
    """FCM 푸시 알림 전송 (같은 샵 멤버에게만).

    - user_id 대상: 본인이거나 요청자와 샵을 하나 이상 공유하는 유저만(아니면 403)
    - shop_id 대상: 요청자가 그 샵의 멤버여야 하고(아니면 403), 현재 멤버의
      활성 토큰에만 전송한다
    - 둘 다 주면 기존과 같이 user_id 를 우선한다
    """
    try:
        # user_id 또는 shop_id 중 하나는 필수
        if not fcm_request.user_id and not fcm_request.shop_id:
            raise CustomException(
                status_code=status.HTTP_400_BAD_REQUEST,
                domain=DOMAIN,
                detail="Either user_id or shop_id is required",
            )

        # 대상 인가 후 디바이스 토큰 조회
        if fcm_request.user_id:
            _require_push_target_user(db, current_user.id, fcm_request.user_id)
            tokens = get_device_tokens_by_user(
                db=db,
                user_id=fcm_request.user_id,
                is_active=True,
            )
        else:
            _require_shop_member(db, fcm_request.shop_id, current_user.id)
            tokens = get_device_tokens_by_shop(
                db=db,
                shop_id=fcm_request.shop_id,
                is_active=True,
            )

        if not tokens:
            raise CustomException(
                status_code=status.HTTP_404_NOT_FOUND,
                domain=DOMAIN,
                detail="No active device tokens found",
            )

        # FCM 메시지 전송
        token_strings = [t.token for t in tokens]

        if len(token_strings) == 1:
            # 단일 디바이스
            result = send_fcm_message(
                token=token_strings[0],
                title=fcm_request.title,
                body=fcm_request.body,
                data=fcm_request.data,
            )
            return FCMMessageResponse(
                success=result["success"],
                message_id=result["message_id"],
            )
        # 여러 디바이스
        result = send_fcm_multicast(
            tokens=token_strings,
            title=fcm_request.title,
            body=fcm_request.body,
            data=fcm_request.data,
        )
        return FCMMessageResponse(
            success=result["success"],
            success_count=result["success_count"],
            failure_count=result["failure_count"],
        )

    except CustomException:
        raise
    except Exception as e:
        # 예외 원문(FCM/Firebase 내부 정보)은 응답 detail 에 싣지 않는다. exception 으로
        # 넘겨 로그 · Sentry 에만 남긴다(debug 노출 여부는 CustomException 이 정한다).
        raise CustomException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            domain=DOMAIN,
            detail="Failed to send FCM notification",
            exception=e,
        ) from e
