import redis

from app.core.config import settings

# Redis 접속 URL은 중앙 settings 에서 단일 정의한다(REQ-INFRA-103).
# - 기본값은 compose 서비스 이름(redis)이다. 운영은 공유 Redis 의 비밀번호 포함 URL 을
#   시크릿 파일(/run/secrets/REDIS_URL)로 받는다(compose.prod.yaml).
# - 로컬 개발 등에서는 REDIS_URL 환경변수로 오버라이드할 수 있다.
#   (예: redis://localhost:6379/0)

# 응답 없는 Redis(네트워크 단절 · 멈춤)를 기다리는 상한(초): 연결과 명령 응답 각각.
# 타임아웃이 없으면 명령이 끝없이 막혀 요청을 처리하는 워커가 묶인다. 넘으면
# redis.TimeoutError(RedisError)로 실패한다 — 유저 캐시는 DB 로 넘어가고, 그 밖은
# 503 이다(app/main.py 의 Redis 오류 처리기).
REDIS_SOCKET_TIMEOUT_SECONDS = 2

redis_client = redis.Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
)
