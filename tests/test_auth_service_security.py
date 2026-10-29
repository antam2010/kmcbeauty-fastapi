"""REQ-SEC-001 / REQ-SEC-002 보안 동작 테스트.

대상: app/services/auth_service.py
- 토큰 회전 시 user_id 포함 저장 (AC-001-1)
- 만료(ttl<=0) 토큰 재사용 금지 (AC-001-2)
- ExpiredSignatureError vs JWTError 구분 (AC-001-3)
- 로그아웃 후 재발급 차단 (AC-002-1)

Redis 는 인메모리 fake 로 대체하여 외부 의존 없이 검증한다.
"""

from datetime import timedelta

import pytest

from app.core.config import settings
from app.exceptions import CustomException
from app.services import auth_service


class FakeUser:
    """auth_service 가 참조하는 최소 User 인터페이스."""

    def __init__(self, user_id: int = 1, role: str = "MASTER") -> None:
        self.id = user_id
        self.role = role
        self.email = "user@test.com"

    def is_deleted(self) -> bool:
        return False


class FakeRequest:
    """FastAPI Request 의 headers/cookies 만 흉내낸다."""

    def __init__(self, refresh_token: str | None) -> None:
        self.headers = {}
        self.cookies = {"refresh_token": refresh_token} if refresh_token else {}


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> dict:
    """auth_service 가 사용하는 redis auth 헬퍼를 인메모리 store 로 대체."""
    store: dict[int, str] = {}
    ttls: dict[int, int] = {}

    def set_refresh(user_id: int, token: str) -> None:
        store[user_id] = token
        ttls.setdefault(user_id, settings.REFRESH_TOKEN_EXPIRE_SECONDS)

    def get_refresh(user_id: int) -> str | None:
        return store.get(user_id)

    def get_ttl(user_id: int) -> int:
        return ttls.get(user_id, -1)

    def clear_refresh(user_id: int) -> None:
        store.pop(user_id, None)
        ttls.pop(user_id, None)

    monkeypatch.setattr(auth_service, "set_refresh_token_redis", set_refresh)
    monkeypatch.setattr(auth_service, "get_refresh_token_redis", get_refresh)
    monkeypatch.setattr(auth_service, "get_refresh_token_ttl", get_ttl)
    monkeypatch.setattr(auth_service, "clear_refresh_token_redis", clear_refresh)
    monkeypatch.setattr(auth_service, "clear_user_redis", lambda _uid: None)

    return {"store": store, "ttls": ttls}


def _make_refresh_token(user_id: int = 1, expired: bool = False) -> str:
    from app.core.security import create_jwt_token

    delta = timedelta(seconds=-10) if expired else timedelta(days=1)
    return create_jwt_token({"sub": str(user_id), "type": "refresh"}, delta)


def test_rotation_stores_token_with_user_id(
    fake_redis: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-001-1: TTL 절반 이하일 때 회전하며 user.id 키로 저장, 저장==반환."""
    user = FakeUser(user_id=42)
    monkeypatch.setattr(auth_service, "get_user_by_id", lambda _db, _uid: user)

    token = _make_refresh_token(42)
    fake_redis["store"][42] = token
    # 절반 이하 TTL → 회전 트리거
    fake_redis["ttls"][42] = settings.REFRESH_TOKEN_EXPIRE_SECONDS // 4

    request = FakeRequest(token)
    access, new_refresh = auth_service.refresh_access_token(db=None, request=request)

    assert access  # 액세스 토큰 발급됨
    # user.id(=42) 키로 새 토큰이 저장되고, 저장값 == 반환값 (정합성)
    assert fake_redis["store"][42] == new_refresh
    assert new_refresh != token  # 실제 회전됨


def test_expired_ttl_token_not_reused(
    fake_redis: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-001-2: TTL==0(만료) 토큰은 재사용되지 않고 401."""
    user = FakeUser(user_id=1)
    monkeypatch.setattr(auth_service, "get_user_by_id", lambda _db, _uid: user)

    token = _make_refresh_token(1)
    fake_redis["store"][1] = token
    fake_redis["ttls"][1] = 0  # 만료

    request = FakeRequest(token)
    with pytest.raises(CustomException) as exc:
        auth_service.refresh_access_token(db=None, request=request)
    assert exc.value.status_code == 401


def test_expired_signature_distinct_from_invalid(
    fake_redis: dict,  # noqa: ARG001  # redis 패치 부작용을 위한 픽스처 주입
) -> None:
    """AC-001-3: ExpiredSignatureError 는 만료 코드로 구분되는 401."""
    expired_token = _make_refresh_token(1, expired=True)
    request = FakeRequest(expired_token)
    with pytest.raises(CustomException) as exc:
        auth_service.refresh_access_token(db=None, request=request)
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "AUTH_REFRESH_TOKEN_EXPIRED"


def test_invalid_signature_returns_invalid_code(
    fake_redis: dict,  # noqa: ARG001  # redis 패치 부작용을 위한 픽스처 주입
) -> None:
    """AC-001-3: 서명 위조/손상 토큰은 무효 코드로 401."""
    request = FakeRequest("this.is.not-a-valid-jwt")
    with pytest.raises(CustomException) as exc:
        auth_service.refresh_access_token(db=None, request=request)
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "AUTH_REFRESH_TOKEN_INVALID"


def test_logout_revokes_then_refresh_blocked(
    fake_redis: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-002-1: 로그아웃 후 동일 토큰으로 재발급 시도 시 401."""
    user = FakeUser(user_id=7)
    monkeypatch.setattr(auth_service, "get_user_by_id", lambda _db, _uid: user)

    token = _make_refresh_token(7)
    fake_redis["store"][7] = token
    fake_redis["ttls"][7] = settings.REFRESH_TOKEN_EXPIRE_SECONDS

    # 로그아웃 → 서버측 무효화(Redis 삭제)
    assert auth_service.logout_user(token) is True
    assert 7 not in fake_redis["store"]

    # 무효화된 토큰으로 재발급 시도 → 401
    request = FakeRequest(token)
    with pytest.raises(CustomException) as exc:
        auth_service.refresh_access_token(db=None, request=request)
    assert exc.value.status_code == 401


def test_logout_with_missing_token_returns_false(
    fake_redis: dict,  # noqa: ARG001  # redis 패치 부작용을 위한 픽스처 주입
) -> None:
    """엣지: 토큰이 없으면 예외 없이 False."""
    assert auth_service.logout_user(None) is False


def test_logout_with_expired_token_still_clears_redis(
    fake_redis: dict,
    monkeypatch: pytest.MonkeyPatch,  # noqa: ARG001  # 픽스처 조합 유지를 위한 주입
) -> None:
    """REQ-SEC-002: 만료된 리프레시 토큰으로 로그아웃해도 Redis 세션을 폐기한다.

    ExpiredSignatureError 가 sub 추출 전에 발생해 revoke 를 건너뛰면
    세션이 남아 재사용 위험이 있다. verify_exp=False 로 sub 를 복구하여
    반드시 clear_refresh_token_redis 가 호출되어야 한다.
    """
    user_id = 9
    expired_token = _make_refresh_token(user_id, expired=True)
    # 만료 이전에 저장되어 있던 세션을 시뮬레이션한다.
    fake_redis["store"][user_id] = expired_token
    fake_redis["ttls"][user_id] = settings.REFRESH_TOKEN_EXPIRE_SECONDS

    result = auth_service.logout_user(expired_token)

    assert result is True  # 만료 토큰이라도 폐기에 성공
    assert user_id not in fake_redis["store"]  # Redis 세션 실제 삭제됨


def test_logout_with_invalid_token_returns_false(
    fake_redis: dict,  # noqa: ARG001  # redis 패치 부작용을 위한 픽스처 주입
) -> None:
    """진짜 무효(서명 위조/손상) 토큰은 폐기 대상이 없으므로 False."""
    assert auth_service.logout_user("this.is.not-a-valid-jwt") is False


def test_valid_token_with_plenty_ttl_reused(
    fake_redis: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정상 회귀: TTL 이 절반 초과로 충분히 남으면 기존 토큰 유지, 새 액세스만 발급."""
    user = FakeUser(user_id=3)
    monkeypatch.setattr(auth_service, "get_user_by_id", lambda _db, _uid: user)

    token = _make_refresh_token(3)
    fake_redis["store"][3] = token
    fake_redis["ttls"][3] = settings.REFRESH_TOKEN_EXPIRE_SECONDS  # 풀 TTL

    request = FakeRequest(token)
    access, new_refresh = auth_service.refresh_access_token(db=None, request=request)
    assert access
    assert new_refresh == token  # 기존 토큰 유지
