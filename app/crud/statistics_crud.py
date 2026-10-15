from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.sql import case

from app.enum.treatment_status import PaymentMethod, TreatmentStatus
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from app.models.treatment_menu_detail import TreatmentMenuDetail
from app.models.user import User
from app.schemas.dashboard import (
    DashboardCustomerInsight,
    DashboardStaffSummaryItem,
    TreatmentItemBase,
    TreatmentSalesItem,
    TreatmentSummarySchema,
)
from app.schemas.treatment_menu_detail import TreatmentMenuDetailBase
from app.schemas.user import UserBaseResponse
from app.utils.query import apply_date_range_filter


def get_treatment_summary(
    db: Session,
    shop_id: int,
    start_date: date,
    end_date: date,
) -> TreatmentSummarySchema:
    # 집계용 결제/상태 구분
    unpaid_methods = PaymentMethod.unpaid_methods()
    paid_methods = PaymentMethod.paid_methods()
    expected_statuses = TreatmentStatus.for_expected_sales()
    completed_status = TreatmentStatus.for_actual_sales()

    # group by 상태+결제방식
    stmt = (
        select(
            Treatment.status,
            Treatment.payment_method,
            func.count(func.distinct(Treatment.id)).label("count"),
            func.sum(TreatmentItem.base_price).label("total_price"),
            func.sum(
                case(
                    # case 구문에서 unpaid_methods 리스트 직접 비교
                    (
                        Treatment.payment_method.in_(unpaid_methods),
                        TreatmentItem.base_price,
                    ),
                    else_=0,
                ),
            ).label("unpaid_total"),
        )
        .join(TreatmentItem, TreatmentItem.treatment_id == Treatment.id)
        .where(Treatment.shop_id == shop_id)
    )
    stmt = apply_date_range_filter(stmt, Treatment.reserved_at, start_date, end_date)
    stmt = stmt.group_by(Treatment.status, Treatment.payment_method)

    results = db.execute(stmt).fetchall()

    # 딕셔너리 가공
    count_map = {}
    sales_map = {}
    status_count = {}
    status_sales = {}

    for row in results:
        key = (row.status, row.payment_method)
        count_map[key] = row.count
        sales_map[key] = int(row.total_price or 0)
        status_count[row.status] = status_count.get(row.status, 0) + row.count
        status_sales[row.status] = status_sales.get(row.status, 0) + int(
            row.total_price or 0,
        )

    # 전체 예약 건수 (노쇼/취소 포함)
    total_reservations = sum(status_count.values())

    # 예상매출: RESERVED, VISITED, COMPLETED만 합산 (enum 메서드 사용)
    expected_sales = sum(status_sales.get(st, 0) for st in expected_statuses)

    # 실매출: COMPLETED 결제완료(카드/현금)
    actual_sales = sum(
        sales_map.get((completed_status, pay), 0) for pay in paid_methods
    )

    # 외상 합계: COMPLETED + 미수금(외상)
    unpaid_total = sum(
        sales_map.get((completed_status, pay), 0) for pay in unpaid_methods
    )
    return TreatmentSummarySchema(
        total_reservations=total_reservations,
        completed=status_count.get(TreatmentStatus.COMPLETED.value, 0),
        reserved=status_count.get(TreatmentStatus.RESERVED.value, 0),
        visited=status_count.get(TreatmentStatus.VISITED.value, 0),
        canceled=status_count.get(TreatmentStatus.CANCELLED.value, 0),
        no_show=status_count.get(TreatmentStatus.NO_SHOW.value, 0),
        expected_sales=expected_sales,
        actual_sales=actual_sales,
        unpaid_total=unpaid_total,
    )


def get_treatment_sales_summary(
    db: Session,
    shop_id: int,
    start_date: date,
    end_date: date,
) -> list[TreatmentSalesItem]:
    # 결제방식 enum 활용
    paid_methods = PaymentMethod.paid_methods()
    expected_statuses = TreatmentStatus.for_expected_sales()
    completed_status = TreatmentStatus.for_actual_sales()

    stmt = (
        select(
            TreatmentItem.menu_detail_id,
            TreatmentMenuDetail.name,
            func.count(TreatmentItem.id).label("count"),
            func.sum(
                # 예상매출: 노쇼/취소 제외, 실제 매출 가능성 있는 상태만 합산
                case(
                    (
                        Treatment.status.in_(expected_statuses),
                        TreatmentItem.base_price,
                    ),
                    else_=0,
                ),
            ).label("expected_price"),
            func.sum(
                # 실매출: COMPLETED  결제완료만
                case(
                    (
                        (Treatment.status == completed_status)
                        & (Treatment.payment_method.in_(paid_methods)),
                        TreatmentItem.base_price,
                    ),
                    else_=0,
                ),
            ).label("actual_price"),
        )
        .join(Treatment, TreatmentItem.treatment_id == Treatment.id)
        .join(
            TreatmentMenuDetail,
            TreatmentItem.menu_detail_id == TreatmentMenuDetail.id,
        )
        .where(Treatment.shop_id == shop_id)
        .group_by(TreatmentItem.menu_detail_id, TreatmentMenuDetail.name)
    )
    stmt = apply_date_range_filter(stmt, Treatment.reserved_at, start_date, end_date)

    results = db.execute(stmt).fetchall()

    # 결과를 스키마 리스트로 가공
    return [
        TreatmentSalesItem(
            menu_detail_id=row.menu_detail_id,
            name=row.name,
            count=row.count,
            expected_price=int(row.expected_price or 0),
            actual_price=int(row.actual_price or 0),
        )
        for row in results
    ]


def get_today_reservation_list_with_customer_insight(
    db: Session,
    shop_id: int,
    start_date: date,
    end_date: date,
) -> list[DashboardCustomerInsight]:
    # 1. 오늘 예약 리스트(관계: 고객, 담당자, 시술항목) 쿼리
    stmt = (
        select(Treatment)
        .options(
            joinedload(Treatment.treatment_items).joinedload(TreatmentItem.menu_detail),
            joinedload(Treatment.phonebook),
            joinedload(Treatment.staff_user),
        )
        .where(Treatment.shop_id == shop_id)
        .order_by(Treatment.reserved_at.asc())
    )
    stmt = apply_date_range_filter(
        stmt,
        Treatment.reserved_at,
        start_date,
        end_date,
    )
    treatments = db.execute(stmt).unique().scalars().all()

    # 2. 오늘 예약에 등장한 phonebook_id 한 번에 추출
    phonebook_ids = list({t.phonebook_id for t in treatments})
    # 3. 고객 인사이트(누적예약수, 노쇼, 외상, 총 결제금액) 일괄 쿼리 (dict 매핑)
    insight_map = get_customer_insight_bulk(db, shop_id, phonebook_ids)

    # 4. 최종 응답 구조로 가공
    result = []
    for t in treatments:
        items = t.treatment_items
        treatments_list = [
            TreatmentItemBase(
                treatment_id=t.id,
                menu_detail_id=it.menu_detail_id,
                base_price=it.base_price or 0,
                duration_min=it.duration_min or 0,
                session_no=it.session_no,
                menu_detail=TreatmentMenuDetailBase.model_validate(it.menu_detail),
            )
            for it in items
        ]
        total_duration = sum(it.duration_min or 0 for it in items)
        total_price = sum(it.base_price or 0 for it in items)
        phonebook = t.phonebook
        staff_name = t.staff_user.name if t.staff_user else None
        customer_insight = insight_map.get(t.phonebook_id, {})

        # 고객 정보: phonebook 우선, 없으면 customer_name/phone 사용
        customer_name = phonebook.name if phonebook else t.customer_name
        phone_number = phonebook.phone_number if phonebook else t.customer_phone

        result.append(
            DashboardCustomerInsight(
                id=t.id,
                reserved_at=t.reserved_at,
                customer_name=customer_name,
                phone_number=phone_number,
                status=t.status,
                treatments=treatments_list,
                total_duration_min=total_duration,
                total_price=total_price,
                memo=t.memo,
                payment_method=t.payment_method,
                staff=staff_name,
                staff_user=UserBaseResponse.model_validate(t.staff_user)
                if t.staff_user
                else None,
                total_reservations=customer_insight.get("total_reservations", 0),
                no_show_count=customer_insight.get("no_show_count", 0),
                no_show_rate=customer_insight.get("no_show_rate", 0.0),
                unpaid_amount=customer_insight.get("unpaid_amount", 0),
                total_spent=customer_insight.get("total_spent", 0),
            ),
        )
    return result


def get_customer_insight_bulk(
    db: Session,
    shop_id: int,
    phonebook_ids: list[int],
) -> dict[int, dict]:
    """고객별 전체 예약수, 노쇼수, 노쇼율, 외상합, 총 결제금액 등 일괄 반환."""
    completed_status = TreatmentStatus.for_actual_sales()

    subq = (
        select(
            Treatment.phonebook_id.label("phonebook_id"),
            func.count(Treatment.id).label("total_reservations"),
            func.sum(
                case((Treatment.status == TreatmentStatus.NO_SHOW.value, 1), else_=0),
            ).label(
                "no_show_count",
            ),
            func.sum(
                case(
                    (
                        (Treatment.status == completed_status)
                        & (Treatment.payment_method == PaymentMethod.UNPAID.value),
                        TreatmentItem.base_price,
                    ),
                    else_=0,
                ),
            ).label("unpaid_amount"),
            func.sum(
                case(
                    (
                        (Treatment.status == completed_status)
                        & (Treatment.payment_method.in_(PaymentMethod.paid_methods())),
                        TreatmentItem.base_price,
                    ),
                    else_=0,
                ),
            ).label("total_spent"),
        )
        .join(TreatmentItem, Treatment.id == TreatmentItem.treatment_id)
        .where(Treatment.shop_id == shop_id)
        .where(Treatment.phonebook_id.in_(phonebook_ids))
        .group_by(Treatment.phonebook_id)
    )
    insight_rows = db.execute(subq).fetchall()
    insight_map = {}
    for row in insight_rows:
        no_show_rate = (
            (row.no_show_count / row.total_reservations) * 100
            if row.total_reservations
            else 0
        )
        insight_map[row.phonebook_id] = {
            "total_reservations": row.total_reservations,
            "no_show_count": row.no_show_count,
            "no_show_rate": round(no_show_rate, 1),
            "unpaid_amount": int(row.unpaid_amount or 0),
            "total_spent": int(row.total_spent or 0),
        }
    return insight_map


def get_staff_summary(
    db: Session,
    shop_id: int,
    start_date: date,
    end_date: date,
) -> list[DashboardStaffSummaryItem]:
    """시술 담당자별 예약 건수 요약 조회."""
    stmt = (
        select(
            Treatment.staff_user_id,
            User.name.label("staff_name"),
            func.count(Treatment.id).label("count"),
        )
        .join(User, Treatment.staff_user_id == User.id)
        .where(Treatment.shop_id == shop_id)
        .where(Treatment.staff_user_id.is_not(None))
        # PostgreSQL 은 비집계 SELECT 컬럼(users.name)이 GROUP BY 에 없으면 거부한다.
        .group_by(Treatment.staff_user_id, User.name)
    )
    stmt = apply_date_range_filter(
        stmt,
        Treatment.reserved_at,
        start_date,
        end_date,
    )

    rows = db.execute(stmt).fetchall()

    return [
        DashboardStaffSummaryItem(
            staff_id=row.staff_user_id,
            staff_name=row.staff_name or "알 수 없는 스태프",
            count=row.count,
        )
        for row in rows
    ]
