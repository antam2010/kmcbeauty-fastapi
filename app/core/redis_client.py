import redis

from app.core.config import settings

# Redis 접속 URL은 중앙 settings 에서 단일 정의한다(REQ-INFRA-103).
# - 기본값은 compose 서비스 이름(redis)이다. 운영은 공유 Redis 의 비밀번호 포함 URL 을
#   시크릿 파일(/run/secrets/REDIS_URL)로 받는다(compose.prod.yaml).
# - 로컬 개발 등에서는 REDIS_URL 환경변수로 오버라이드할 수 있다.
#   (예: redis://localhost:6379/0)
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
