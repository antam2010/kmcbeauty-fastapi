import threading
import time
from collections.abc import Callable
from typing import Any

import redis
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

from app.core.config import settings

# Redis 장애 중 RedisError 이벤트는 프로세스당 이 간격(초)에 한 건만 보낸다.
REDIS_ERROR_EVENT_INTERVAL_SECONDS = 60


class EventThrottle:
    """간격(초)마다 한 건만 통과시키는 스레드 안전 스로틀.

    통과한 시각부터 창이 새로 시작되고, 창 안에서 버린 건은 창을 늘리지 않는다.
    clock 은 단조 시계다(테스트는 가짜 시계를 넣는다).
    """

    def __init__(
        self,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._last_allowed: float | None = None

    def allow(self) -> bool:
        """이번 건을 보내도 되면 True(창을 새로 시작한다), 창 안이면 False."""
        # 읽기 · 비교 · 갱신을 한 잠금 안에서 한다(동시에 온 이벤트는 한 건만 통과).
        with self._lock:
            last, now = self._last_allowed, self._clock()
            if last is not None and now - last < self._interval:
                return False
            self._last_allowed = now
            return True


# @MX:NOTE: [AUTO] 프로세스 전역 상태(마지막으로 보낸 RedisError 이벤트 시각). 워커
#            프로세스마다 따로 센다 — 운영은 uvicorn 한 프로세스(Dockerfile CMD 에
#            --workers 없음)라 60초에 한 건이다. 테스트는 가짜 시계를 쓰는 새 인스턴스로
#            바꾼다(tests/test_sentry_redis_throttle_fix.py).
_redis_error_throttle = EventThrottle(REDIS_ERROR_EVENT_INTERVAL_SECONDS)


def before_send(
    event: dict[str, Any],
    hint: dict[str, Any],
) -> dict[str, Any] | None:
    # hint 는 SDK 가 넘기는 원본 정보다. 예외 이벤트면 exc_info=(type, value, tb)가 있어
    # RedisError 이벤트를 가려내는 데 쓴다(없거나 None 이면 예외 이벤트가 아니다).
    if settings.APP_ENV in ["local", "debug"]:
        return None  # 완전히 차단하고 싶으면 이렇게

    # Redis 장애 중에는 요청마다 RedisError 이벤트가 생긴다(redis_error_handler 의 503).
    # 프로세스당 60초에 한 건만 보내고 나머지는 버린다. 그 밖의 이벤트는 그대로 보낸다.
    exc_info = hint.get("exc_info")
    if (
        exc_info is not None
        and isinstance(exc_info[1], redis.RedisError)
        and not _redis_error_throttle.allow()
    ):
        return None

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
