import logging
from typing import Any

import firebase_admin
from firebase_admin import credentials, messaging

from app.core.config import settings

logger = logging.getLogger(__name__)

# Firebase 서비스 계정 키 파일 경로(중앙 settings 에서 소싱).
# 시크릿 노출 방지: 하드코딩된 기본 경로로 폴백하지 않고 설정값을 필수로 요구한다.
# 미설정 시 빈 문자열이므로 아래 분기에서 초기화를 스킵한다(현행 동작 보존).
SERVICE_ACCOUNT_KEY_PATH = settings.FIREBASE_SERVICE_ACCOUNT_KEY_PATH

# Firebase 초기화
if not SERVICE_ACCOUNT_KEY_PATH:
    logger.error(
        "FIREBASE_SERVICE_ACCOUNT_KEY_PATH env var is not set; "
        "Firebase Admin SDK will not be initialized.",
    )
else:
    try:
        # firebase-admin 은 초기화 여부 확인용 공개 API 를 제공하지 않아
        # 관용적으로 내부 _apps 레지스트리를 참조한다.
        if not firebase_admin._apps:  # noqa: SLF001
            cred = credentials.Certificate(SERVICE_ACCOUNT_KEY_PATH)
            firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin SDK initialized successfully")
    except Exception:
        # 초기화 실패는 다양한 원인으로 발생할 수 있어 광범위 처리 후 degrade 한다.
        logger.exception("Failed to initialize Firebase Admin SDK")


def send_fcm_message(
    token: str,
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """FCM 메시지를 전송합니다.

    Args:
        token: FCM 디바이스 토큰
        title: 푸시 알림 제목
        body: 푸시 알림 내용
        data: 추가 데이터 (선택사항)

    Returns:
        dict: 메시지 전송 결과

    Raises:
        Exception: 메시지 전송 실패 시

    """
    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data or {},
            token=token,
        )
        response = messaging.send(message)
    except Exception:
        logger.exception("Failed to send FCM message")
        raise
    else:
        logger.info("FCM message sent successfully: %s", response)
        return {"success": True, "message_id": response}


def send_fcm_multicast(
    tokens: list[str],
    title: str,
    body: str,
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    """여러 디바이스에 FCM 메시지를 전송합니다.

    Args:
        tokens: FCM 디바이스 토큰 리스트
        title: 푸시 알림 제목
        body: 푸시 알림 내용
        data: 추가 데이터 (선택사항)

    Returns:
        dict: 메시지 전송 결과

    Raises:
        Exception: 메시지 전송 실패 시

    """
    try:
        message = messaging.MulticastMessage(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data or {},
            tokens=tokens,
        )
        # firebase-admin 7.x 에서 send_multicast()는 제거되었으므로
        # send_each_for_multicast()를 사용한다. 응답 객체의
        # success_count / failure_count 필드는 동일하게 제공된다.
        response = messaging.send_each_for_multicast(message)
    except Exception:
        logger.exception("Failed to send FCM multicast")
        raise
    else:
        logger.info(
            "FCM multicast sent: %s successful, %s failed",
            response.success_count,
            response.failure_count,
        )
        return {
            "success": True,
            "success_count": response.success_count,
            "failure_count": response.failure_count,
        }
