import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi_pagination import add_pagination
from sentry_sdk import capture_exception
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import DataError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from app.api import (
    auth,
    device_push_token,
    phonebook,
    shop,
    summary,
    treatment,
    treatment_menu,
    user,
)
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.rate_limit import limiter
from app.core.sentry import init_sentry
from app.docs import api_change
from app.docs.tags_metadata import tags_metadata
from app.exceptions import CustomException, data_error_handler

# 기타

# 로그 설정 (컨테이너에서는 LOG_TO_FILE=false 로 stdout 만 사용)
setup_logging(app_env=settings.APP_ENV, log_to_file=settings.LOG_TO_FILE)

# Sentry 설정
# SENTRY_DSN 이 비어 있어도 init_sentry 를 무조건 호출한다(현행 동작 보존).
# sentry_sdk.init(dsn="") 는 no-op 이므로 예외 없이 Sentry 비활성으로 기동한다.
init_sentry(
    dsn=settings.SENTRY_DSN,
    environment=settings.APP_ENV,
    traces_sample_rate=0.2,
    profiles_sample_rate=0.0,
)


def api_docs_enabled(app_env: str) -> bool:
    """문서(/docs · /redoc · /openapi.json · /changelog) 노출 여부를 돌려준다.

    production 은 노출하지 않는다(엔드포인트 · 스키마 정찰 차단). local/debug/stage 는
    기존처럼 노출한다.
    """
    return app_env != "production"


def api_docs_kwargs(app_env: str) -> dict[str, str | None]:
    """APP_ENV 에 맞는 FastAPI 문서 경로 인자(None 이면 해당 경로를 만들지 않는다)."""
    if api_docs_enabled(app_env):
        return {
            "docs_url": "/docs",
            "redoc_url": "/redoc",
            "openapi_url": "/openapi.json",
        }
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


app = FastAPI(
    title="뷰티앱",
    description=(
        "이 프로젝트는 FastAPI로 개발된 API 서비스입니다.\n\n"
        "- 공통 미들웨어를 통해 사용자 인증 및 샵(상점) 선택 정보가 "
        "응답에 포함됩니다.\n"
        "- 액세스 토큰이 만료되었거나 리프레시 토큰이 없으면 "
        "`401 Unauthorized` 에러가 발생합니다.\n"
        "- 상점이 선택되지 않은 경우, 다음과 같은 에러 응답이 반환됩니다:\n"
        "  ```json\n"
        "  {\n"
        '    "detail": {\n'
        '      "code": "SHOP_NOT_SELECTED",\n'
        '      "message": "상점이 선택되지 않았습니다."\n'
        "    }\n"
        "  }\n\n"
        "- 각 API의 응답에는 도메인별 에러 코드가 포함되며, 코드 앞에는 "
        "도메인 이름(`{DOMAIN}`)이 붙습니다.\n"
        "  예: `USER_NOT_FOUND`, `TREATMENT_MENU_CONFLICT`, "
        "`PHONEBOOK_VALIDATION_ERROR` 등\n"
        "- `{DOMAIN}` 값은 각 API 설명 옆에 명시되어 있습니다."
    ),
    version="1.0.0",
    **api_docs_kwargs(settings.APP_ENV),
    debug=settings.APP_ENV == "debug",
    openapi_tags=tags_metadata,
)

# Rate limiting (slowapi) 등록: limiter 상태 + 429 예외 핸들러
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# 서비스가 감싸지 않은 DataError(목록 검색어의 NUL 문자 등)도 500 이 아니라 400 이다.
# 서비스가 감싼 경로는 CustomException 이 같은 400 으로 바꾼다(app/exceptions.py).
app.add_exception_handler(DataError, data_error_handler)


# CORS 설정 (Cross-Origin Resource Sharing)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://kmc2.daeho2.shop",
        "https://bueafit.vercel.app",
    ],
    # http/https + localhost/127.0.0.1/::1 + 10/172.16-31/192.168 대역 + 선택적 포트
    allow_origin_regex=r"^https?://("
    r"localhost|127\.0\.0\.1|\[::1\]|"
    r"10(?:\.\d{1,3}){3}|"
    r"172\.(?:1[6-9]|2[0-9]|3[0-1])(?:\.\d{1,3}){2}|"
    r"192\.168(?:\.\d{1,3}){2}"
    r")(?::\d+)?$",
    allow_credentials=True,  # 쿠키 허용
    # 와일드카드 대신 실제 사용하는 메서드만 명시적으로 허용한다.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    # 와일드카드 대신 실제 사용하는 헤더만 명시적으로 허용한다.
    # - Content-Type: JSON/폼 요청 본문
    # - Authorization: Bearer 액세스 토큰
    # - X-Refresh-Token: 리프레시 토큰 헤더 전달(auth_service.py:103)
    allow_headers=["Content-Type", "Authorization", "X-Refresh-Token"],
)


@app.middleware("http")
async def error_logger(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    try:
        # call_next 의 반환값을 Response 타입으로 명시
        response: Response = await call_next(request)
    except CustomException:
        raise
    except Exception as e:
        # 핸들되지 않은 예외(500) 일 때 스택트레이스 포함 로깅
        logging.exception(
            "[UNHANDLED 500] %s %s %s",
            request.client.host,
            request.method,
            request.url.path,
        )
        # Sentry 전송
        capture_exception(e)
        raise
    return response


# 헬스체크 엔드포인트 (서버 상태 확인)
@app.get("/health", tags=["System"])
async def health_check() -> dict:
    return {"status": "ok", "message": "API 서버가 정상 동작 중입니다."}


# 사용자 관련 API 라우터 등록
app.include_router(user.router)
app.include_router(auth.router)
app.include_router(phonebook.router)
app.include_router(treatment.router)
app.include_router(treatment_menu.router)
app.include_router(shop.router)
app.include_router(summary.router)
app.include_router(device_push_token.router)
# API 변경 이력(/changelog)도 문서라 production 에서는 등록하지 않는다.
if api_docs_enabled(settings.APP_ENV):
    app.include_router(api_change.router)

# FastAPI Pagination 설정
add_pagination(app)
