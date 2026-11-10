"""애플리케이션 전역 rate limiter 정의.

slowapi Limiter 를 별도 모듈로 분리하여 main.py 와 라우터(api/auth.py) 간
순환 참조 없이 공유한다. IP(get_remote_address) 기준으로 제한한다.

클라이언트 IP 는 uvicorn ProxyHeadersMiddleware 가 신뢰 프록시(FORWARDED_ALLOW_IPS)의
X-Forwarded-For 로 복원한 값이다. 운영에서 이 값이 비면 모든 요청이 NGINX IP 로 보여
한도가 전 사용자 공용이 된다(docs/deployment.md 참조).
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings
from app.core.redis_client import REDIS_SOCKET_TIMEOUT_SECONDS

# IP 기반 rate limiter. 로그인 등 인증 엔드포인트 보호에 사용한다.
# 저장소는 settings.RATE_LIMIT_STORAGE_URI 로 주입한다(기본 memory:// — 로컬/테스트).
# storage_uri 를 명시하므로 slowapi 자체 설정(RATELIMIT_STORAGE_URL)은 쓰이지 않는다.
# storage_options 는 Redis 저장소의 redis.from_url 인자다(memory:// 는 무시한다). 연결 ·
# 응답 대기 상한이 유저 캐시 클라이언트와 같은 2초다. 없으면 응답 없는 Redis 에 한도
# 검사가 끝없이 막히고, 한도가 걸린 엔드포인트는 모두 sync 라 스레드풀 스레드를
# 붙잡는다. 넘으면 RedisError 로 503 이다(app/main.py 의 Redis 오류 처리기).
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=settings.RATE_LIMIT_STORAGE_URI,
    storage_options={
        "socket_connect_timeout": REDIS_SOCKET_TIMEOUT_SECONDS,
        "socket_timeout": REDIS_SOCKET_TIMEOUT_SECONDS,
    },
)
