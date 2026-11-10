"""Sentry 로 보내는 Redis 오류 이벤트 스로틀 테스트 (Redis 장애 알림 폭주).

대상: app/core/sentry.py::before_send (+ 프로세스 단위 스로틀 EventThrottle)

결함: 처리되지 않은 Redis 오류는 모두 redis_error_handler(app/exceptions.py)에서
CustomException(503, exception=exc) 가 되고, 5xx 라서 capture_exception(exc) 된다.
Starlette 연동은 RedisError 에 status_code 가 없어 다시 보내지 않으므로, Redis 장애
동안에는 요청마다 이벤트가 한 건씩 Sentry 로 갔다.

요구 동작(사용자 결정, 2026-11-10):
- RedisError 이벤트(하위 클래스 포함)는 프로세스당 60초에 한 건만 보낸다. 첫 건은 바로
  보내고 60초 안의 나머지는 before_send 에서 버린다. 60초 이상 지난 뒤의 첫 건은 다시
  보내고 창을 새로 시작한다(운영은 uvicorn 한 프로세스다).
- Redis 가 아닌 예외 이벤트와 exc_info 가 없는 이벤트는 그대로 보낸다.
- APP_ENV local/debug 는 지금처럼 모든 이벤트를 버린다(이 검사가 먼저다).

Sentry 클라이언트를 초기화하지 않고, SDK 가 넘기는 것과 같은 모양의 hint 로
before_send 를 직접 부른다. 스로틀은 테스트마다 가짜 시계를 쓰는 새 인스턴스로 바꿔
테스트 순서와 무관하다. DB · Redis 는 쓰지 않는다.
"""

import threading
import time

import pytest
import redis

from app.core import sentry as sentry_module

# 사용자 결정: RedisError 이벤트는 프로세스당 60초에 한 건.
INTERVAL_SECONDS = 60
REDIS_DOWN = "Error 111 connecting to redis:6379. Connection refused."


class FakeClock:
    """테스트가 직접 움직이는 단조 시계(초)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _hint(error: BaseException) -> dict[str, object]:
    """capture_exception 이 before_send 에 넘기는 hint 모양(exc_info 3튜플)."""
    return {"exc_info": (type(error), error, error.__traceback__)}


def _redis_hint() -> dict[str, object]:
    return _hint(redis.ConnectionError(REDIS_DOWN))


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """테스트마다 가짜 시계를 쓰는 새 스로틀로 바꾸고 APP_ENV 를 운영 값으로 둔다."""
    fake = FakeClock()
    throttle = sentry_module.EventThrottle(
        sentry_module.REDIS_ERROR_EVENT_INTERVAL_SECONDS,
        clock=fake,
    )
    monkeypatch.setattr(sentry_module, "_redis_error_throttle", throttle)
    monkeypatch.setattr(sentry_module.settings, "APP_ENV", "production")
    return fake


@pytest.mark.parametrize(
    "later_error",
    [
        pytest.param(redis.ConnectionError(REDIS_DOWN), id="same-error"),
        pytest.param(redis.TimeoutError("Timeout reading from socket"), id="timeout"),
        pytest.param(
            redis.exceptions.BusyLoadingError("Redis is loading the dataset"),
            id="nested-subclass",
        ),
    ],
)
def test_second_redis_error_event_within_interval_is_dropped(
    later_error: redis.RedisError,
) -> None:
    """첫 RedisError 이벤트는 보내고, 60초 안에 이어진 RedisError 이벤트는 버린다.

    하위 클래스(타임아웃 · 로딩 중 등)도 같은 창을 쓴다.
    """
    first, later = {"event_id": "first"}, {"event_id": "later"}

    assert sentry_module.before_send(first, _redis_hint()) is first
    assert sentry_module.before_send(later, _hint(later_error)) is None


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ValueError("boom"), id="value-error"),
        # 내장 ConnectionError · TimeoutError 는 RedisError 가 아니다(이름이 같을 뿐).
        pytest.param(ConnectionError("db connection lost"), id="builtin-connection"),
        pytest.param(TimeoutError("upstream timed out"), id="builtin-timeout"),
    ],
)
def test_non_redis_event_is_never_dropped(error: Exception) -> None:
    """대조군: Redis 가 아닌 예외 이벤트는 RedisError 이벤트 직후에도 매번 보낸다."""
    redis_event = {"event_id": "redis"}
    assert sentry_module.before_send(redis_event, _redis_hint()) is redis_event

    for event_id in ("other-1", "other-2"):
        event = {"event_id": event_id}
        assert sentry_module.before_send(event, _hint(error)) is event


def test_redis_error_event_passes_again_after_interval(clock: FakeClock) -> None:
    """대조군: 60초 이상 지나면 RedisError 이벤트를 다시 보내고 창이 새로 시작된다.

    창 안에서 버린 이벤트는 창을 늘리지 않는다.
    """
    events = [{"event_id": f"redis-{i}"} for i in range(5)]

    assert sentry_module.before_send(events[0], _redis_hint()) is events[0]
    clock.now += INTERVAL_SECONDS - 0.5
    assert sentry_module.before_send(events[1], _redis_hint()) is None
    clock.now += 0.5  # 첫 건으로부터 정확히 60초
    assert sentry_module.before_send(events[2], _redis_hint()) is events[2]
    clock.now += INTERVAL_SECONDS - 0.5
    assert sentry_module.before_send(events[3], _redis_hint()) is None
    clock.now += 0.5
    assert sentry_module.before_send(events[4], _redis_hint()) is events[4]


@pytest.mark.parametrize("app_env", ["local", "debug"])
def test_local_and_debug_drop_every_event(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    """대조군: local/debug 는 종류와 상관없이 모든 이벤트를 버린다.

    이 검사가 스로틀보다 먼저라, 여기서 버린 RedisError 는 60초 창을 쓰지 않는다.
    """
    monkeypatch.setattr(sentry_module.settings, "APP_ENV", app_env)
    for hint in (_redis_hint(), _hint(ValueError("boom")), {}):
        assert sentry_module.before_send({"event_id": "local"}, hint) is None

    monkeypatch.setattr(sentry_module.settings, "APP_ENV", "production")
    event = {"event_id": "production"}
    assert sentry_module.before_send(event, _redis_hint()) is event


@pytest.mark.parametrize(
    "hint",
    [
        pytest.param({}, id="no-hint"),
        pytest.param({"exc_info": None}, id="exc-info-none"),
    ],
)
def test_event_without_exc_info_passes(hint: dict[str, object]) -> None:
    """대조군: exc_info 가 없는 이벤트(로그 메시지 등)는 Redis 창 안에서도 보낸다."""
    redis_event = {"event_id": "redis"}
    assert sentry_module.before_send(redis_event, _redis_hint()) is redis_event

    event = {"event_id": "message"}
    assert sentry_module.before_send(event, hint) is event


def test_concurrent_redis_error_events_pass_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """동시에 온 RedisError 이벤트도 한 건만 보낸다(읽기 · 비교 · 갱신이 한 잠금 안).

    느린 시계로 "마지막 시각을 읽은 뒤 · 갱신하기 전" 틈에 다른 스레드로 넘어가게 해,
    잠금이 없으면 여러 스레드가 갱신 전 상태를 보고 함께 통과하게 만든다.
    """

    def slow_clock() -> float:
        time.sleep(0.01)
        return 1000.0

    throttle = sentry_module.EventThrottle(INTERVAL_SECONDS, clock=slow_clock)
    monkeypatch.setattr(sentry_module, "_redis_error_throttle", throttle)
    senders = 8
    barrier = threading.Barrier(senders)
    sent: list[dict | None] = []

    def send(event_id: int) -> None:
        barrier.wait()
        sent.append(sentry_module.before_send({"event_id": event_id}, _redis_hint()))

    threads = [threading.Thread(target=send, args=(i,)) for i in range(senders)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(sent) == senders
    assert sum(event is not None for event in sent) == 1
