"""인증 하드닝 테스트 — JWT type 검증 · 삭제 유저 즉시 차단 · Sentry PII 차단.

대상:
- app/dependencies/auth.py::get_current_user
  - refresh 토큰(type=refresh)·type 없는 토큰을 bearer 로 쓰면 401 (Medium)
  - Redis 미스 → DB 폴백에서 소프트삭제 유저는 401 (Medium)
  - Sentry set_user 에는 id 만 전달 (High, PII)
- app/services/user_service.py::delete_user_service
  - 커밋 성공 뒤에만 유저 캐시·리프레시 토큰 삭제, 커밋 실패 시 보존 (Medium)
- app/core/sentry.py::init_sentry — send_default_pii=False (High, PII)

DB/Redis/Sentry 없이 검증한다. Redis·Sentry 헬퍼는 monkeypatch 로 스텁하고, DB 폴백은
등식·IS NULL 필터를 평가하는 가짜 Query 로 "결과"를 관찰한다. 토큰은 실제 발급 함수
(generate_access_token / generate_refresh_token)로 만든다.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression, Null

from app.core import sentry as sentry_module
from app.core.security import create_jwt_token
from app.dependencies import auth as auth_dep
from app.exceptions import CustomException
from app.services import auth_service, user_service

USER_ID = 7
INVALID_AUTH_HINT = "유효하지 않은 인증 토큰입니다."
USER_NOT_FOUND_HINT = "사용자를 찾을 수 없습니다."


class FakeUser:
    """토큰 발급과 get_current_user 가 참조하는 최소 User 인터페이스."""

    def __init__(self, user_id: int = USER_ID) -> None:
        self.id = user_id
        self.role = "MASTER"
        self.email = "owner@test.com"
        self.name = "원장"
        self.deleted_at = None


def _matches(row: object, criterion: BinaryExpression) -> bool:
    """`컬럼 == 값` / `컬럼 IS NULL` 필터 하나를 row 에 적용한다."""
    value = getattr(row, criterion.left.key)
    if criterion.operator is operators.eq:
        return value == criterion.right.value
    if criterion.operator is operators.is_ and isinstance(criterion.right, Null):
        return value is None
    msg = f"지원하지 않는 필터 형태: {criterion}"
    raise AssertionError(msg)


class FakeQuery:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def filter(self, *criteria: BinaryExpression) -> "FakeQuery":
        kept = [r for r in self._rows if all(_matches(r, c) for c in criteria)]
        return FakeQuery(kept)

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class FakeSession:
    """users 테이블 행 목록을 가진 가짜 세션(DB 폴백 조회용)."""

    def __init__(self, rows: list) -> None:
        self.rows = rows

    def query(self, *_entities: object) -> FakeQuery:
        return FakeQuery(self.rows)


@pytest.fixture
def auth_spy(monkeypatch: pytest.MonkeyPatch) -> dict:
    """get_current_user 의 Redis 캐시 · Sentry 의존을 스텁한다(기본: 캐시 미스)."""
    spy: dict = {"cache": {}, "cached": [], "sentry_user": []}

    def fake_get_user_redis(user_id: int) -> dict | None:
        return spy["cache"].get(user_id)

    def fake_set_user_redis(user: FakeUser) -> None:
        spy["cached"].append(user.id)

    def fake_set_user(data: dict) -> None:
        spy["sentry_user"].append(data)

    monkeypatch.setattr(auth_dep, "get_user_redis", fake_get_user_redis)
    monkeypatch.setattr(auth_dep, "set_user_redis", fake_set_user_redis)
    monkeypatch.setattr(auth_dep, "set_user", fake_set_user)
    return spy


def _warm_cache(spy: dict, user: FakeUser) -> None:
    """set_user_redis 가 저장하는 형태와 같은 캐시 항목을 넣는다(캐시 히트 경로)."""
    spy["cache"][user.id] = {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
    }


# ---------------------------------------------------------------------------
# Item 4 — 액세스 토큰만 bearer 로 허용
# ---------------------------------------------------------------------------


def test_refresh_token_as_bearer_is_401(auth_spy: dict) -> None:
    """리프레시 토큰은 bearer 로 쓸 수 없다 — 유효한 유저 캐시가 있어도 401."""
    user = FakeUser()
    _warm_cache(auth_spy, user)
    refresh = auth_service.generate_refresh_token(user)

    with pytest.raises(CustomException) as exc:
        auth_dep.get_current_user(token=refresh, db=FakeSession([user]))

    assert exc.value.status_code == 401
    assert exc.value.detail["hint"] == INVALID_AUTH_HINT
    assert auth_spy["sentry_user"] == []


def test_token_without_type_claim_is_401(auth_spy: dict) -> None:
    """토큰 종류(type 클레임)가 없는 토큰도 액세스 토큰으로 인정하지 않는다."""
    user = FakeUser()
    _warm_cache(auth_spy, user)
    untyped = create_jwt_token({"sub": str(user.id)}, timedelta(minutes=5))

    with pytest.raises(CustomException) as exc:
        auth_dep.get_current_user(token=untyped, db=FakeSession([user]))

    assert exc.value.status_code == 401
    assert exc.value.detail["hint"] == INVALID_AUTH_HINT


def test_access_token_as_bearer_is_ok(auth_spy: dict) -> None:
    """정상 경로: 액세스 토큰(type=access)은 통과한다(캐시 히트)."""
    user = FakeUser()
    _warm_cache(auth_spy, user)

    current = auth_dep.get_current_user(
        token=auth_service.generate_access_token(user),
        db=FakeSession([]),
    )

    assert current.id == user.id


# ---------------------------------------------------------------------------
# Item 5 — 삭제 유저 즉시 차단
# ---------------------------------------------------------------------------


def test_soft_deleted_user_db_fallback_is_401(auth_spy: dict) -> None:
    """Redis 미스 → DB 폴백에서 소프트삭제 유저는 401, 캐시에 다시 올리지 않는다."""
    user = FakeUser()
    user.deleted_at = datetime.now(UTC)

    with pytest.raises(CustomException) as exc:
        auth_dep.get_current_user(
            token=auth_service.generate_access_token(user),
            db=FakeSession([user]),
        )

    assert exc.value.status_code == 401
    assert exc.value.detail["hint"] == USER_NOT_FOUND_HINT
    assert auth_spy["cached"] == []


def test_active_user_db_fallback_is_ok_and_cached(auth_spy: dict) -> None:
    """정상 경로: 활성 유저는 DB 폴백으로 인증되고 Redis 에 캐싱된다."""
    user = FakeUser()

    current = auth_dep.get_current_user(
        token=auth_service.generate_access_token(user),
        db=FakeSession([user]),
    )

    assert current is user
    assert auth_spy["cached"] == [user.id]


class EventSession:
    """commit/rollback 을 이벤트 순서로 기록하는 스파이 세션(커밋 실패 주입 가능)."""

    def __init__(self, events: list, commit_error: Exception | None = None) -> None:
        self.events = events
        self.commit_error = commit_error

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


@pytest.fixture
def delete_events(monkeypatch: pytest.MonkeyPatch) -> list:
    """delete_user_service 의 CRUD · Redis 의존을 이벤트 기록 스텁으로 대체한다."""
    events: list = []
    user = FakeUser()

    def fake_delete_user_db(**kwargs: object) -> None:
        events.append("soft_delete" if kwargs["is_soft_delete"] else "hard_delete")

    def fake_clear_refresh(user_id: int) -> None:
        events.append(f"clear_refresh:{user_id}")

    def fake_clear_user(user_id: int) -> None:
        events.append(f"clear_user:{user_id}")

    monkeypatch.setattr(user_service, "get_user_by_id", lambda _db, _uid: user)
    monkeypatch.setattr(user_service, "delete_user_db", fake_delete_user_db)
    monkeypatch.setattr(user_service, "clear_refresh_token_redis", fake_clear_refresh)
    monkeypatch.setattr(user_service, "clear_user_redis", fake_clear_user)
    return events


@pytest.mark.parametrize("is_soft_delete", [True, False], ids=["soft", "force"])
def test_delete_user_clears_sessions_only_after_commit(
    delete_events: list,
    is_soft_delete: bool,
) -> None:
    """유저 캐시 · 리프레시 토큰 삭제는 커밋 성공 뒤에만 일어난다(소프트/강제 공통)."""
    user_service.delete_user_service(
        db=EventSession(delete_events),
        current_user=FakeUser(),
        is_soft_delete=is_soft_delete,
    )

    commit_at = delete_events.index("commit")
    assert delete_events.index(f"clear_user:{USER_ID}") > commit_at
    assert delete_events.index(f"clear_refresh:{USER_ID}") > commit_at


def test_delete_user_commit_failure_keeps_sessions(delete_events: list) -> None:
    """커밋 실패 시 500 · 롤백, 유저 캐시와 리프레시 토큰은 그대로 둔다."""
    db = EventSession(delete_events, commit_error=SQLAlchemyError("commit failed"))

    with pytest.raises(CustomException) as exc:
        user_service.delete_user_service(
            db=db,
            current_user=FakeUser(),
            is_soft_delete=True,
        )

    assert exc.value.status_code == 500
    assert "rollback" in delete_events
    assert [e for e in delete_events if e.startswith("clear_")] == []


# ---------------------------------------------------------------------------
# Item 6 — Sentry PII 차단
# ---------------------------------------------------------------------------


def test_init_sentry_disables_default_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sentry 는 send_default_pii=False 로 초기화한다(쿠키 · 인증 헤더 · IP 미첨부)."""
    captured: dict = {}
    monkeypatch.setattr(
        sentry_module.sentry_sdk,
        "init",
        lambda **kwargs: captured.update(kwargs),
    )

    sentry_module.init_sentry(dsn="", environment="test")

    assert captured["send_default_pii"] is False


def test_sentry_set_user_receives_id_only(auth_spy: dict) -> None:
    """Sentry 사용자 컨텍스트에는 id 만 보낸다(email 등 PII 제외)."""
    user = FakeUser()
    _warm_cache(auth_spy, user)

    auth_dep.get_current_user(
        token=auth_service.generate_access_token(user),
        db=FakeSession([]),
    )

    assert auth_spy["sentry_user"] == [{"id": user.id}]
