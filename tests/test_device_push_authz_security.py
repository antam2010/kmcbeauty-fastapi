"""푸시 토큰 등록 IDOR · 무단 FCM 전송 차단 테스트 (Critical).

대상:
- app/services/device_push_token_service.py
  - register_device_token_service: 소유자는 항상 current_user, shop_id 는 멤버만
  - update/delete_device_token_service: 타인 토큰은 404 (존재 여부 은닉)
  - get_my_device_tokens_service: 본인 토큰만 조회 (감사 결과 고정)
  - send_fcm_notification_service: 같은 샵 멤버에게만 전송
- app/crud/device_push_token_crud.py::get_device_tokens_by_shop
  (현재 shop_user 멤버의 토큰만 수신 — 제외된 직원은 받지 않는다)
- app/crud/shop_user_crud.py::users_share_shop (공유 샵 판정)

DB 없이 검증한다. 서비스가 import 한 CRUD/FCM 함수는 monkeypatch 로 스텁하고,
소유권 필터는 단순 등식 필터를 평가하는 가짜 Query 로 "결과"를 관찰한다.
CRUD SQL 은 실제 SQLAlchemy Query 로 구성한 뒤 실행 대신 컴파일해 확인한다.
실제 FCM 전송 함수는 항상 스텁한다(네트워크 호출 없음).
"""

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import BinaryExpression

from app.crud import device_push_token_crud, shop_user_crud
from app.exceptions import CustomException
from app.schemas.device_push_token import (
    DevicePushTokenCreate,
    DevicePushTokenUpdate,
    FCMMessageRequest,
)
from app.services import device_push_token_service as service

REQUESTER_ID = 7
OTHER_USER_ID = 8
SHOP_ID = 10
FCM_A = "fcm-registration-a"
FCM_B = "fcm-registration-b"


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeShopUser:
    def __init__(self, shop_id: int, user_id: int) -> None:
        self.shop_id = shop_id
        self.user_id = user_id


class FakeToken:
    """DevicePushTokenResponse 직렬화(from_attributes)에 필요한 최소 속성."""

    def __init__(self, token_id: int, user_id: int, value: str = FCM_A) -> None:
        self.id = token_id
        self.user_id = user_id
        self.shop_id = None
        self.device_id = None
        self.token = value
        self.platform = "android"
        self.is_active = True


def _matches(row: object, criterion: BinaryExpression) -> bool:
    """`컬럼 == 값` 형태의 필터 하나를 row 에 적용한다(그 외 형태는 테스트 실패)."""
    if criterion.operator is not operators.eq:
        msg = f"지원하지 않는 필터 형태: {criterion}"
        raise AssertionError(msg)
    return getattr(row, criterion.left.key) == criterion.right.value


class FakeQuery:
    """등식 필터만 평가하는 가짜 Query.

    소유권 필터가 실제로 걸렸는지를 결과로 관찰한다: 타인 토큰을 id 로만 찾으면
    행이 보이고, id + user_id 로 찾으면 보이지 않는다.
    """

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def filter(self, *criteria: BinaryExpression) -> "FakeQuery":
        kept = [r for r in self._rows if all(_matches(r, c) for c in criteria)]
        return FakeQuery(kept)

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class SpySession:
    """query 는 FakeQuery 로, commit/rollback/delete 는 기록만 하는 스파이 세션."""

    def __init__(self, rows: list | None = None) -> None:
        self.rows = rows or []
        self.commit_count = 0
        self.rollback_count = 0
        self.deleted: list = []

    def query(self, *_entities: object) -> FakeQuery:
        return FakeQuery(self.rows)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def flush(self) -> None:
        pass

    def refresh(self, _obj: object) -> None:
        pass

    def delete(self, obj: object) -> None:
        self.deleted.append(obj)


class CapturingSession:
    """실제 SQLAlchemy Query 로 체인을 만들고, 실행 대신 최종 SQL 을 기록한다."""

    def __init__(self, first_result: object = None) -> None:
        self.sql: list[str] = []
        self.first_result = first_result

    def query(self, *entities: object) -> "CapturingQuery":
        return CapturingQuery(Query(list(entities)), self)


class CapturingQuery:
    def __init__(self, query: Query, session: CapturingSession) -> None:
        self._query = query
        self._session = session

    def join(self, *args: object) -> "CapturingQuery":
        return CapturingQuery(self._query.join(*args), self._session)

    def filter(self, *criteria: object) -> "CapturingQuery":
        return CapturingQuery(self._query.filter(*criteria), self._session)

    def order_by(self, *args: object) -> "CapturingQuery":
        return CapturingQuery(self._query.order_by(*args), self._session)

    def limit(self, value: int) -> "CapturingQuery":
        return CapturingQuery(self._query.limit(value), self._session)

    def _record(self) -> None:
        compiled = self._query.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
        self._session.sql.append(" ".join(str(compiled).split()))

    def all(self) -> list:
        self._record()
        return []

    def first(self) -> object:
        self._record()
        return self._session.first_result


def _has_eq(sql: str, left: str, right: str) -> bool:
    """좌우 순서와 무관하게 SQL 에 `left = right` 등식이 있는지 확인한다."""
    return f"{left} = {right}" in sql or f"{right} = {left}" in sql


# ---------------------------------------------------------------------------
# 공통 스텁
# ---------------------------------------------------------------------------


@pytest.fixture
def fcm_spy(monkeypatch: pytest.MonkeyPatch) -> list:
    """실제 FCM 전송을 막고, 전송 시도된 토큰 목록을 기록한다."""
    sent: list = []

    def fake_send_one(**kwargs: object) -> dict:
        sent.append([kwargs["token"]])
        return {"success": True, "message_id": "projects/test/messages/1"}

    def fake_send_many(**kwargs: object) -> dict:
        sent.append(list(kwargs["tokens"]))
        return {"success": True, "success_count": 2, "failure_count": 0}

    monkeypatch.setattr(service, "send_fcm_message", fake_send_one)
    monkeypatch.setattr(service, "send_fcm_multicast", fake_send_many)
    return sent


def _stub_membership(
    monkeypatch: pytest.MonkeyPatch,
    members: set[tuple[int, int]],
) -> list:
    """get_shop_user 를 (shop_id, user_id) 멤버 집합 기반 스텁으로 대체한다."""
    calls: list = []

    def fake_get_shop_user(_db: object, shop_id: int, user_id: int) -> object:
        calls.append((shop_id, user_id))
        if (shop_id, user_id) in members:
            return FakeShopUser(shop_id, user_id)
        return None

    monkeypatch.setattr(service, "get_shop_user", fake_get_shop_user)
    return calls


def _stub_share(monkeypatch: pytest.MonkeyPatch, *, shared: bool) -> list:
    """users_share_shop 를 고정 결과 스텁으로 대체하고 호출 인자를 기록한다."""
    calls: list = []

    def fake_users_share_shop(_db: object, user_id: int, other_user_id: int) -> bool:
        calls.append((user_id, other_user_id))
        return shared

    monkeypatch.setattr(service, "users_share_shop", fake_users_share_shop)
    return calls


def _stub_tokens(monkeypatch: pytest.MonkeyPatch, tokens: list) -> dict:
    """유저/샵 토큰 조회를 고정 결과로 대체하고 호출 kwargs 를 기록한다."""
    calls: dict = {"by_user": [], "by_shop": []}

    def fake_by_user(**kwargs: object) -> list:
        calls["by_user"].append(kwargs)
        return tokens

    def fake_by_shop(**kwargs: object) -> list:
        calls["by_shop"].append(kwargs)
        return tokens

    monkeypatch.setattr(service, "get_device_tokens_by_user", fake_by_user)
    monkeypatch.setattr(service, "get_device_tokens_by_shop", fake_by_shop)
    return calls


def _stub_get_or_create(monkeypatch: pytest.MonkeyPatch) -> list:
    captured: list = []

    def fake_get_or_create(**kwargs: object) -> FakeToken:
        captured.append(kwargs)
        return FakeToken(token_id=1, user_id=kwargs["user_id"])

    monkeypatch.setattr(service, "get_or_create_device_token", fake_get_or_create)
    return captured


# ---------------------------------------------------------------------------
# 1. 등록 — 소유자 강제 · shop_id 멤버십
# ---------------------------------------------------------------------------


def test_create_schema_ignores_body_user_id() -> None:
    """요청 스키마는 user_id 를 받지 않는다(거부가 아니라 무시 → 하위 호환)."""
    payload = DevicePushTokenCreate(
        user_id=OTHER_USER_ID,
        token=FCM_A,
        platform="android",
    )
    assert "user_id" not in DevicePushTokenCreate.model_fields
    assert "user_id" not in payload.model_dump()


def test_register_ignores_body_user_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """본문에 타인 user_id 를 넣어도 토큰 소유자는 요청자(current_user)다."""
    captured = _stub_get_or_create(monkeypatch)
    payload = DevicePushTokenCreate(
        user_id=OTHER_USER_ID,
        token=FCM_A,
        platform="android",
    )

    result = service.register_device_token_service(
        db=SpySession(),
        token_data=payload,
        current_user=FakeUser(REQUESTER_ID),
    )

    assert captured[0]["user_id"] == REQUESTER_ID
    assert result.user_id == REQUESTER_ID


def test_register_with_non_member_shop_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """멤버가 아닌 샵 shop_id 로 등록하면 403 이고 아무것도 쓰지 않는다."""
    captured = _stub_get_or_create(monkeypatch)
    _stub_membership(monkeypatch, members=set())
    db = SpySession()
    payload = DevicePushTokenCreate(token=FCM_A, platform="android", shop_id=SHOP_ID)

    with pytest.raises(CustomException) as exc:
        service.register_device_token_service(
            db=db,
            token_data=payload,
            current_user=FakeUser(REQUESTER_ID),
        )

    assert exc.value.status_code == 403
    assert captured == []
    assert db.commit_count == 0


def test_register_with_member_shop_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """요청자가 멤버인 샵이면 등록되고, 멤버십은 (shop_id, 요청자)로 확인한다."""
    captured = _stub_get_or_create(monkeypatch)
    calls = _stub_membership(monkeypatch, members={(SHOP_ID, REQUESTER_ID)})
    payload = DevicePushTokenCreate(token=FCM_A, platform="android", shop_id=SHOP_ID)

    service.register_device_token_service(
        db=SpySession(),
        token_data=payload,
        current_user=FakeUser(REQUESTER_ID),
    )

    assert calls == [(SHOP_ID, REQUESTER_ID)]
    assert captured[0]["shop_id"] == SHOP_ID
    assert captured[0]["user_id"] == REQUESTER_ID


# ---------------------------------------------------------------------------
# 2. PATCH / DELETE / GET me — 소유권(타인 토큰은 404)
# ---------------------------------------------------------------------------


def test_patch_other_users_token_is_404() -> None:
    """타인 소유 토큰 수정은 존재를 숨기고 404, 값은 바뀌지 않는다."""
    token = FakeToken(token_id=5, user_id=OTHER_USER_ID)
    db = SpySession(rows=[token])

    with pytest.raises(CustomException) as exc:
        service.update_device_token_service(
            db=db,
            token_id=5,
            token_update=DevicePushTokenUpdate(platform="ios"),
            current_user=FakeUser(REQUESTER_ID),
        )

    assert exc.value.status_code == 404
    assert token.platform == "android"
    assert db.commit_count == 0


def test_delete_other_users_token_is_404() -> None:
    """타인 소유 토큰 삭제는 존재를 숨기고 404, 삭제되지 않는다."""
    db = SpySession(rows=[FakeToken(token_id=5, user_id=OTHER_USER_ID)])

    with pytest.raises(CustomException) as exc:
        service.delete_device_token_service(
            db=db,
            token_id=5,
            current_user=FakeUser(REQUESTER_ID),
        )

    assert exc.value.status_code == 404
    assert db.deleted == []
    assert db.commit_count == 0


def test_patch_and_delete_own_token_succeed() -> None:
    """정상 경로: 본인 토큰은 수정·삭제된다."""
    token = FakeToken(token_id=5, user_id=REQUESTER_ID)
    db = SpySession(rows=[token])

    updated = service.update_device_token_service(
        db=db,
        token_id=5,
        token_update=DevicePushTokenUpdate(platform="ios"),
        current_user=FakeUser(REQUESTER_ID),
    )
    service.delete_device_token_service(
        db=db,
        token_id=5,
        current_user=FakeUser(REQUESTER_ID),
    )

    assert updated.platform == "ios"
    assert db.deleted == [token]


def test_get_my_tokens_is_scoped_to_current_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """감사: GET /device-tokens/me 는 요청자 user_id 로만 조회한다."""
    calls = _stub_tokens(monkeypatch, [FakeToken(token_id=1, user_id=REQUESTER_ID)])

    result = service.get_my_device_tokens_service(
        db=SpySession(),
        current_user=FakeUser(REQUESTER_ID),
    )

    assert [c["user_id"] for c in calls["by_user"]] == [REQUESTER_ID]
    assert [t.user_id for t in result] == [REQUESTER_ID]


# ---------------------------------------------------------------------------
# 3. 전송 — 같은 샵 멤버에게만
# ---------------------------------------------------------------------------


def _send(user_id: int | None = None, shop_id: int | None = None) -> object:
    return service.send_fcm_notification_service(
        db=SpySession(),
        fcm_request=FCMMessageRequest(
            user_id=user_id,
            shop_id=shop_id,
            title="예약 알림",
            body="10시 예약이 있습니다.",
        ),
        current_user=FakeUser(REQUESTER_ID),
    )


def test_send_to_non_member_shop_is_403(
    monkeypatch: pytest.MonkeyPatch,
    fcm_spy: list,
) -> None:
    """요청자가 멤버가 아닌 샵으로는 전송할 수 없다(403, FCM 호출 없음)."""
    _stub_membership(monkeypatch, members={(SHOP_ID, OTHER_USER_ID)})
    _stub_tokens(monkeypatch, [FakeToken(1, OTHER_USER_ID)])

    with pytest.raises(CustomException) as exc:
        _send(shop_id=SHOP_ID)

    assert exc.value.status_code == 403
    assert fcm_spy == []


def test_send_to_unrelated_user_is_403(
    monkeypatch: pytest.MonkeyPatch,
    fcm_spy: list,
) -> None:
    """샵을 공유하지 않는 유저에게는 전송할 수 없다(403, FCM 호출 없음)."""
    _stub_share(monkeypatch, shared=False)
    _stub_tokens(monkeypatch, [FakeToken(1, OTHER_USER_ID)])

    with pytest.raises(CustomException) as exc:
        _send(user_id=OTHER_USER_ID)

    assert exc.value.status_code == 403
    assert fcm_spy == []


def test_send_to_same_shop_member_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
    fcm_spy: list,
) -> None:
    """같은 샵 멤버에게는 전송된다(공유 판정은 요청자·대상 순서로 호출)."""
    share_calls = _stub_share(monkeypatch, shared=True)
    _stub_tokens(monkeypatch, [FakeToken(1, OTHER_USER_ID, FCM_B)])

    result = _send(user_id=OTHER_USER_ID)

    assert share_calls == [(REQUESTER_ID, OTHER_USER_ID)]
    assert fcm_spy == [[FCM_B]]
    assert result.success is True


def test_send_to_self_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
    fcm_spy: list,
) -> None:
    """본인에게 보내는 것은 샵 공유 여부와 무관하게 허용된다."""
    _stub_share(monkeypatch, shared=False)
    _stub_tokens(monkeypatch, [FakeToken(1, REQUESTER_ID, FCM_A)])

    _send(user_id=REQUESTER_ID)

    assert fcm_spy == [[FCM_A]]


def test_send_to_member_shop_uses_shop_recipients(
    monkeypatch: pytest.MonkeyPatch,
    fcm_spy: list,
) -> None:
    """멤버인 샵으로의 전송은 샵 수신자 조회(멤버십 필터) 결과로 멀티캐스트한다."""
    member_calls = _stub_membership(monkeypatch, members={(SHOP_ID, REQUESTER_ID)})
    tokens = [FakeToken(1, REQUESTER_ID, FCM_A), FakeToken(2, OTHER_USER_ID, FCM_B)]
    token_calls = _stub_tokens(monkeypatch, tokens)

    result = _send(shop_id=SHOP_ID)

    assert member_calls == [(SHOP_ID, REQUESTER_ID)]
    assert [c["shop_id"] for c in token_calls["by_shop"]] == [SHOP_ID]
    assert fcm_spy == [[FCM_A, FCM_B]]
    assert result.success_count == 2


def test_send_without_target_is_400(fcm_spy: list) -> None:
    """회귀: user_id/shop_id 둘 다 없으면 400."""
    with pytest.raises(CustomException) as exc:
        _send()
    assert exc.value.status_code == 400
    assert fcm_spy == []


def test_send_with_no_active_tokens_is_404(
    monkeypatch: pytest.MonkeyPatch,
    fcm_spy: list,
) -> None:
    """회귀: 권한은 있으나 활성 토큰이 없으면 404."""
    _stub_membership(monkeypatch, members={(SHOP_ID, REQUESTER_ID)})
    _stub_tokens(monkeypatch, [])

    with pytest.raises(CustomException) as exc:
        _send(shop_id=SHOP_ID)

    assert exc.value.status_code == 404
    assert fcm_spy == []


# ---------------------------------------------------------------------------
# 4. CRUD SQL — 샵 수신자는 현재 멤버로 한정, 공유 샵 판정
# ---------------------------------------------------------------------------


def test_shop_send_excludes_non_member_tokens() -> None:
    """샵 수신자 조회는 shop_user 와 (shop_id, user_id) 로 조인해 현재 멤버만 남긴다.

    토큰에 shop_id 가 남아 있어도 해당 유저의 shop_user 매핑이 없으면(제외된 직원)
    조인에서 탈락하여 수신하지 않는다.
    """
    db = CapturingSession()
    device_push_token_crud.get_device_tokens_by_shop(db, shop_id=SHOP_ID)

    (sql,) = db.sql
    assert "JOIN shop_user ON" in sql
    assert _has_eq(sql, "shop_user.shop_id", "device_push_tokens.shop_id")
    assert _has_eq(sql, "shop_user.user_id", "device_push_tokens.user_id")
    assert "device_push_tokens.shop_id = 10" in sql
    assert "device_push_tokens.is_active = true" in sql


def test_users_share_shop_self_joins_memberships() -> None:
    """공유 샵 판정: shop_user 를 shop_id 로 자기조인해 두 유저의 매핑을 찾는다."""
    miss = CapturingSession(first_result=None)
    hit = CapturingSession(first_result=(1,))

    assert shop_user_crud.users_share_shop(miss, REQUESTER_ID, OTHER_USER_ID) is False
    assert shop_user_crud.users_share_shop(hit, REQUESTER_ID, OTHER_USER_ID) is True

    sql = miss.sql[0]
    assert "FROM shop_user JOIN shop_user AS peer ON" in sql
    assert _has_eq(sql, "peer.shop_id", "shop_user.shop_id")
    assert "shop_user.user_id = 7" in sql
    assert "peer.user_id = 8" in sql
