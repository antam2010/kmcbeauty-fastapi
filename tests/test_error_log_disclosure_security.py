"""오류 응답 · Sentry 이벤트 정보 노출 차단 테스트 (보안).

대상:
- app/services/device_push_token_service.py::send_fcm_notification_service
  - FCM 전송 실패 500 응답 본문에 예외 원문을 싣지 않는다(모든 APP_ENV).
    예외는 로그 · Sentry 로만 남긴다(exception=e). 예외 원문 노출은 debug 에서
    CustomException 이 exception 필드로만 한다(기존 정책).
- app/core/sentry.py::init_sentry
  - 기본 스크러버(DEFAULT_DENYLIST)가 지우지 못하는 refresh_token / x-refresh-token /
    x_refresh_token 키를 요청 헤더 · 쿠키 · 본문에서 지운다(중첩 dict 포함).
    키 비교는 소문자 정확 일치다(하이픈/밑줄 정규화 · 부분 일치 없음).

DB/FCM/Sentry 없이 검증한다. 라우터만 올린 최소 FastAPI 앱에 의존성을 오버라이드하고,
서비스가 import 한 CRUD · FCM 이름을 monkeypatch 한다. sentry_sdk.init 은 kwargs 만
기록하고, 설정된 EventScrubber 로 샘플 이벤트를 직접 스크럽한다.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.scrubber import EventScrubber
from sentry_sdk.utils import AnnotatedValue

from app import exceptions as exceptions_module
from app.api import device_push_token as device_push_token_api
from app.core import sentry as sentry_module
from app.core.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.services import device_push_token_service as service

REQUESTER_ID = 7
SHOP_ID = 10
FCM_A = "fcm-registration-a"
INTERNAL_ERROR = "firebase project kmc-internal-01 credential rejected at 10.0.3.7"
REFRESH_JWT = "eyJ.refresh.secret"


# ---------------------------------------------------------------------------
# send-fcm 500: 응답 본문에 예외 원문이 없다
# ---------------------------------------------------------------------------


@pytest.fixture
def failing_fcm_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list]:
    """멤버 · 토큰은 정상이고 FCM 전송만 내부 오류로 실패하는 send-fcm 클라이언트."""
    captured: list = []
    token = SimpleNamespace(token=FCM_A)

    def failing_send(**_kwargs: object) -> dict:
        raise RuntimeError(INTERNAL_ERROR)

    monkeypatch.setattr(service, "get_shop_user", lambda _db, _sid, _uid: object())
    monkeypatch.setattr(service, "get_device_tokens_by_shop", lambda **_kw: [token])
    monkeypatch.setattr(service, "send_fcm_message", failing_send)
    monkeypatch.setattr(exceptions_module, "capture_exception", captured.append)

    api = FastAPI()
    api.include_router(device_push_token_api.router)
    api.dependency_overrides[get_db] = lambda: None
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=REQUESTER_ID,
    )
    return TestClient(api), captured


@pytest.mark.parametrize("app_env", ["local", "stage", "production"])
def test_send_fcm_500_body_hides_exception_text(
    failing_fcm_client: tuple[TestClient, list],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    app_env: str,
) -> None:
    """FCM 실패 500 본문에는 예외 원문이 없고, 예외는 로그 · Sentry 로만 남는다."""
    monkeypatch.setattr(settings, "APP_ENV", app_env)
    client, captured = failing_fcm_client

    response = client.post(
        "/device-tokens/send-fcm",
        json={"shop_id": SHOP_ID, "title": "예약 알림", "body": "10시 예약"},
    )

    assert response.status_code == 500
    assert INTERNAL_ERROR not in response.text
    assert response.json()["detail"]["detail"] == "Failed to send FCM notification"
    # 로그 · Sentry 에는 원래 예외가 그대로 전달된다(exception=e 유지).
    assert INTERNAL_ERROR in caplog.text
    assert [str(error) for error in captured] == [INTERNAL_ERROR]


# ---------------------------------------------------------------------------
# Sentry: 리프레시 토큰 키 스크럽
# ---------------------------------------------------------------------------


@pytest.fixture
def sentry_init_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict:
    """init_sentry 가 sentry_sdk.init 에 넘긴 kwargs 를 기록한다(실제 초기화 없음)."""
    captured: dict = {}
    monkeypatch.setattr(
        sentry_module.sentry_sdk,
        "init",
        lambda **kwargs: captured.update(kwargs),
    )

    sentry_module.init_sentry(dsn="", environment="test")

    return captured


def _filtered(value: object) -> bool:
    return isinstance(value, AnnotatedValue) and value.value == "[Filtered]"


def test_scrubber_removes_refresh_token_from_request(sentry_init_kwargs: dict) -> None:
    """요청 헤더 · 쿠키 · 본문(중첩 포함)의 리프레시 토큰 키가 [Filtered] 로 바뀐다."""
    scrubber: EventScrubber = sentry_init_kwargs["event_scrubber"]
    event = {
        "request": {
            "headers": {
                "x-refresh-token": REFRESH_JWT,
                "content-type": "application/json",
            },
            "cookies": {"refresh_token": REFRESH_JWT},
            "data": {
                "x_refresh_token": REFRESH_JWT,
                "session_info": {"refresh_token": REFRESH_JWT},
                "name": "홍길동",
            },
        },
    }

    scrubber.scrub_event(event)

    request = event["request"]
    assert _filtered(request["headers"]["x-refresh-token"])
    assert _filtered(request["cookies"]["refresh_token"])
    assert _filtered(request["data"]["x_refresh_token"])
    assert _filtered(request["data"]["session_info"]["refresh_token"])
    assert REFRESH_JWT not in repr(event)
    # 민감하지 않은 키는 그대로 둔다.
    assert request["headers"]["content-type"] == "application/json"
    assert request["data"]["name"] == "홍길동"


def test_scrubber_keeps_default_denylist_and_ignores_key_case(
    sentry_init_kwargs: dict,
) -> None:
    """기본 목록(authorization 등)은 유지되고, 키 비교는 대소문자를 가리지 않는다."""
    scrubber: EventScrubber = sentry_init_kwargs["event_scrubber"]
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer access-secret",
                "X-Refresh-Token": REFRESH_JWT,
            },
        },
    }

    scrubber.scrub_event(event)

    headers = event["request"]["headers"]
    assert _filtered(headers["Authorization"])
    assert _filtered(headers["X-Refresh-Token"])
