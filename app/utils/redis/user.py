import json
import logging
from typing import Any

import redis

from app.core.redis_client import redis_client
from app.models.user import User

logger = logging.getLogger(__name__)

REDIS_USER_PREFIX = "user"
REDIS_USER_TTL = 60 * 60 * 24  # 24시간


def _get_user_key(user_id: int) -> str:
    return f"{REDIS_USER_PREFIX}:{user_id}"


def get_user_redis(user_id: int) -> dict[str, Any] | None:
    """Redis에서 사용자 정보를 조회.

    Redis 오류(장애 · 타임아웃)는 캐시 미스(None)로 본다. 호출자(get_current_user)가 DB
    에서 읽으므로 Redis 가 죽어도 인증은 된다. 로그에는 user_id 만 남긴다(PII 없음).
    """
    key = _get_user_key(user_id)
    try:
        data = redis_client.get(key)
    except redis.RedisError:
        logger.warning(
            "user cache read failed for user %s; loading from DB",
            user_id,
            exc_info=True,
        )
        return None
    if data:
        return json.loads(data)
    return None


def set_user_redis(user: User) -> None:
    """사용자 정보를 Redis에 저장.

    Redis 오류는 기록만 한다(인증은 이미 DB 로 끝났고, 캐시는 다음 요청이 다시 채운다).
    로그에는 user_id 만 남긴다(PII 없음).
    """
    key = _get_user_key(user.id)

    user_dict = {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
    }

    try:
        redis_client.setex(key, REDIS_USER_TTL, json.dumps(user_dict))
    except redis.RedisError:
        logger.warning("user cache write failed for user %s", user.id, exc_info=True)


def clear_user_redis(user_id: int) -> None:
    """사용자 정보를 Redis에서 삭제.

    오류를 삼키지 않는다: 지우지 못한 캐시가 남으면 탈퇴 · 정보 변경이 TTL(24시간) 동안
    인증에 반영되지 않는다. 처리되지 않은 Redis 오류는 503 이다(app/main.py).
    """
    key = _get_user_key(user_id)
    redis_client.delete(key)
