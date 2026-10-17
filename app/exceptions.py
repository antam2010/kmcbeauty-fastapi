import logging
import sys

from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from sentry_sdk import capture_exception
from sqlalchemy.exc import DataError
from starlette import status
from starlette.responses import Response

from app.core.config import settings

# 기본 메시지 매핑
DEFAULT_MESSAGES = {
    status.HTTP_400_BAD_REQUEST: ("BAD_REQUEST", "잘못된 요청입니다."),
    status.HTTP_401_UNAUTHORIZED: ("UNAUTHORIZED", "인증이 필요합니다."),
    status.HTTP_403_FORBIDDEN: ("FORBIDDEN", "접근이 금지되었습니다."),
    status.HTTP_404_NOT_FOUND: ("NOT_FOUND", "요청한 리소스를 찾을 수 없습니다."),
    status.HTTP_409_CONFLICT: ("CONFLICT", "요청이 충돌되었습니다."),
    status.HTTP_422_UNPROCESSABLE_ENTITY: (
        "UNPROCESSABLE_ENTITY",
        "요청 유효성 오류입니다.",
    ),
    status.HTTP_500_INTERNAL_SERVER_ERROR: (
        "INTERNAL_ERROR",
        "서버 오류가 발생했습니다.",
    ),
}

# DB 가 요청 값을 거절한 DataError 의 응답(일반 문구). 원문(SQL · 파라미터 · 드라이버
# 메시지)은 싣지 않는다.
DATA_ERROR_CODE = "BAD_REQUEST"
DATA_ERROR_DETAIL = "처리할 수 없는 값이 요청에 있습니다."
DATA_ERROR_HINT = "입력 값을 확인해 주세요."


def _data_error_of(exception: BaseException | None) -> DataError | None:
    """오류의 원인이 DataError 면 돌려준다.

    exception 을 넘기지 않고 `raise CustomException(...) from e` 만 하는 서비스도 있어,
    그때는 생성 시점에 처리 중인 예외(sys.exception())를 본다.
    """
    cause = exception if exception is not None else sys.exception()
    return cause if isinstance(cause, DataError) else None


# @MX:ANCHOR: [AUTO] 서비스 오류 응답의 단일 생성 지점(응답 모양 · 로그 · Sentry).
# @MX:REASON: 거의 모든 서비스가 호출한다(fan_in 수십). 응답 {code, detail, hint}
#             모양, debug 에서만 원문 노출, 5xx 만 Sentry, DataError 는 400 이라는
#             규약을 여기서 지킨다(tests/test_exception_disclosure_security.py ·
#             tests/test_data_error_400_fix.py).
class CustomException(HTTPException):
    def __init__(  # noqa: PLR0913  # 에러 응답 구성 필드가 본질적으로 많다(키워드 전용)
        self,
        *,
        status_code: int,
        domain: str | None = None,
        code: str | None = None,
        detail: str | None = None,
        hint: str | None = "놉",
        exception: Exception | None = None,
    ) -> None:
        # @MX:NOTE: [AUTO] DataError(요청 값을 DB 가 거절: NUL 문자 · 범위 초과 ·
        #            형식 오류)는 서버 결함이 아니라 잘못된 요청이다. 서비스가
        #            SQLAlchemyError 를 500 으로 감싸도 여기서 일반 문구의 400
        #            으로 바꾼다. 5xx 가 아니므로 Sentry 로 보내지 않고 아래 경고
        #            로그만 남는다. DataError 가 아닌 오류의 상태 · 문구는 그대로다.
        data_error = _data_error_of(exception)
        if data_error is not None and status_code >= 500:
            status_code = status.HTTP_400_BAD_REQUEST
            code, detail, hint = DATA_ERROR_CODE, DATA_ERROR_DETAIL, DATA_ERROR_HINT
            exception = data_error  # `from e` 만 한 경로도 로그에 원인을 남긴다

        # 기본 메시지 및 코드 설정
        default_code, default_detail = DEFAULT_MESSAGES.get(
            status_code,
            ("UNKNOWN_ERROR", "알 수 없는 오류입니다."),
        )
        if domain is None:
            final_code = f"{code}"
        else:
            final_code = f"{domain.upper()}_{code or default_code}"

        final_detail = detail or default_detail

        # 예외 메시지 변환 (항상 문자열, 없으면 "-")
        exception_str = str(exception) if exception is not None else "-"

        # 응답 구조 정의
        error_response = {
            "code": final_code,
            "detail": final_detail,
        }

        if hint:
            error_response["hint"] = hint

        # 정보 노출 방지: raw exception 문자열은 debug 환경에서만 응답 본문에 포함한다.
        # 비-debug 환경에서는 로깅/Sentry 로만 기록하고 응답에는 노출하지 않는다.
        # DataError 원문(SQL · 바인드 값)은 debug 에서도 싣지 않는다(로그에만 남긴다).
        if exception and settings.APP_ENV == "debug" and data_error is None:
            error_response["exception"] = exception_str

        # 500 이상 에러는 Sentry로
        if status_code >= 500:
            capture_exception(
                exception or Exception(f"{final_code}: {final_detail} ({hint})"),
            )

        logging.warning(
            "CustomException: %s - %s (%s) | Exception: %s",
            final_code,
            final_detail,
            hint,
            exception_str,
            extra={
                "status_code": status_code,
                "domain": domain,
                "code": final_code,
                "detail": final_detail,
                "hint": hint if hint else "-",
                "exception": exception_str,
            },
        )

        super().__init__(
            status_code=status_code,
            detail=error_response,
        )


async def data_error_handler(request: Request, exc: Exception) -> Response:
    """서비스가 감싸지 않은 DataError(예: 목록 검색어의 NUL 문자)를 400 응답으로 바꾼다.

    서비스가 감싼 경로(CustomException(500, exception=e))와 같은 생성 경로를 태워 같은
    400 일반 문구 · 경고 로그가 되게 한다. 처리되지 않은 예외로 남지 않으므로
    error_logger 미들웨어의 ERROR 로그 · Sentry 전송도 없다(app/main.py 에서 등록).
    """
    error = CustomException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        exception=exc,
    )
    return await http_exception_handler(request, error)
