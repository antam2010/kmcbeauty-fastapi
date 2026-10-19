from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError
from passlib.context import CryptContext

from app.core.config import settings

# 신규 해시는 argon2(argon2id), 기존 bcrypt 해시는 검증만 허용(deprecated).
# - default="argon2": hash_password() 는 항상 argon2id 로 해싱한다.
# - schemes 에 "bcrypt" 를 남겨 DB 의 레거시 bcrypt 해시도 계속 verify 된다
#   (제거하면 기존 사용자가 로그인 불가 상태가 된다).
# - deprecated=["bcrypt"]: bcrypt 해시는 "구식"으로 표시되어 verify_and_update()
#   가 재해싱 대상으로 인식한다(로그인 성공 시 argon2 로 자동 승급).
pwd_context = CryptContext(
    schemes=["argon2", "bcrypt"],
    deprecated=["bcrypt"],
    default="argon2",
)

fernet = Fernet(settings.FERNET_KEY.encode())


class TokenDecodeError(Exception):
    """JWT 디코딩 실패 시 커스텀 예외."""


def hash_password(password: str) -> str:
    """평문 비밀번호를 argon2id 해시로 변환한다(기본 스킴).

    passlib 의 default 스킴(argon2)으로만 새 해시를 생성하므로, 이 함수의
    출력은 항상 `$argon2id$...` 형식이다. bcrypt 는 검증 전용으로만 유지된다.
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """평문과 저장된 해시를 비교한다(argon2/bcrypt 모두 지원).

    schemes 에 bcrypt 가 남아있으므로 레거시 bcrypt 해시도 그대로 검증된다.
    승급(re-hash)이 필요한 호출부는 verify_and_upgrade_password 를 사용한다.
    """
    return pwd_context.verify(plain_password, hashed_password)


# @MX:ANCHOR: [AUTO] 비밀번호 검증+무중단 해시 승급의 단일 진입점(bcrypt -> argon2).
# @MX:REASON: 인증 핵심 경로. 레거시 bcrypt 해시를 로그인 성공 시점에 argon2 로
#             교체(rehash-on-login)하는 불변식을 보장한다. 반환된 new_hash 가
#             None 이 아니면 반드시 DB 에 영속화해야 승급이 완료된다.
#             (SPEC-SECURITY-001 REQ-SEC-002 인증 로직 무약화 유지)
def verify_and_upgrade_password(
    plain_password: str,
    hashed_password: str,
) -> tuple[bool, str | None]:
    """비밀번호를 검증하고, 구식 해시면 argon2 로 재해싱한 값을 함께 반환한다.

    passlib 의 verify_and_update 를 감싼다.

    - 반환값: (검증 성공 여부, 새 해시 또는 None)
      - 검증 실패: (False, None)
      - 검증 성공 + 해시가 최신(argon2): (True, None)  # 재저장 불필요
      - 검증 성공 + 해시가 구식(bcrypt): (True, "<새 argon2 해시>")

    호출부는 new_hash 가 None 이 아닐 때에만 user.password 를 갱신하면 된다.
    verify_and_update 는 검증 실패 시 new_hash 로 None 을 반환하므로,
    승급 로직이 인증 실패를 우회시키는 일은 없다.
    """
    valid, new_hash = pwd_context.verify_and_update(plain_password, hashed_password)
    return valid, new_hash


def create_jwt_token(data: dict, expires_delta: timedelta) -> str:
    to_encode = data.copy()
    now = datetime.now(UTC)
    expire = now + expires_delta

    to_encode.update(
        {
            "exp": expire,  # 만료 시각
            "iat": now,  # 발급 시각
            "nbf": now,  # 이 시점부터 유효 (optional)
        },
    )
    return jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def decode_jwt_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
    except ExpiredSignatureError as e:
        raise TokenDecodeError(e) from e
    except JWTError as e:
        raise TokenDecodeError(e) from e
    except Exception as e:
        raise TokenDecodeError(e) from e


def encrypt_token(token: str) -> str:
    return fernet.encrypt(token.encode()).decode()


def decrypt_token(token: str) -> str:
    return fernet.decrypt(token.encode()).decode()
