import redis

from app.core.config import settings

# Redis 접속 URL은 중앙 settings 에서 단일 정의한다(REQ-INFRA-103).
# - Swarm 에서는 항상 서비스 이름(redis)으로 접근하므로 기본값이 서비스 이름이다.
# - 로컬 개발 등에서는 REDIS_URL 환경변수로 오버라이드할 수 있다.
#   (예: redis://localhost:6379/0)
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
