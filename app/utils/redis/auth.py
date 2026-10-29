from app.core.config import settings
from app.core.redis_client import redis_client

REDIS_PREFIX = "auth:refresh"
REDIS_TTL = settings.REFRESH_TOKEN_EXPIRE_SECONDS


def _get_refresh_token_key(user_id: int) -> str:
    return f"{REDIS_PREFIX}:{user_id}"


def set_refresh_token_redis(user_id: int, token: str) -> None:
    """Redis에 리프레시 토큰 저장."""
    key = _get_refresh_token_key(user_id)
    redis_client.setex(key, REDIS_TTL, token)


def get_refresh_token_redis(user_id: int) -> str | None:
    """Redis에서 리프레시 토큰 조회."""
    key = _get_refresh_token_key(user_id)
    return redis_client.get(key)


def get_refresh_token_ttl(user_id: int) -> int:
    """Redis에서 리프레시 토큰의 TTL 조회."""
    key = _get_refresh_token_key(user_id)
    ttl = redis_client.ttl(key)
    return ttl if ttl >= 0 else -1  # -1은 키가 없거나 만료된 경우를 의미합니다.


def clear_refresh_token_redis(user_id: int) -> None:
    """Redis에서 리프레시 토큰 삭제."""
    key = _get_refresh_token_key(user_id)
    redis_client.delete(key)
