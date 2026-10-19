import json
import logging
from datetime import date

import redis

from app.core.redis_client import redis_client

REDIS_PREFIX = "dashboard"
REDIS_TTL = 1800  # 30분


def day_period(day: date) -> str:
    """일 단위 집계 기간 키 조각(KST 날짜)."""
    return f"day:{day.isoformat()}"


def month_period(day: date) -> str:
    """월 단위 집계 기간 키 조각(KST 연-월)."""
    return f"month:{day:%Y-%m}"


def _version_key(shop_id: int) -> str:
    return f"{REDIS_PREFIX}:{shop_id}:version"


def get_dashboard_cache_version(shop_id: int) -> int:
    """상점의 현재 대시보드 캐시 버전(무효화한 적이 없으면 0)."""
    raw = redis_client.get(_version_key(shop_id))
    return 0 if raw is None else int(raw)


# @MX:ANCHOR: [AUTO] 대시보드 캐시 무효화 단일 진입점(예약 생성·수정·취소, 자동완료).
# @MX:REASON: 버전을 올려 상점의 기존 키를 모두 버린다(키에 버전 포함, TTL 로 만료).
#             반드시 DB 커밋이 성공한 뒤 호출해야 한다. 커밋 전에 올리면 다른 요청이
#             새 버전 키에 커밋 전 데이터를 캐시해 TTL 동안 이전 값이 남는다.
#             날짜 계산(KST/UTC) 없이 상점 단위로 무효화하므로 영향 날짜 누락이 없다.
def bump_dashboard_cache_version(shop_id: int) -> None:
    """상점의 대시보드 캐시를 무효화한다(커밋 성공 후 호출).

    Redis 오류는 이미 커밋된 쓰기를 실패(500)로 만들지 않도록 기록만 한다. 이 경우
    최대 TTL(30분) 동안 이전 캐시가 보일 수 있다.
    """
    try:
        redis_client.incr(_version_key(shop_id))
    except redis.RedisError:
        logging.warning(
            "dashboard cache invalidation failed for shop %s",
            shop_id,
            exc_info=True,
        )


def get_dashboard_cache_key(
    shop_id: int,
    version: int,
    field: str,
    period: str,
) -> str:
    """Redis 키 생성 함수. period 는 day_period()/month_period() 결과다."""
    return f"{REDIS_PREFIX}:{shop_id}:v{version}:{field}:{period}"


def set_dashboard_cache(key: str, value: dict | list, ttl: int = REDIS_TTL) -> None:
    serialized = json.dumps(value, ensure_ascii=False)
    redis_client.set(key, serialized, ex=ttl)


def get_dashboard_cache(key: str) -> dict | list | None:
    raw = redis_client.get(key)
    if raw is None:
        return None
    return json.loads(raw)


def clear_dashboard_cache(key: str) -> None:
    redis_client.delete(key)
