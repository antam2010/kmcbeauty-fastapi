from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

from app.core.config import settings


def before_send(
    event: dict[str, Any],
    _hint: dict[str, Any],
) -> dict[str, Any] | None:
    # _hint 는 Sentry before_send 콜백 시그니처가 요구하는 인자로 미사용이다.
    if settings.APP_ENV in ["local", "debug"]:
        return None  # 완전히 차단하고 싶으면 이렇게

    return event


def init_sentry(
    dsn: str,
    environment: str,
    traces_sample_rate: float = 0.2,
    profiles_sample_rate: float = 0.0,
) -> None:
    """Sentry SDK 초기화 함수.

    Args:
        dsn (str): Sentry DSN URL
        environment (str): 실행 환경 (예: local, debug, production)
        traces_sample_rate (float): 성능 추적 샘플 비율 (0.0 ~ 1.0)
        profiles_sample_rate (float): CPU 프로파일링 샘플 비율 (0.0 ~ 1.0)

    """
    # Sentry에서 로깅도 연동할 수 있도록 설정
    sentry_logging = LoggingIntegration(
        level="INFO",  # breadcrumb 수집 로그 레벨 (INFO 이상 수집)
        event_level="ERROR",  # 이벤트로 전송할 최소 로그 레벨
    )

    sentry_sdk.init(
        dsn=dsn,
        integrations=[
            FastApiIntegration(),  # FastAPI 프레임워크 연동
            sentry_logging,  # Logging 연동
        ],
        traces_sample_rate=traces_sample_rate,  # 요청 성능 수집 비율
        profiles_sample_rate=profiles_sample_rate,  # CPU/메모리 프로파일링 비율
        environment=environment,  # 환경 구분(local, debug, prod)
        # 개인정보 자동 첨부 차단. True 이면 요청 쿠키(refresh_token 포함) · 인증 헤더 ·
        # 클라이언트 IP 가 이벤트에 붙고, 기본 스크러버는 "refresh_token" 을 못 지운다.
        send_default_pii=False,
        # 기본 목록(DEFAULT_DENYLIST)에 리프레시 토큰 키를 더한다. 키는 소문자로 바꿔
        # 정확히 일치할 때만 지운다(하이픈/밑줄 정규화 · 부분 일치 없음). 그래서
        # X-Refresh-Token 헤더(ASGI 는 소문자 x-refresh-token), refresh_token 쿠키 ·
        # 본문 키, 밑줄 변형을 모두 적는다. recursive=True 는 중첩 dict 까지 지운다.
        event_scrubber=EventScrubber(
            denylist=[
                *DEFAULT_DENYLIST,
                "refresh_token",
                "x-refresh-token",
                "x_refresh_token",
            ],
            recursive=True,
        ),
        # 스택 프레임 지역변수를 싣지 않는다(기본 True). auth 경로의 raw_token ·
        # new_refresh_token 같은 변수명은 스크러버의 키 정확 일치로 지워지지 않는다.
        include_local_variables=False,
        before_send=before_send,  # 이벤트 전송 전 처리 함수
    )
