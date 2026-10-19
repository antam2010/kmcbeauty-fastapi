"""SPEC-API-001 REQ-API-001 — 누락 엔드포인트 계약 정합 테스트(백엔드).

대상:
- app/services/treatment_service.py::cancel_treatment_service
  (DELETE /treatments/{id} 백엔드 진입점 — 상태 CANCELLED 전환 방식)
- app/services/shop_user_service.py::{create,update,delete}_shop_user_service
  (POST/PUT/DELETE /shops/{shop_id}/users[/{user_id}] 진입점)

DB 없이 CRUD 함수를 monkeypatch 로 스텁하고, commit/rollback 호출을 기록하는 스파이
세션과 가짜 row 로 서비스 로직을 관찰한다(기존 fake/spy 패턴 재사용).

[HARD] 보존 검증:
- Treatment 는 SoftDeleteMixin 이 없으므로(FIX-001) deleted_at 을 쓰지 않고 status 를
  CANCELLED 로 전환함을 확인한다.
- shop-user 쓰기는 대표원장(is_primary_owner) 게이트를 통과해야 한다(SECURITY-001 관례).
"""

from datetime import datetime

import pytest

from app.enum.treatment_status import PaymentMethod, TreatmentStatus
from app.exceptions import CustomException
from app.services import shop_user_service, treatment_service


class SpySession:
    """commit/rollback/refresh/delete 호출을 기록하는 스파이 세션."""

    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0
        self.refresh_count = 0
        self.deleted: list = []

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def refresh(self, _obj: object) -> None:
        self.refresh_count += 1

    def delete(self, obj: object) -> None:
        self.deleted.append(obj)


class FakeShop:
    def __init__(self, shop_id: int) -> None:
        self.id = shop_id


class FakeUser:
    def __init__(self, user_id: int, email: str = "u@test.com") -> None:
        self.id = user_id
        self.email = email


class FakeTreatment:
    def __init__(self, shop_id: int, status_: TreatmentStatus) -> None:
        self.shop_id = shop_id
        self.status = status_


class FakeShopUser:
    def __init__(
        self,
        shop_id: int,
        user_id: int,
        is_primary_owner: int,
    ) -> None:
        self.shop_id = shop_id
        self.user_id = user_id
        self.is_primary_owner = is_primary_owner


# ---------------------------------------------------------------------------
# REQ-API-001.1 — DELETE /treatments/{id} (cancel_treatment_service)
# ---------------------------------------------------------------------------


def test_cancel_treatment_transitions_status_to_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-001-1: 소유 예약 삭제 시 status 를 CANCELLED 로 전환하고 commit 한다.

    Treatment 는 deleted_at 이 없으므로 소프트삭제 대신 상태 전환을 검증한다.
    """
    treatment = FakeTreatment(shop_id=10, status_=TreatmentStatus.RESERVED)
    monkeypatch.setattr(
        treatment_service,
        "get_treatment_by_id",
        lambda _db, _tid: treatment,
    )

    db = SpySession()
    result = treatment_service.cancel_treatment_service(
        db=db,
        current_shop=FakeShop(10),
        treatment_id=1,
    )

    assert result is None  # 204 (본문 없음)
    assert treatment.status == TreatmentStatus.CANCELLED
    assert db.commit_count == 1
    assert db.rollback_count == 0
    # deleted_at 계열 필드를 만들지 않았음을 확인(모델 오염 방지).
    assert not hasattr(treatment, "deleted_at")


def test_cancel_treatment_other_shop_is_rejected_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-001-1: 다른 상점의 예약 삭제 시도는 404(BOLA 방지)."""
    # 조회는 되지만 shop_id 가 현재 상점과 다르다.
    treatment = FakeTreatment(shop_id=999, status_=TreatmentStatus.RESERVED)
    monkeypatch.setattr(
        treatment_service,
        "get_treatment_by_id",
        lambda _db, _tid: treatment,
    )

    db = SpySession()
    with pytest.raises(CustomException) as exc:
        treatment_service.cancel_treatment_service(
            db=db,
            current_shop=FakeShop(10),
            treatment_id=1,
        )

    assert exc.value.status_code == 404
    assert treatment.status == TreatmentStatus.RESERVED  # 미변경
    assert db.commit_count == 0


def test_cancel_treatment_missing_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """엣지: 존재하지 않는 treatment_id 삭제 시 404(무음 성공 금지)."""
    monkeypatch.setattr(
        treatment_service,
        "get_treatment_by_id",
        lambda _db, _tid: None,
    )

    db = SpySession()
    with pytest.raises(CustomException) as exc:
        treatment_service.cancel_treatment_service(
            db=db,
            current_shop=FakeShop(10),
            treatment_id=12345,
        )

    assert exc.value.status_code == 404
    assert db.commit_count == 0


def test_cancel_treatment_already_cancelled_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """엣지: 이미 CANCELLED 인 예약 재삭제는 멱등하게 204(재-commit 없음)."""
    treatment = FakeTreatment(shop_id=10, status_=TreatmentStatus.CANCELLED)
    monkeypatch.setattr(
        treatment_service,
        "get_treatment_by_id",
        lambda _db, _tid: treatment,
    )

    db = SpySession()
    result = treatment_service.cancel_treatment_service(
        db=db,
        current_shop=FakeShop(10),
        treatment_id=1,
    )

    assert result is None
    assert treatment.status == TreatmentStatus.CANCELLED
    assert db.commit_count == 0  # 상태 불변 → 추가 commit 불필요


def test_treatment_model_has_no_deleted_at_regression() -> None:
    """FIX-001 보존: Treatment 는 여전히 deleted_at 컬럼이 없어야 한다.

    API-001 의 삭제 구현이 스키마에 deleted_at 을 추가하지 않았음을 회귀 방지로 확인.
    """
    from app.models.treatment import Treatment

    assert "deleted_at" not in Treatment.__table__.columns


def test_payment_method_enum_unchanged() -> None:
    """REQ-API-002.4 정본 확인: 백엔드 PaymentMethod 값 집합이 CARD/CASH/UNPAID 유지."""
    assert {m.value for m in PaymentMethod} == {"CARD", "CASH", "UNPAID"}


def test_treatment_status_enum_unchanged() -> None:
    """REQ-API-004.3 정본 확인: TreatmentStatus 5개 값 집합 유지(IN_PROGRESS 부재)."""
    assert {s.value for s in TreatmentStatus} == {
        "RESERVED",
        "VISITED",
        "CANCELLED",
        "NO_SHOW",
        "COMPLETED",
    }


# ---------------------------------------------------------------------------
# REQ-API-001.2 — POST/PUT/DELETE /shops/{shop_id}/users (shop_user_service)
# ---------------------------------------------------------------------------


def _patch_get_shop_user(
    monkeypatch: pytest.MonkeyPatch,
    requester: object,
) -> None:
    """요청자 권한 조회(get_shop_user)를 requester 로 스텁."""
    monkeypatch.setattr(
        shop_user_service,
        "get_shop_user",
        lambda _db, _sid, _uid: requester,
    )


def test_create_shop_user_requires_primary_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-001-2: 대표원장이 아니면 연결 생성이 403(SECURITY-001 관례 재사용)."""
    # 요청자는 멤버지만 대표원장 아님.
    _patch_get_shop_user(
        monkeypatch,
        FakeShopUser(shop_id=1, user_id=5, is_primary_owner=0),
    )

    db = SpySession()
    with pytest.raises(CustomException) as exc:
        shop_user_service.create_shop_user_service(
            db=db,
            shop_id=1,
            current_user=FakeUser(5),
            payload=_associate_payload("new@test.com", 0),
        )

    assert exc.value.status_code == 403
    assert db.commit_count == 0


def test_create_shop_user_non_member_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """비멤버(요청자 매핑 없음)는 403."""
    _patch_get_shop_user(monkeypatch, None)

    db = SpySession()
    with pytest.raises(CustomException) as exc:
        shop_user_service.create_shop_user_service(
            db=db,
            shop_id=1,
            current_user=FakeUser(5),
            payload=_associate_payload("new@test.com", 0),
        )

    assert exc.value.status_code == 403


def test_create_shop_user_target_email_not_found_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대상 유저 email 미존재 시 404."""
    _patch_get_shop_user(
        monkeypatch,
        FakeShopUser(shop_id=1, user_id=5, is_primary_owner=1),
    )
    monkeypatch.setattr(
        shop_user_service,
        "get_user_by_email",
        lambda _db, _email: None,
    )

    db = SpySession()
    with pytest.raises(CustomException) as exc:
        shop_user_service.create_shop_user_service(
            db=db,
            shop_id=1,
            current_user=FakeUser(5),
            payload=_associate_payload("missing@test.com", 0),
        )

    assert exc.value.status_code == 404
    assert db.commit_count == 0


def test_create_shop_user_duplicate_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """이미 연결된 유저 재연결 시 409."""
    owner = FakeShopUser(shop_id=1, user_id=5, is_primary_owner=1)
    target = FakeUser(7, email="dup@test.com")
    existing = FakeShopUser(shop_id=1, user_id=7, is_primary_owner=0)

    def fake_get_shop_user(_db: object, _sid: object, uid: int) -> object:
        # 첫 호출은 요청자(uid=5)=owner, 두번째는 대상 중복확인(uid=7)=existing.
        return owner if uid == 5 else existing

    monkeypatch.setattr(shop_user_service, "get_shop_user", fake_get_shop_user)
    monkeypatch.setattr(
        shop_user_service,
        "get_user_by_email",
        lambda _db, _email: target,
    )

    db = SpySession()
    with pytest.raises(CustomException) as exc:
        shop_user_service.create_shop_user_service(
            db=db,
            shop_id=1,
            current_user=FakeUser(5),
            payload=_associate_payload("dup@test.com", 0),
        )

    assert exc.value.status_code == 409
    assert db.commit_count == 0


def test_create_shop_user_success_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 흐름: 대표원장이 신규 유저를 연결하면 create+commit 후 응답 반환."""
    owner = FakeShopUser(shop_id=1, user_id=5, is_primary_owner=1)
    target = FakeUser(7, email="new@test.com")

    def fake_get_shop_user(_db: object, _sid: object, uid: int) -> object:
        # 요청자(uid=5)는 owner, 대상 중복확인(uid=7)은 없음.
        return owner if uid == 5 else None

    monkeypatch.setattr(shop_user_service, "get_shop_user", fake_get_shop_user)
    monkeypatch.setattr(
        shop_user_service,
        "get_user_by_email",
        lambda _db, _email: target,
    )
    created_holder = {}

    def fake_create_shop_user(_db: object, shop_user: object) -> object:
        created_holder["obj"] = shop_user
        return shop_user

    monkeypatch.setattr(
        shop_user_service,
        "create_shop_user",
        fake_create_shop_user,
    )
    # 응답 직렬화용 재조회 → user 관계 포함 매핑 반환.
    monkeypatch.setattr(
        shop_user_service,
        "get_shop_user_with_user",
        lambda _db, _sid, _uid: _shop_user_row_with_user(1, target),
    )

    db = SpySession()
    resp = shop_user_service.create_shop_user_service(
        db=db,
        shop_id=1,
        current_user=FakeUser(5),
        payload=_associate_payload("new@test.com", 1),
    )

    assert db.commit_count == 1
    assert db.rollback_count == 0
    # 연결 생성 시 대상 user_id 로 매핑되고 role 등 권한 필드는 관여하지 않음.
    assert created_holder["obj"].user_id == 7
    assert created_holder["obj"].is_primary_owner == 1
    assert resp.user_id == 7


def test_update_shop_user_success_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 흐름: 대표원장이 연결(대표원장 여부)을 수정하면 commit."""
    owner = FakeShopUser(shop_id=1, user_id=5, is_primary_owner=1)
    _patch_get_shop_user(monkeypatch, owner)

    target = FakeUser(7, email="member@test.com")
    row = _shop_user_row_with_user(1, target, is_primary_owner=0)
    monkeypatch.setattr(
        shop_user_service,
        "get_shop_user_with_user",
        lambda _db, _sid, _uid: row,
    )

    db = SpySession()
    resp = shop_user_service.update_shop_user_service(
        db=db,
        shop_id=1,
        user_id=7,
        current_user=FakeUser(5),
        payload=_associate_update_payload(1),
    )

    assert db.commit_count == 1
    assert row.is_primary_owner == 1  # 갱신 반영
    assert resp.user_id == 7


def test_update_shop_user_missing_mapping_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """연결이 없는 user_id 수정 시 404."""
    owner = FakeShopUser(shop_id=1, user_id=5, is_primary_owner=1)
    _patch_get_shop_user(monkeypatch, owner)
    monkeypatch.setattr(
        shop_user_service,
        "get_shop_user_with_user",
        lambda _db, _sid, _uid: None,
    )

    db = SpySession()
    with pytest.raises(CustomException) as exc:
        shop_user_service.update_shop_user_service(
            db=db,
            shop_id=1,
            user_id=999,
            current_user=FakeUser(5),
            payload=_associate_update_payload(1),
        )

    assert exc.value.status_code == 404
    assert db.commit_count == 0


def test_delete_shop_user_success_removes_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """정상 흐름: 대표원장이 연결을 해제하면 매핑을 delete 하고 commit."""
    owner = FakeShopUser(shop_id=1, user_id=5, is_primary_owner=1)
    mapping = FakeShopUser(shop_id=1, user_id=7, is_primary_owner=0)

    def fake_get_shop_user(_db: object, _sid: object, uid: int) -> object:
        return owner if uid == 5 else mapping

    monkeypatch.setattr(shop_user_service, "get_shop_user", fake_get_shop_user)

    deleted_holder = {}

    def fake_delete_shop_user(_db: object, obj: object) -> None:
        deleted_holder["obj"] = obj

    monkeypatch.setattr(
        shop_user_service,
        "delete_shop_user",
        fake_delete_shop_user,
    )

    db = SpySession()
    result = shop_user_service.delete_shop_user_service(
        db=db,
        shop_id=1,
        user_id=7,
        current_user=FakeUser(5),
    )

    assert result is None  # 204
    assert db.commit_count == 1
    assert deleted_holder["obj"] is mapping


def test_delete_shop_user_requires_primary_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대표원장이 아니면 연결 해제 403."""
    _patch_get_shop_user(
        monkeypatch,
        FakeShopUser(shop_id=1, user_id=5, is_primary_owner=0),
    )

    db = SpySession()
    with pytest.raises(CustomException) as exc:
        shop_user_service.delete_shop_user_service(
            db=db,
            shop_id=1,
            user_id=7,
            current_user=FakeUser(5),
        )

    assert exc.value.status_code == 403
    assert db.commit_count == 0


def test_delete_shop_user_missing_mapping_is_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """연결이 없는 user_id 해제 시 404."""
    owner = FakeShopUser(shop_id=1, user_id=5, is_primary_owner=1)

    def fake_get_shop_user(_db: object, _sid: object, uid: int) -> object:
        return owner if uid == 5 else None

    monkeypatch.setattr(shop_user_service, "get_shop_user", fake_get_shop_user)

    db = SpySession()
    with pytest.raises(CustomException) as exc:
        shop_user_service.delete_shop_user_service(
            db=db,
            shop_id=1,
            user_id=999,
            current_user=FakeUser(5),
        )

    assert exc.value.status_code == 404
    assert db.commit_count == 0


# ---------------------------------------------------------------------------
# 요청 스키마: role 등 권한 상승 필드가 계약에 포함되지 않음(SECURITY-001)
# ---------------------------------------------------------------------------


def test_associate_request_schema_has_no_role_field() -> None:
    """SECURITY-001 보존: 샵 유저 연결 요청 스키마에 role/password 필드가 없다."""
    from app.schemas.shop_user import (
        ShopUserAssociateRequest,
        ShopUserAssociateUpdateRequest,
    )

    create_fields = set(ShopUserAssociateRequest.model_fields.keys())
    update_fields = set(ShopUserAssociateUpdateRequest.model_fields.keys())

    assert "role" not in create_fields
    assert "password" not in create_fields
    assert "role" not in update_fields
    # 연결 요청은 email + 대표원장 여부만 받는다.
    assert create_fields == {"email", "is_primary_owner"}
    assert update_fields == {"is_primary_owner"}


# ---------------------------------------------------------------------------
# 헬퍼: Pydantic 요청 모델/응답 row 생성
# ---------------------------------------------------------------------------


def _associate_payload(email: str, is_primary_owner: int) -> object:
    from app.schemas.shop_user import ShopUserAssociateRequest

    return ShopUserAssociateRequest(email=email, is_primary_owner=is_primary_owner)


def _associate_update_payload(is_primary_owner: int) -> object:
    from app.schemas.shop_user import ShopUserAssociateUpdateRequest

    return ShopUserAssociateUpdateRequest(is_primary_owner=is_primary_owner)


class _UserRow:
    """ShopUserUserResponse.model_validate 를 위한 user 관계 stand-in."""

    def __init__(self, user: FakeUser) -> None:
        from app.enum.role import UserRole

        self.id = user.id
        self.name = "member-name"
        self.email = user.email
        self.role = UserRole.MASTER
        self.role_name = None
        self.created_at = _fixed_dt()
        self.updated_at = _fixed_dt()


class _ShopUserRow:
    """ShopUserUserResponse(from_attributes) 직렬화를 위한 매핑 stand-in."""

    def __init__(
        self,
        shop_id: int,
        user: FakeUser,
        is_primary_owner: int,
    ) -> None:
        self.shop_id = shop_id
        self.user_id = user.id
        self.is_primary_owner = is_primary_owner
        self.user = _UserRow(user)


def _shop_user_row_with_user(
    shop_id: int,
    user: FakeUser,
    is_primary_owner: int = 0,
) -> _ShopUserRow:
    return _ShopUserRow(shop_id, user, is_primary_owner)


def _fixed_dt() -> datetime:
    # DTZ001: DB 저장값과 동일하게 naive datetime 을 쓰는 테스트 stand-in 이다.
    return datetime(2026, 7, 3, 12, 0, 0)  # noqa: DTZ001  # naive 의도
