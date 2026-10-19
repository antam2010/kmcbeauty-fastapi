"""시술 예약을 미완료 상태로 되돌리면 완료 시각(finished_at)을 지우는 회귀 테스트.

대상:
- app/services/treatment_service.py::_update_treatment (upsert_treatment_service)

결함:
- 자동완료된 예약을 직원이 정정하며 VISITED/RESERVED 로 되돌려도 finished_at 이 그대로
  남았다. 자동완료 조회는 finished_at IS NULL 인 예약만 고르므로 이 예약은 다시는
  자동완료되지 않았다.

요구 동작:
- 불변식: 미완료(RESERVED/VISITED) 예약의 finished_at 은 NULL 이다. 수정 결과 상태가
  미완료면 finished_at 을 지운다(클라이언트가 finished_at 을 보내도 같다).
- 완료·취소·노쇼로 수정하면 finished_at 을 그대로 둔다.
- 항목·생성자(created_user_id)·교차 테넌트 검증 동작은 바꾸지 않는다
  (test_treatment_update_items_fix.py, test_treatment_cross_tenant_fk_security.py).

실제 라우터·서비스·CRUD 를 그대로 쓰고 DB/인증/상점 의존성만 바꾼다(tests/fake_sql.py).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.api import treatment as treatment_api
from app.crud import treatment_crud
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
TREATMENT_ID, ITEM_ID = 500, 5001
RESERVED_AT = datetime(2026, 10, 15, 1, 0, tzinfo=UTC)
# 자동완료 태스크가 기록한 완료 시각(naive UTC 컬럼 규약, 30분 시술 종료 후).
FINISHED_AT = datetime(2026, 10, 15, 2, 0)  # noqa: DTZ001  # naive UTC 컬럼 규약


def _world() -> FakeSQLSession:
    """자동완료된(COMPLETED, finished_at 있음) 예약 500 과 항목 1개가 있는 상점."""
    return FakeSQLSession(
        TreatmentMenu(id=1000, shop_id=SHOP_ID, name="메뉴"),
        TreatmentMenuDetail(
            id=1100,
            menu_id=1000,
            name="젤네일",
            duration_min=30,
            base_price=10000,
            created_at=RESERVED_AT,
            updated_at=RESERVED_AT,
        ),
        Treatment(
            id=TREATMENT_ID,
            shop_id=SHOP_ID,
            reserved_at=RESERVED_AT.replace(tzinfo=None),
            status=TreatmentStatus.COMPLETED,
            finished_at=FINISHED_AT,
            payment_method=PaymentMethod.CARD,
            created_user_id=OWNER_ID,
            created_at=RESERVED_AT,
            updated_at=RESERVED_AT,
        ),
        TreatmentItem(
            id=ITEM_ID,
            treatment_id=TREATMENT_ID,
            menu_detail_id=1100,
            base_price=10000,
            duration_min=30,
            session_no=1,
        ),
    )


def _put(db: FakeSQLSession, **body: object) -> int:
    """직원이 예약 500 을 수정한다(reserved_at 은 그대로 보낸다)."""
    api = FastAPI()
    api.include_router(treatment_api.router)
    api.dependency_overrides[get_db] = lambda: db
    api.dependency_overrides[get_current_shop] = lambda: CURRENT_SHOP
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=STAFF_ID)
    payload = {"reserved_at": RESERVED_AT.isoformat(), **body}
    return TestClient(api).put(f"/treatments/{TREATMENT_ID}", json=payload).status_code


def _treatment(db: FakeSQLSession) -> Treatment:
    return next(t for t in db.rows("treatment") if t.id == TREATMENT_ID)


@pytest.mark.parametrize("unfinished", ["VISITED", "RESERVED"])
def test_back_to_unfinished_status_clears_finished_at(unfinished: str) -> None:
    """COMPLETED → VISITED/RESERVED 정정은 finished_at 을 지운다(항목·생성자 유지)."""
    db = _world()

    assert _put(db, status=unfinished) == 200

    treatment = _treatment(db)
    assert treatment.status == unfinished
    assert treatment.finished_at is None
    assert [item.id for item in db.rows("treatment_item")] == [ITEM_ID]
    assert treatment.created_user_id == OWNER_ID


def test_unfinished_status_ignores_client_finished_at() -> None:
    """미완료 상태로 수정하며 finished_at 을 보내도 NULL 로 남는다(불변식 우선)."""
    db = _world()

    assert _put(db, status="VISITED", finished_at=RESERVED_AT.isoformat()) == 200

    assert _treatment(db).finished_at is None


@pytest.mark.parametrize("closed", ["COMPLETED", "CANCELLED", "NO_SHOW"])
def test_closed_status_keeps_finished_at(closed: str) -> None:
    """완료 유지·취소·노쇼로의 수정은 finished_at 을 그대로 둔다."""
    db = _world()

    assert _put(db, status=closed) == 200

    treatment = _treatment(db)
    assert treatment.status == closed
    assert treatment.finished_at == FINISHED_AT


def test_visited_again_is_picked_up_by_autocomplete() -> None:
    """VISITED 로 되돌린 지난 예약은 자동완료 조회가 다시 고른다."""
    db = _world()

    assert _put(db, status="VISITED") == 200

    later = FINISHED_AT + timedelta(hours=1)
    rows = treatment_crud.get_treatments_to_autocomplete(db, later)
    assert [row.treatment_id for row in rows] == [TREATMENT_ID]
