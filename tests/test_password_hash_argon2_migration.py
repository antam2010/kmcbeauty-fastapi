"""비밀번호 해시 argon2 마이그레이션 테스트.

대상: app/core/security.py, app/services/auth_service.py

검증 항목:
- (1) 신규 해시는 argon2(argon2id) 형식이며 verify 로 검증된다.
- (2) 레거시 bcrypt 해시도 그대로 verify 된다(하위호환 — 기존 사용자 락아웃 방지).
- (3) verify_and_upgrade_password 가 bcrypt 해시를 argon2 로 승급한다.
- (4) 로그인 경로(authenticate_user_service)가 bcrypt 사용자를 로그인시키면서
      DB 에 argon2 해시를 write-back 한다(rehash-on-login).
- (5) DB 승급 기록이 실패해도 로그인 자체는 성공한다(승급은 best-effort).
- (6) 잘못된 비밀번호는 승급 없이 여전히 인증 실패한다(SECURITY-001 무약화).

외부 의존(네트워크/실DB) 없이 검증한다. bcrypt 벡터는 passlib 의 bcrypt 스킴으로
테스트 시점에 생성하므로(설치된 bcrypt 4.0.1 사용) 라운드 불일치가 발생하지 않는다.
"""

import pytest
from passlib.context import CryptContext

from app.core.security import (
    hash_password,
    verify_and_upgrade_password,
    verify_password,
)
from app.services import auth_service

# 레거시 해시를 재현하기 위한 bcrypt 전용 컨텍스트(테스트 픽스처 용도).
# 운영 코드(app.core.security.pwd_context)와 분리해, "과거에 bcrypt 로 저장된 해시"
# 를 결정론적으로 만들어낸다.
_bcrypt_only = CryptContext(schemes=["bcrypt"], deprecated="auto")

_PLAINTEXT = "S3cur3-P@ssw0rd!"


def _make_legacy_bcrypt_hash(password: str = _PLAINTEXT) -> str:
    """DB 에 남아있을 법한 진짜 bcrypt($2b$) 해시를 생성한다."""
    return _bcrypt_only.hash(password)


# ---------------------------------------------------------------------------
# (1) 신규 해시 = argon2
# ---------------------------------------------------------------------------
def test_new_hash_is_argon2_and_verifies() -> None:
    hashed = hash_password(_PLAINTEXT)

    # argon2id 접두사 확인 (passlib default 스킴이 argon2 임을 증명)
    assert hashed.startswith("$argon2id$")
    # 자기 자신에 대한 검증 성공
    assert verify_password(_PLAINTEXT, hashed) is True
    # 오답은 실패
    assert verify_password("wrong-password", hashed) is False


# ---------------------------------------------------------------------------
# (2) 레거시 bcrypt 해시 하위호환 — 계속 verify 됨
# ---------------------------------------------------------------------------
def test_legacy_bcrypt_hash_still_verifies() -> None:
    legacy = _make_legacy_bcrypt_hash()

    # 사전 조건: 실제로 bcrypt 형식이어야 의미가 있다.
    assert legacy.startswith(("$2b$", "$2a$"))

    # 운영 verify 함수가 bcrypt 해시를 그대로 검증(사용자 락아웃 없음)
    assert verify_password(_PLAINTEXT, legacy) is True
    assert verify_password("nope", legacy) is False


# ---------------------------------------------------------------------------
# (3) verify_and_upgrade_password: bcrypt -> argon2 승급
# ---------------------------------------------------------------------------
def test_verify_and_upgrade_upgrades_bcrypt_to_argon2() -> None:
    legacy = _make_legacy_bcrypt_hash()

    valid, new_hash = verify_and_upgrade_password(_PLAINTEXT, legacy)

    assert valid is True
    # 구식(bcrypt) 해시였으므로 새 해시(argon2)가 반환되어야 한다.
    assert new_hash is not None
    assert new_hash.startswith("$argon2id$")
    # 새 해시도 같은 비밀번호로 검증된다(승급 결과가 유효).
    assert verify_password(_PLAINTEXT, new_hash) is True


def test_verify_and_upgrade_no_rehash_for_argon2() -> None:
    """이미 argon2 해시면 재해싱하지 않는다(new_hash 는 None)."""
    current = hash_password(_PLAINTEXT)  # argon2

    valid, new_hash = verify_and_upgrade_password(_PLAINTEXT, current)

    assert valid is True
    assert new_hash is None  # 최신 스킴이므로 승급 불필요


def test_verify_and_upgrade_rejects_wrong_password() -> None:
    """검증 실패 시 (False, None) — 승급 로직이 인증 실패를 우회시키지 않는다."""
    legacy = _make_legacy_bcrypt_hash()

    valid, new_hash = verify_and_upgrade_password("totally-wrong", legacy)

    assert valid is False
    assert new_hash is None


# ---------------------------------------------------------------------------
# (4)~(6) 로그인 경로 write-back
# ---------------------------------------------------------------------------
class FakeUser:
    """authenticate_user_service 가 참조하는 최소 User 인터페이스."""

    def __init__(self, password_hash: str, user_id: int = 1) -> None:
        self.id = user_id
        self.password = password_hash


class FakeSession:
    """SQLAlchemy Session 의 commit/rollback 만 흉내낸다."""

    def __init__(self, *, fail_commit: bool = False) -> None:
        self.committed = False
        self.rolled_back = False
        self._fail_commit = fail_commit

    def commit(self) -> None:
        if self._fail_commit:
            from sqlalchemy.exc import SQLAlchemyError

            msg = "simulated commit failure"
            raise SQLAlchemyError(msg)
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def test_login_rehashes_bcrypt_user_and_writes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: bcrypt 사용자가 로그인하면 argon2 로 승급되어 DB 에 기록된다."""
    legacy = _make_legacy_bcrypt_hash()
    user = FakeUser(password_hash=legacy, user_id=42)
    db = FakeSession()

    monkeypatch.setattr(auth_service, "get_user_by_email", lambda _db, _email: user)

    result = auth_service.authenticate_user_service(db, "user@test.com", _PLAINTEXT)

    assert result is user
    # DB write-back 발생: 새 해시가 argon2 이고 commit 되었다.
    assert user.password.startswith("$argon2id$")
    assert db.committed is True
    assert db.rolled_back is False
    # 승급된 해시도 동일 비밀번호로 검증된다.
    assert verify_password(_PLAINTEXT, user.password) is True


def test_login_with_argon2_user_does_not_write_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이미 argon2 사용자는 불필요한 commit 이 일어나지 않는다."""
    current = hash_password(_PLAINTEXT)  # argon2
    user = FakeUser(password_hash=current, user_id=7)
    db = FakeSession()

    monkeypatch.setattr(auth_service, "get_user_by_email", lambda _db, _email: user)

    result = auth_service.authenticate_user_service(db, "user@test.com", _PLAINTEXT)

    assert result is user
    assert user.password == current  # 변경 없음
    assert db.committed is False  # write-back 없음


def test_login_write_back_failure_still_logs_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC(best-effort): DB commit 이 실패해도 로그인은 성공한다.

    이미 유효한 자격증명으로 인증에 성공한 사용자를 승급 기록 실패로
    401/500 처리하면 안 된다. 롤백만 하고 사용자를 그대로 반환한다.
    """
    legacy = _make_legacy_bcrypt_hash()
    user = FakeUser(password_hash=legacy, user_id=99)
    db = FakeSession(fail_commit=True)

    monkeypatch.setattr(auth_service, "get_user_by_email", lambda _db, _email: user)

    # 예외가 전파되지 않아야 한다.
    result = auth_service.authenticate_user_service(db, "user@test.com", _PLAINTEXT)

    assert result is user  # 로그인 성공(예외 없음)
    assert db.rolled_back is True  # 실패 시 롤백
    assert db.committed is False


def test_login_wrong_password_raises_401_and_no_write_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SECURITY-001 무약화: 틀린 비밀번호는 승급 없이 여전히 401."""
    from app.exceptions import CustomException

    legacy = _make_legacy_bcrypt_hash()
    user = FakeUser(password_hash=legacy, user_id=5)
    db = FakeSession()

    monkeypatch.setattr(auth_service, "get_user_by_email", lambda _db, _email: user)

    with pytest.raises(CustomException) as exc:
        auth_service.authenticate_user_service(db, "user@test.com", "wrong-password")

    assert exc.value.status_code == 401
    # 승급/write-back 이 발생하지 않았다(해시 원본 유지, commit 없음).
    assert user.password == legacy
    assert db.committed is False


def test_login_unknown_user_raises_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """사용자가 없으면 해시 검증을 시도하지 않고 401(기존 동작 유지)."""
    from app.exceptions import CustomException

    db = FakeSession()
    monkeypatch.setattr(auth_service, "get_user_by_email", lambda _db, _email: None)

    with pytest.raises(CustomException) as exc:
        auth_service.authenticate_user_service(db, "ghost@test.com", _PLAINTEXT)

    assert exc.value.status_code == 401
    assert db.committed is False
