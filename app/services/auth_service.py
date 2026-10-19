import logging
from datetime import timedelta

from fastapi import Request, status
from jose import ExpiredSignatureError, JWTError, jwt
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_jwt_token, verify_and_upgrade_password
from app.crud.user_crud import get_user_by_email, get_user_by_id
from app.exceptions import CustomException
from app.models.user import User
from app.schemas.auth import LoginResponse
from app.utils.redis.auth import (
    clear_refresh_token_redis,
    get_refresh_token_redis,
    get_refresh_token_ttl,
    set_refresh_token_redis,
)
from app.utils.redis.user import clear_user_redis

DOMAIN = "AUTH"

logger = logging.getLogger(__name__)


def authenticate_user_service(db: Session, email: str, password: str) -> User:
    """이메일과 비밀번호를 사용하여 사용자를 인증한다.

    - db: SQLAlchemy 세션 객체
    - email: 사용자 이메일
    - password: 사용자 비밀번호
    - 반환값: 인증된 사용자 객체
    - 예외: 인증 실패 시 CustomException 발생

    부수효과(무중단 해시 승급): 저장된 해시가 레거시 bcrypt 이면 검증 성공
    시점에 argon2 로 재해싱하여 DB 에 기록한다(rehash-on-login). 승급 기록에
    실패하더라도 이미 검증된 로그인은 그대로 성공시킨다(승급은 best-effort).
    """
    user = get_user_by_email(db, email)
    # 인증 실패 분기는 기존과 동일하다: 사용자가 없거나 검증 실패 시 401.
    # verify_and_upgrade_password 는 검증 실패 시 (False, None) 을 반환하므로
    # 승급 로직이 인증 실패를 우회시키지 않는다. (SECURITY-001 무약화)
    if not user:
        raise CustomException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            domain=DOMAIN,
        )

    valid, new_hash = verify_and_upgrade_password(password, user.password)
    if not valid:
        raise CustomException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            domain=DOMAIN,
        )

    # 레거시 bcrypt 해시였다면 new_hash 에 argon2 해시가 담겨온다.
    # 검증에 성공한 경우에만 도달하므로, 이 write-back 은 로그인 성공 경로에서만
    # 일어난다.
    if new_hash is not None:
        _persist_upgraded_password_hash(db, user, new_hash)

    return user


def _persist_upgraded_password_hash(db: Session, user: User, new_hash: str) -> None:
    """승급된 argon2 해시를 DB 에 기록한다(실패는 로그인을 막지 않는다).

    비밀번호 해시 교체는 로그인 성공의 부수효과이다. 여기서 발생하는 DB 오류가
    이미 유효한 자격증명으로 인증에 성공한 사용자를 401/500 으로 떨어뜨리면
    안 되므로, 예외를 삼켜 로그(경고)만 남기고 롤백한다. 다음 로그인에서 다시
    승급을 시도하게 된다.
    """
    try:
        user.password = new_hash
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        # 비밀번호/해시 값은 로그에 남기지 않는다(민감정보). user.id 만 기록.
        logger.warning(
            "argon2 해시 승급 기록 실패(로그인은 성공 처리): user_id=%s",
            getattr(user, "id", None),
        )


def generate_tokens(user: User) -> LoginResponse:
    """유저 정보를 기반으로 액세스 토큰과 리프레시 토큰을 생성해서 반환한다.

    - 액세스 토큰: 만료 시간은 ACCESS_TOKEN_EXPIRE_SECONDS 초
    - 리프레시 토큰: 만료 시간은 REFRESH_TOKEN_EXPIRE_SECONDS 초
    """
    access_token = generate_access_token(user)  # 액세스 토큰 생성
    refresh_token = generate_refresh_token(user)  # 리프레시 토큰 생성
    set_refresh_token_redis(user.id, refresh_token)  # Redis에 리프레시 토큰 저장
    return access_token, refresh_token


def generate_access_token(user: User) -> str:
    """유저 정보를 기반으로 액세스 토큰(JWT)을 생성해서 반환한다.

    - sub: 유저 ID
    - role: 유저 권한(예: admin, user)
    - email: 유저 이메일
    - type: 'access' (액세스 토큰임을 명시)
    - 만료 시간: ACCESS_TOKEN_EXPIRE_SECONDS 초
    """
    return create_jwt_token(
        data={
            "sub": str(user.id),
            "role": user.role,
            "email": user.email,
            "type": "access",
        },
        expires_delta=timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS),
    )


def generate_refresh_token(user: User) -> str:
    """유저 정보를 기반으로 리프레시 토큰(JWT)을 생성해서 반환한다.

    - sub: 유저 ID
    - type: 'refresh' (리프레시 토큰임을 명시)
    - 만료 시간: REFRESH_TOKEN_EXPIRE_SECONDS 초
      (config/.env 의 값은 '초' 단위. days 로 잘못 해석하면 사실상 만료되지
       않는 토큰이 되어 ExpiredSignatureError 경로가 도달 불가해진다.
       SPEC-SECURITY-001 REQ-SEC-001)
    """
    return create_jwt_token(
        data={"sub": str(user.id), "type": "refresh"},
        expires_delta=timedelta(seconds=settings.REFRESH_TOKEN_EXPIRE_SECONDS),
    )


def _refresh_token_user_id(payload: dict) -> int:
    """디코딩한 리프레시 토큰 클레임에서 user_id 를 꺼낸다.

    재발급에는 리프레시 토큰(type=refresh)만 쓴다. 액세스 토큰도 같은 키로 서명되므로
    type 을 봐야 한다(Redis 대조 앞단의 심층 방어).

    :raises ValueError: type 이 refresh 가 아니거나 sub 클레임이 없거나 정수가 아닐 때
    :raises TypeError: sub 클레임을 정수로 바꿀 수 없는 타입일 때
    """
    if payload.get("type") != "refresh":
        msg = "token type is not refresh"
        raise ValueError(msg)
    sub = payload.get("sub")
    if sub is None:
        msg = "sub claim missing"
        raise ValueError(msg)
    return int(sub)


# @MX:ANCHOR: [AUTO] 토큰 재발급/회전 경계 — 세션 무효화·만료 처리의 단일 진입점
# @MX:REASON: 인증 핵심 경로. Redis 저장 토큰과 클라이언트 토큰의 정합성 및 만료 토큰
#             재사용 금지 불변식을 보장해야 함 (SPEC-SECURITY-001 REQ-SEC-001/002).
def refresh_access_token(db: Session, request: Request) -> tuple[str, str]:
    """리프레시 토큰으로 액세스 토큰과(필요 시) 새로운 리프레시 토큰을 재발급한다.

    리프레시 토큰 만료 TTL이 절반 이하일 때만 새로 발급하고,
    절반 이상 남아있으면 기존 토큰을 그대로 반환한다.

    - request: FastAPI Request 객체
    - db: SQLAlchemy 세션 객체
    - 반환값: (액세스 토큰, 리프레시 토큰)
    - 예외: 리프레시 토큰이 없거나 유효하지 않은 경우(type=refresh 가 아닌 토큰 포함)
    """
    raw_token = request.headers.get("X-Refresh-Token") or request.cookies.get(
        "refresh_token",
    )
    if not raw_token:
        raise CustomException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            domain=DOMAIN,
            detail="Refresh token not found",
            hint="헤더나 쿠키에 리프레시 토큰 확인해보슈",
        )
    try:
        payload = jwt.decode(
            raw_token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        user_id = _refresh_token_user_id(payload)
    except (ValueError, TypeError) as e:
        # 리프레시 토큰이 아니거나(type), sub 클레임이 없거나 정수로 변환할 수 없으면
        # 크래시(500) 대신 무효 토큰으로 처리하여 401을 반환한다. (AC-001-1 엣지)
        raise CustomException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            domain=DOMAIN,
            code="REFRESH_TOKEN_INVALID",
            detail="Invalid refresh token",
            hint="리프레시 토큰이 유효하지 않으니 로그인으로 보내도록",
            exception=e,
        ) from e
    except ExpiredSignatureError as e:
        # 서명 만료(ExpiredSignatureError)는 무효 토큰(JWTError)과 구분하여
        # 만료 사유가 식별 가능한 401을 반환한다.
        raise CustomException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            domain=DOMAIN,
            code="REFRESH_TOKEN_EXPIRED",
            detail="Refresh token has expired",
            hint="리프레시 토큰이 만료되었으니 재로그인이 필요합니다.",
            exception=e,
        ) from e
    except JWTError as e:
        raise CustomException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            domain=DOMAIN,
            code="REFRESH_TOKEN_INVALID",
            detail="Invalid refresh token",
            hint="리프레시 토큰이 유효하지 않으니 로그인으로 보내도록",
            exception=e,
        ) from e

    # Redis에서 리프레시 토큰 확인
    saved_token = get_refresh_token_redis(user_id)
    if not saved_token or saved_token != raw_token:
        raise CustomException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            domain=DOMAIN,
            detail="Refresh token is invalid or expired.",
            hint="리프레시 토큰이 레디스에 없으니 로그인으로 보내도록",
        )

    # 남은 TTL 확인 (초 단위). ttl<=0 은 키 없음/만료를 의미한다.
    ttl = get_refresh_token_ttl(user_id)

    # 만료(ttl<=0)된 리프레시 토큰은 재사용하여 액세스 토큰을 발급하지 않는다.
    # (off-by-one 방지: 기존 `0 < ttl <= half_ttl` 는 ttl==0 일 때 else 분기로
    #  빠져 만료 토큰을 그대로 재사용했다.)
    if ttl <= 0:
        raise CustomException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            domain=DOMAIN,
            code="REFRESH_TOKEN_EXPIRED",
            detail="Refresh token has expired",
            hint="리프레시 토큰이 만료되었으니 재로그인이 필요합니다.",
        )

    # 회전 임계치: 남은 TTL 이 "유효 만료 시간의 절반" 이하이면 회전한다.
    half_ttl = settings.REFRESH_TOKEN_EXPIRE_SECONDS / 2

    # 유저 정보 조회
    user = get_user_by_id(db, user_id)
    if not user or user.is_deleted():
        raise CustomException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            domain=DOMAIN,
            detail="User not found",
        )

    # 리프레시 토큰 재발급 여부 결정 (ttl>0 은 위에서 보장됨)
    if ttl <= half_ttl:
        # 남은 시간이 절반 이하라면 새 리프레시 토큰 발급 및 저장.
        # generate_tokens 가 이미 set_refresh_token_redis(user.id, ...) 로 저장하므로
        # 중복 저장(인자 누락 버그 포함)을 제거한다.
        new_access_token, new_refresh_token = generate_tokens(user)
    else:
        # 아직 충분히 남았으면 기존 토큰 유지
        new_access_token = generate_access_token(user)
        new_refresh_token = raw_token

    return new_access_token, new_refresh_token


def logout_user(token: str | None) -> bool:
    """사용자 로그아웃 처리.

    - token: 리프레시 토큰 (없으면 False 반환)
    - 반환값: 성공 여부 (True/False)
    """
    if not token:
        return False
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
    except ExpiredSignatureError:
        # 서명이 만료된 토큰이라도 sub 를 추출해 서버측 세션(Redis)을 반드시 폐기한다.
        # 만료를 이유로 revoke 를 건너뛰면 세션이 남아 재사용 위험이 있다.
        # (SPEC-SECURITY-001 REQ-SEC-002)
        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=[settings.ALGORITHM],
                options={"verify_exp": False},
            )
        except JWTError:
            return False
    except JWTError:
        # 서명 위조/손상 등 진짜 무효 토큰은 폐기 대상이 없으므로 False.
        return False

    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        return False
    clear_refresh_token_redis(user_id)
    clear_user_redis(user_id)
    return True
