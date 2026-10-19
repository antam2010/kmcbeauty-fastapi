from fastapi import Depends, status
from fastapi.security import OAuth2PasswordBearer
from sentry_sdk import set_user
from sqlalchemy.orm import Session

from app.core.security import TokenDecodeError, decode_jwt_token
from app.crud.user_crud import get_user_by_id
from app.database import get_db
from app.exceptions import CustomException
from app.models.user import User
from app.utils.redis.user import clear_user_redis, get_user_redis, set_user_redis

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
DOMAIN = "AUTH"


# @MX:ANCHOR: [AUTO] bearer 인증 단일 진입점 — 모든 보호 라우터가 의존한다.
# @MX:REASON: 액세스 토큰(type=access)만 허용하고 소프트삭제 유저를 차단하는 불변식의
#             유일한 지점. 같은 키로 서명된 refresh 토큰이 bearer 로 통과하거나 삭제
#             유저가 DB 폴백으로 인증되면 로그아웃·탈퇴의 세션 무효화가 무력화된다.
def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = decode_jwt_token(token)
        # 액세스 토큰만 bearer 로 허용한다. refresh 토큰도 같은 키로 서명되므로
        # type 을 보지 않으면 만료가 긴 refresh 토큰이 API 인증을 그대로 통과한다.
        if payload.get("type") != "access":
            error_msg = "token type is not access"
            raise ValueError(error_msg)

        user_id = payload.get("sub")
        if not user_id:
            error_msg = "sub(claim) not found in token"
            raise ValueError(error_msg)

        user_id = int(user_id)

    except (TokenDecodeError, ValueError) as e:
        raise CustomException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            domain=DOMAIN,
            hint="유효하지 않은 인증 토큰입니다.",
            exception=e,
        ) from e

    # Redis → fallback to DB
    if user_redis := get_user_redis(user_id):
        user = User(**user_redis)
    else:
        # 소프트삭제 유저는 제외한다(get_user_by_id 가 deleted_at IS NULL 로 거른다).
        # 캐시 항목에는 deleted_at 이 없으므로 탈퇴 시 delete_user_service 가 지운다.
        user = get_user_by_id(db, user_id)
        if not user:
            raise CustomException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                domain=DOMAIN,
                hint="사용자를 찾을 수 없습니다.",
            )
        set_user_redis(user)  # ORM 객체를 캐싱

    # Sentry 사용자 식별 정보 설정 — id 만 보낸다(email 등 PII 는 전송하지 않는다).
    set_user({"id": user.id})
    return user


def clear_current_user_cache(
    user_id: int,
) -> None:
    """현재 사용자 캐시를 제거합니다.

    주로 로그아웃 시 사용됩니다.
    """
    clear_user_redis(user_id)
