"""대시보드 통계 집계 단위 회귀 테스트(건수는 예약 1건 단위).

대상: app/crud/statistics_crud.py
- get_customer_insight_bulk: 시술 항목을 조인한 뒤 예약 id 와 노쇼를 항목 행마다 세어
  누적 예약수/노쇼수가 항목 수만큼 부풀었다. 항목 없는 예약은 내부 조인에서 빠졌다.
- get_treatment_summary: 항목 내부 조인 때문에 항목 없는 예약이 건수에서 빠졌다.

요구 동작:
- 건수(전체 예약/노쇼/상태별)는 예약 단위로 센다. 항목 수와 무관하고 항목 없는
  예약도 포함한다.
- 금액(예상/실매출/외상/총결제)은 기존 의미(시술 항목 base_price 합)를 유지하고,
  항목이 없으면 0 으로 본다.

tests/fake_sql.py 의 in-memory SQL 평가기로 실제 CRUD 문을 DB 없이 실행한다(조인 행
곱과 GROUP BY 집계가 SQL 의미 그대로 재현된다).
"""

from datetime import date, datetime

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.crud import statistics_crud
from app.enum.treatment_status import PaymentMethod, TreatmentStatus
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from tests.fake_sql import FakeSQLSession

SHOP, OTHER_SHOP = 10, 20
CUSTOMER = 100
DAY = date(2026, 10, 15)
# naive UTC 저장 규약: 2026-10-15 01:00 UTC = 10:00 KST (같은 KST 날짜)
RESERVED_AT = datetime(2026, 10, 15, 1, 0)  # noqa: DTZ001  # naive UTC 컬럼 규약


def _treatment(
    treatment_id: int,
    status: TreatmentStatus,
    payment_method: PaymentMethod = PaymentMethod.CARD,
    shop_id: int = SHOP,
) -> Treatment:
    return Treatment(
        id=treatment_id,
        shop_id=shop_id,
        phonebook_id=CUSTOMER,
        reserved_at=RESERVED_AT,
        status=status,
        payment_method=payment_method,
    )


def _items(treatment_id: int, *prices: int) -> list[TreatmentItem]:
    return [
        TreatmentItem(
            id=treatment_id * 100 + seq,
            treatment_id=treatment_id,
            menu_detail_id=1,
            base_price=price,
            duration_min=30,
            session_no=1,
        )
        for seq, price in enumerate(prices)
    ]


def _world(*extra: object) -> FakeSQLSession:
    """완료(항목 3개) / 노쇼(항목 1개) / 예약(항목 없음) 예약 3건을 가진 고객."""
    return FakeSQLSession(
        _treatment(1, TreatmentStatus.COMPLETED),
        *_items(1, 10000, 10000, 10000),
        _treatment(2, TreatmentStatus.NO_SHOW),
        *_items(2, 20000),
        _treatment(3, TreatmentStatus.RESERVED),
        # 타 상점 예약은 어떤 집계에도 섞이면 안 된다.
        _treatment(9, TreatmentStatus.NO_SHOW, shop_id=OTHER_SHOP),
        *_items(9, 50000),
        *extra,
    )


def test_customer_insight_counts_each_treatment_once() -> None:
    """누적 예약수/노쇼수는 예약 단위다(항목 수 무관, 항목 없는 예약 포함)."""
    insight = statistics_crud.get_customer_insight_bulk(_world(), SHOP, [CUSTOMER])

    assert insight[CUSTOMER] == {
        "total_reservations": 3,
        "no_show_count": 1,
        "no_show_rate": 33.3,
        "unpaid_amount": 0,
        "total_spent": 30000,  # 완료+결제(카드) 예약의 항목 금액 합
    }


def test_customer_insight_keeps_item_price_money_semantics() -> None:
    """외상/총결제 금액은 여전히 완료 예약의 항목 base_price 합이다."""
    unpaid = _treatment(4, TreatmentStatus.COMPLETED, PaymentMethod.UNPAID)
    db = _world(unpaid, *_items(4, 5000, 7000))

    insight = statistics_crud.get_customer_insight_bulk(db, SHOP, [CUSTOMER])

    assert insight[CUSTOMER]["total_reservations"] == 4
    assert insight[CUSTOMER]["no_show_rate"] == 25.0
    assert insight[CUSTOMER]["unpaid_amount"] == 12000
    assert insight[CUSTOMER]["total_spent"] == 30000


def test_same_day_summary_counts_treatments_not_items() -> None:
    """당일 요약은 예약 3건을 센다(항목 없는 예약 포함). 금액은 항목 합이다."""
    summary = statistics_crud.get_treatment_summary(_world(), SHOP, DAY, DAY)

    assert summary.model_dump() == {
        "total_reservations": 3,
        "completed": 1,
        "reserved": 1,
        "visited": 0,
        "canceled": 0,
        "no_show": 1,
        "expected_sales": 30000,  # RESERVED/VISITED/COMPLETED 항목 합(노쇼 제외)
        "actual_sales": 30000,  # COMPLETED + 카드/현금
        "unpaid_total": 0,
    }


def test_summary_unpaid_total_sums_completed_unpaid_items() -> None:
    """외상 합계는 COMPLETED + 미수금 예약의 항목 금액 합이다."""
    unpaid = _treatment(4, TreatmentStatus.COMPLETED, PaymentMethod.UNPAID)
    db = _world(unpaid, *_items(4, 5000, 7000))

    summary = statistics_crud.get_treatment_summary(db, SHOP, DAY, DAY)

    assert summary.total_reservations == 4
    assert summary.completed == 2
    assert summary.unpaid_total == 12000
    assert summary.actual_sales == 30000
    assert summary.expected_sales == 42000
