"""시술 예약 수정 시 항목 보존 · 생성자(created_user_id) 기록 회귀 테스트.

대상:
- app/schemas/treatment.py::TreatmentUpdate.treatment_items
- app/services/treatment_service.py::upsert_treatment_service / _create_treatment
- app/api/treatment.py (요청자를 서비스로 전달)

결함:
- PUT /treatments/{id} 에 treatment_items 를 보내지 않으면 기본값 [] 가 적용되어 기존
  항목이 모두 삭제됐다. 다른 필드는 exclude_unset 부분 수정인데 항목만 전체 교체였다.
  treatment_items: null 은 스키마(list)가 거절했다(422).
- 예약 생성 시 created_user_id 에 요청자가 아닌 상점 원장(shop.user_id)이 기록됐다.

요구 동작:
- 수정에서 treatment_items 가 생략되거나 null 이면 기존 항목을 건드리지 않는다. 목록을
  보내면 기존처럼 upsert 한다(빈 목록은 전부 삭제). 생성은 기존 동작 그대로다.
- 생성 시 created_user_id 는 요청자 id 다. 수정은 created_user_id 를 바꾸지 않는다.
- 교차 테넌트 참조 검증(test_treatment_cross_tenant_fk_security.py)은 그대로 유지한다.

실제 라우터·서비스·CRUD 를 그대로 쓰고 DB/인증/상점 의존성만 바꾼다(tests/fake_sql.py).
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.api import treatment as treatment_api
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.shop import get_current_shop
from app.enum.treatment_status import PaymentMethod, TreatmentStatus
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from app.models.treatment_menu import TreatmentMenu
from app.models.treatment_menu_detail import TreatmentMenuDetail
from tests.fake_sql import FakeSQLSession

SHOP_ID, OWNER_ID, STAFF_ID = 10, 1, 3
CURRENT_SHOP = SimpleNamespace(id=SHOP_ID, user_id=OWNER_ID)
NOW = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
TREATMENT_ID = 500
ITEM_IDS = [5001, 5002, 5003]


def _detail(detail_id: int) -> TreatmentMenuDetail:
    return TreatmentMenuDetail(
        id=detail_id,
        menu_id=1000,
        name=f"상세{detail_id}",
        duration_min=30,
        base_price=10000,
        created_at=NOW,
        updated_at=NOW,
    )


def _item(item_id: int, menu_detail_id: int) -> TreatmentItem:
    return TreatmentItem(
        id=item_id,
        treatment_id=TREATMENT_ID,
        menu_detail_id=menu_detail_id,
        base_price=10000,
        duration_min=30,
        session_no=1,
    )


def _world() -> FakeSQLSession:
    """원장이 만든 예약 500(항목 3개)이 있는 상점."""
    return FakeSQLSession(
        TreatmentMenu(id=1000, shop_id=SHOP_ID, name="메뉴"),
        _detail(1100),
        _detail(1101),
        _detail(1102),
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
        _item(5001, 1100),
        _item(5002, 1101),
        _item(5003, 1102),
    )


def _client(db: FakeSQLSession, requester_id: int = STAFF_ID) -> TestClient:
    api = FastAPI()
    api.include_router(treatment_api.router)
    api.dependency_overrides[get_db] = lambda: db
    api.dependency_overrides[get_current_shop] = lambda: CURRENT_SHOP
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=requester_id,
    )
    return TestClient(api)


def _put(db: FakeSQLSession, **extra: object) -> int:
    body = {"reserved_at": NOW.isoformat(), "status": "RESERVED", **extra}
    response = _client(db).put(f"/treatments/{TREATMENT_ID}", json=body)
    return response.status_code


def _treatment(db: FakeSQLSession, treatment_id: int = TREATMENT_ID) -> Treatment:
    return next(t for t in db.rows("treatment") if t.id == treatment_id)


def _item_ids(db: FakeSQLSession) -> list[int]:
    return sorted(item.id for item in db.rows("treatment_item"))


def test_put_without_items_keeps_all_items() -> None:
    """항목을 보내지 않은 부분 수정은 기존 항목 3개를 그대로 둔다."""
    db = _world()

    assert _put(db, memo="메모만 수정") == 200

    assert _item_ids(db) == ITEM_IDS
    assert db.deleted == []
    assert _treatment(db).memo == "메모만 수정"


def test_put_with_null_items_keeps_items() -> None:
    """treatment_items: null 도 생략과 같다(오류 없이 항목 유지)."""
    db = _world()

    assert _put(db, memo="null 항목", treatment_items=None) == 200

    assert _item_ids(db) == ITEM_IDS
    assert db.deleted == []


def test_put_with_item_list_still_upserts() -> None:
    """목록을 보내면 기존처럼 맞춘다(id 일치 수정, 빠진 항목 삭제, 새 항목 추가)."""
    db = _world()
    items = [
        {
            "id": 5001,
            "menu_detail_id": 1100,
            "base_price": 15000,
            "duration_min": 45,
            "session_no": 2,
        },
        {
            "menu_detail_id": 1101,
            "base_price": 20000,
            "duration_min": 60,
            "session_no": 1,
        },
    ]

    assert _put(db, treatment_items=items) == 200

    kept = next(item for item in db.rows("treatment_item") if item.id == 5001)
    assert (kept.base_price, kept.duration_min, kept.session_no) == (15000, 45, 2)
    assert sorted(item.id for item in db.deleted) == [5002, 5003]
    [added] = [item for item in db.rows("treatment_item") if item.id not in ITEM_IDS]
    assert (added.menu_detail_id, added.base_price) == (1101, 20000)


def test_put_with_empty_item_list_removes_all_items() -> None:
    """빈 목록은 명시적인 전체 삭제 요청이다(기존 동작 유지)."""
    db = _world()

    assert _put(db, treatment_items=[]) == 200

    assert _item_ids(db) == []


@pytest.mark.parametrize("requester_id", [STAFF_ID, OWNER_ID], ids=["staff", "owner"])
def test_create_records_requester_as_created_user(requester_id: int) -> None:
    """생성 시 created_user_id 는 상점 원장이 아니라 요청자다."""
    db = _world()
    body = {
        "reserved_at": NOW.isoformat(),
        "status": "RESERVED",
        "treatment_items": [
            {
                "menu_detail_id": 1100,
                "base_price": 0,
                "duration_min": 30,
                "session_no": 1,
            },
        ],
    }

    response = _client(db, requester_id).post("/treatments", json=body)

    assert response.status_code == 201
    created = _treatment(db, response.json()["id"])
    assert created.created_user_id == requester_id


def test_update_does_not_change_created_user() -> None:
    """직원이 수정해도 생성자는 원래 생성자(원장)로 남는다."""
    db = _world()

    assert _put(db, memo="직원 수정") == 200

    assert _treatment(db).created_user_id == OWNER_ID
