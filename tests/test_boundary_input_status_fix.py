"""경계 입력 500 → 4xx 교정 테스트.

대상:
1. app/schemas/shop.py::ShopPhoneValidatorMixin — "phone": null 이 AttributeError 로 500
2. GET /phonebooks/check-duplicate (app/api/phonebook.py ·
   app/services/phonebook_service.py::check_duplicate_phone_number_service)
   - 잘못된 번호(123)는 서비스가 만든 응답 모델 검증이 실패해 500
   - 정규화 전 번호(01012345678)를 그대로 조회해, 저장된 010-1234-5678 을 못 찾았다
3. PUT /treatments/{id} 의 treatment_items (TreatmentItemUpdate,
   app/services/treatment_service.py::_upsert_treatment_items)
   - 기존 항목의 base_price / duration_min / session_no 가 null 이면 NOT NULL 컬럼에
     NULL 을 UPDATE 해 500
   - 새 항목(id 없음 · 이 예약에 없는 id)에 필수 값이 없으면 기본값(0 · 1)이 조용히
     저장됐다
4. PATCH /device-tokens/{token_id} 로 이미 있는 토큰 문자열로 바꾸면 UNIQUE 위반이
   500 이었다

요구 동작:
1. phone 은 null 을 허용한다(생략과 같음).
2. 생성 경로와 같은 검증 · 정규화를 조회 전에 한다. 잘못된 번호는 422, 정규화 전 번호도
   저장된 번호를 찾는다.
3. 기존 항목의 null 은 "변경 없음"이다. 새 항목에 필수 값이 없으면 기존 서비스 관례대로
   400 이고 아무것도 쓰지 않는다. 교차 테넌트 검증 · created_user_id 동작은 그대로다.
4. 등록 경로의 "Token already exists" 처럼 409 다.

DB 없이 검증한다. 라우터만 올린 최소 FastAPI 앱에 의존성을 바꾸고, 전화번호부 · 시술
예약은 실제 CRUD 를 tests/fake_sql.py 위에서 실행한다.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.api import device_push_token as device_push_token_api
from app.api import phonebook as phonebook_api
from app.api import shop as shop_api
from app.api import treatment as treatment_api
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.shop import get_current_shop
from app.enum.treatment_status import PaymentMethod, TreatmentStatus
from app.models.device_push_token import DevicePushToken
from app.models.phonebook import Phonebook
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from app.models.treatment_menu import TreatmentMenu
from app.models.treatment_menu_detail import TreatmentMenuDetail
from app.schemas.shop import ShopCreate, ShopResponse, ShopUpdate
from app.services import device_push_token_service
from tests.fake_sql import FakeSQLSession

NOW = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
SHOP_ID, OTHER_SHOP_ID = 10, 20
OWNER_ID, STAFF_ID = 1, 3
CURRENT_SHOP = SimpleNamespace(id=SHOP_ID, user_id=OWNER_ID)


def _api(*routers: object, db: object = None, user_id: int = OWNER_ID) -> TestClient:
    api = FastAPI()
    for router in routers:
        api.include_router(router)
    api.dependency_overrides[get_db] = lambda: db
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id,
        role="MASTER",
    )
    api.dependency_overrides[get_current_shop] = lambda: CURRENT_SHOP
    return TestClient(api, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. 샵 전화번호 null
# ---------------------------------------------------------------------------

SHOP = {"name": "케이엠씨 뷰티", "address": "서울시 강남구"}


@pytest.fixture
def shop_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def _upsert(**kwargs: object) -> ShopResponse:
        return ShopResponse(
            id=1,
            created_at=NOW,
            updated_at=NOW,
            **kwargs["shop_data"].model_dump(),
        )

    monkeypatch.setattr(shop_api, "upsert_shop_service", _upsert)
    return _api(shop_api.router)


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [("POST", "/shops", 201), ("PUT", "/shops/1", 200)],
    ids=["create", "update"],
)
def test_shop_phone_null_is_accepted(
    shop_client: TestClient,
    method: str,
    path: str,
    expected: int,
) -> None:
    """전화번호(phone)가 null 이어도 500 이 아니라 전화번호 없음으로 처리된다."""
    response = shop_client.request(method, path, json={**SHOP, "phone": None})

    assert response.status_code == expected, response.text
    assert response.json()["phone"] is None


@pytest.mark.parametrize("schema", [ShopCreate, ShopUpdate])
def test_shop_phone_none_and_omitted_mean_no_phone(
    schema: type[ShopCreate | ShopUpdate],
) -> None:
    assert schema(**SHOP, phone=None).phone is None
    assert schema(**SHOP).phone is None


@pytest.mark.parametrize("schema", [ShopCreate, ShopUpdate])
def test_shop_phone_is_still_validated_and_normalized(
    schema: type[ShopCreate | ShopUpdate],
) -> None:
    """기존 동작 유지: 유효하면 정규화, 아니면 422 대상(ValueError)."""
    assert schema(**SHOP, phone="01098765432").phone == "010-9876-5432"
    with pytest.raises(ValueError, match="유효하지 않은 전화번호"):
        schema(**SHOP, phone="123")


# ---------------------------------------------------------------------------
# 2. 전화번호 중복 확인: 생성 경로와 같은 검증 · 정규화 후 조회
# ---------------------------------------------------------------------------


@pytest.fixture
def phonebook_client() -> TestClient:
    """010-1234-5678 이 저장된 샵(삭제된 02-123-4567 · 타 샵 031 번호 포함)."""
    db = FakeSQLSession(
        Phonebook(id=1, shop_id=SHOP_ID, name="홍길동", phone_number="010-1234-5678"),
        Phonebook(
            id=2,
            shop_id=SHOP_ID,
            name="삭제고객",
            phone_number="02-123-4567",
            deleted_at=NOW,
        ),
        Phonebook(
            id=3,
            shop_id=OTHER_SHOP_ID,
            name="타샵고객",
            phone_number="031-1234-5678",
        ),
    )
    return _api(phonebook_api.router, db=db)


def _check(client: TestClient, phone_number: str) -> object:
    return client.get(
        "/phonebooks/check-duplicate",
        params={"phone_number": phone_number},
    )


@pytest.mark.parametrize("phone_number", ["123", "010-12-5678", "abc", ""])
def test_check_duplicate_invalid_number_is_422(
    phonebook_client: TestClient,
    phone_number: str,
) -> None:
    response = _check(phonebook_client, phone_number)

    assert response.status_code == 422, response.text
    [error] = response.json()["detail"]
    assert error["loc"] == ["query", "phone_number"]
    assert "유효하지 않은 전화번호" in error["msg"]


@pytest.mark.parametrize(
    "phone_number",
    ["01012345678", "010-1234-5678", " 01012345678 ", "010-12345678"],
    ids=["digits", "normalized", "padded", "partial-dash"],
)
def test_check_duplicate_finds_stored_number_in_any_format(
    phonebook_client: TestClient,
    phone_number: str,
) -> None:
    """정규화 전 입력도 저장된 정규형(010-1234-5678)을 찾는다."""
    response = _check(phonebook_client, phone_number)

    assert response.status_code == 200, response.text
    assert response.json() == {"exists": True, "phone_number": "010-1234-5678"}


@pytest.mark.parametrize(
    ("phone_number", "normalized"),
    [("021234567", "02-123-4567"), ("03112345678", "031-1234-5678")],
    ids=["soft-deleted", "other-shop"],
)
def test_check_duplicate_ignores_deleted_and_other_shop_numbers(
    phonebook_client: TestClient,
    phone_number: str,
    normalized: str,
) -> None:
    """기존 범위 유지: 삭제된 연락처 · 타 샵 번호는 중복이 아니다."""
    response = _check(phonebook_client, phone_number)

    assert response.status_code == 200, response.text
    assert response.json() == {"exists": False, "phone_number": normalized}


def test_check_duplicate_accepts_app_default_customer_number(
    phonebook_client: TestClient,
) -> None:
    """앱 기본 고객 번호(999-9999-9999)는 그대로 확인된다."""
    response = _check(phonebook_client, "999-9999-9999")

    assert response.status_code == 200, response.text
    assert response.json() == {"exists": False, "phone_number": "999-9999-9999"}


# ---------------------------------------------------------------------------
# 3. 시술 항목 수정: 기존 항목의 null 은 변경 없음, 새 항목의 누락은 400
# ---------------------------------------------------------------------------

TREATMENT_ID = 500


def _detail(detail_id: int, menu_id: int) -> TreatmentMenuDetail:
    return TreatmentMenuDetail(
        id=detail_id,
        menu_id=menu_id,
        name=f"상세{detail_id}",
        duration_min=30,
        base_price=10000,
        created_at=NOW,
        updated_at=NOW,
    )


def _stored_item(item_id: int, menu_detail_id: int) -> TreatmentItem:
    return TreatmentItem(
        id=item_id,
        treatment_id=TREATMENT_ID,
        menu_detail_id=menu_detail_id,
        base_price=10000,
        duration_min=30,
        session_no=1,
    )


def _treatment_world() -> FakeSQLSession:
    """원장이 만든 예약 500(항목 5001 · 5002) + 타 샵 메뉴 상세 2100."""
    return FakeSQLSession(
        TreatmentMenu(id=1000, shop_id=SHOP_ID, name="메뉴"),
        TreatmentMenu(id=2000, shop_id=OTHER_SHOP_ID, name="타 샵 메뉴"),
        _detail(1100, 1000),
        _detail(1101, 1000),
        _detail(2100, 2000),
        Treatment(
            id=TREATMENT_ID,
            shop_id=SHOP_ID,
            reserved_at=NOW.replace(tzinfo=None),
            status=TreatmentStatus.RESERVED,
            payment_method=PaymentMethod.CARD,
            created_user_id=OWNER_ID,
            created_at=NOW,
            updated_at=NOW,
        ),
        _stored_item(5001, 1100),
        _stored_item(5002, 1101),
    )


def _put_items(db: FakeSQLSession, items: list[dict]) -> object:
    body = {
        "reserved_at": NOW.isoformat(),
        "status": "RESERVED",
        "treatment_items": items,
    }
    client = _api(treatment_api.router, db=db, user_id=STAFF_ID)
    return client.put(f"/treatments/{TREATMENT_ID}", json=body)


def _values(db: FakeSQLSession) -> dict[int, tuple]:
    return {
        item.id: (
            item.menu_detail_id,
            item.base_price,
            item.duration_min,
            item.session_no,
        )
        for item in db.rows("treatment_item")
    }


KEEP_5002 = {
    "id": 5002,
    "menu_detail_id": 1101,
    "base_price": 10000,
    "duration_min": 30,
    "session_no": 1,
}
UNCHANGED = {5001: (1100, 10000, 30, 1), 5002: (1101, 10000, 30, 1)}


@pytest.mark.parametrize(
    "item_5001",
    [
        {
            "id": 5001,
            "menu_detail_id": 1100,
            "base_price": None,
            "duration_min": None,
            "session_no": None,
        },
        {"id": 5001, "menu_detail_id": 1100},
    ],
    ids=["explicit-null", "omitted"],
)
def test_existing_item_null_fields_keep_stored_values(item_5001: dict) -> None:
    """기존 항목의 null(또는 생략) 필드는 저장된 값을 그대로 둔다(500 아님)."""
    db = _treatment_world()

    response = _put_items(db, [item_5001, KEEP_5002])

    assert response.status_code == 200, response.text
    assert _values(db) == UNCHANGED
    assert db.deleted == []
    # 생성자는 수정자(직원)가 아니라 원래 생성자(원장)로 남는다.
    [treatment] = db.rows("treatment")
    assert treatment.created_user_id == OWNER_ID


def test_existing_item_updates_only_given_fields() -> None:
    db = _treatment_world()
    item = {"id": 5001, "menu_detail_id": 1100, "base_price": None, "duration_min": 45}

    response = _put_items(db, [item, KEEP_5002])

    assert response.status_code == 200, response.text
    assert _values(db)[5001] == (1100, 10000, 45, 1)


NEW_ITEM = {
    "menu_detail_id": 1100,
    "base_price": 20000,
    "duration_min": 60,
    "session_no": 2,
}


@pytest.mark.parametrize(
    ("new_item", "missing"),
    [
        ({**NEW_ITEM, "base_price": None}, "base_price"),
        ({**NEW_ITEM, "duration_min": None}, "duration_min"),
        ({**NEW_ITEM, "session_no": None}, "session_no"),
        ({"menu_detail_id": 1100}, "base_price"),
        ({**NEW_ITEM, "id": 9999, "duration_min": None}, "duration_min"),
    ],
    ids=["null-price", "null-duration", "null-session", "omitted-all", "unknown-id"],
)
def test_new_item_missing_required_value_is_400_without_writes(
    new_item: dict,
    missing: str,
) -> None:
    """새 항목(id 없음 · 이 예약에 없는 id)에 필수 값이 없으면 400 이고 쓰지 않는다."""
    db = _treatment_world()

    response = _put_items(db, [new_item])

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "TREATMENT_BAD_REQUEST"
    assert missing in detail["detail"]
    assert db.commit_count == 0
    assert _values(db) == UNCHANGED  # 기존 항목 삭제 · 새 항목 추가 없음
    assert db.deleted == []


def test_new_item_with_all_values_is_still_added() -> None:
    db = _treatment_world()

    response = _put_items(db, [KEEP_5002, NEW_ITEM])

    assert response.status_code == 200, response.text
    values = _values(db)
    assert values[5002] == UNCHANGED[5002]
    [added] = [v for item_id, v in values.items() if item_id not in UNCHANGED]
    assert added == (1100, 20000, 60, 2)
    assert [item.id for item in db.deleted] == [5001]


@pytest.mark.parametrize(
    "items",
    [
        [{"id": 5001, "menu_detail_id": 2100, "base_price": None}, KEEP_5002],
        [KEEP_5002, {**NEW_ITEM, "menu_detail_id": 2100}],
        [{"id": 5001, "menu_detail_id": None, "base_price": None}, KEEP_5002],
    ],
    ids=["existing-to-other-shop", "new-other-shop", "existing-null-detail"],
)
def test_menu_detail_tenant_validation_is_unchanged(items: list[dict]) -> None:
    """교차 테넌트 검증 유지: 타 샵 · 없는 상세는 null 필드와 무관하게 400 이다."""
    db = _treatment_world()

    response = _put_items(db, items)

    assert response.status_code == 400, response.text
    assert "menu_detail_id" in response.json()["detail"]["detail"]
    assert db.commit_count == 0
    assert _values(db) == UNCHANGED


# ---------------------------------------------------------------------------
# 4. 디바이스 토큰 수정: 이미 있는 토큰 문자열이면 409
# ---------------------------------------------------------------------------

FCM_MINE = "fcm-registration-mine"
FCM_TAKEN = "fcm-registration-taken"


class UniqueTokenSession:
    """flush 때 token UNIQUE 위반을 IntegrityError 로 내는 세션(다른 행의 토큰)."""

    def __init__(self, row: DevicePushToken, other_tokens: set[str]) -> None:
        self.row = row
        self.other_tokens = other_tokens
        self.commit_count = 0
        self.rollback_count = 0

    def flush(self) -> None:
        if self.row.token in self.other_tokens:
            statement = "UPDATE device_push_tokens SET token=..."
            orig = Exception("duplicate key value violates unique constraint")
            raise IntegrityError(statement, {}, orig)

    def commit(self) -> None:
        self.flush()
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1

    def refresh(self, _obj: object) -> None:
        """DB 재조회 없음."""


@pytest.fixture
def token_session(monkeypatch: pytest.MonkeyPatch) -> UniqueTokenSession:
    row = DevicePushToken(
        id=7,
        user_id=OWNER_ID,
        token=FCM_MINE,
        platform="android",
        is_active=True,
    )
    monkeypatch.setattr(
        device_push_token_service,
        "get_user_device_token_by_id",
        lambda _db, user_id, token_id: row if (user_id, token_id) == (1, 7) else None,
    )
    return UniqueTokenSession(row, other_tokens={FCM_TAKEN})


def test_patch_to_existing_token_is_409(token_session: UniqueTokenSession) -> None:
    client = _api(device_push_token_api.router, db=token_session)

    response = client.patch("/device-tokens/7", json={"token": FCM_TAKEN})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["code"], detail["detail"]) == (
        "DEVICE_PUSH_TOKEN_CONFLICT",
        "Token already exists",
    )
    assert (token_session.commit_count, token_session.rollback_count) == (0, 1)


def test_patch_to_new_token_still_succeeds(token_session: UniqueTokenSession) -> None:
    client = _api(device_push_token_api.router, db=token_session)

    response = client.patch(
        "/device-tokens/7",
        json={"token": "fcm-registration-new", "platform": "ios"},
    )

    assert response.status_code == 200, response.text
    assert (response.json()["token"], response.json()["platform"]) == (
        "fcm-registration-new",
        "ios",
    )
    assert token_session.commit_count == 1
