from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.enum.treatment_status import TreatmentStatus
from app.models.phonebook import Phonebook
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from app.models.treatment_menu import TreatmentMenu
from app.models.treatment_menu_detail import TreatmentMenuDetail
from app.schemas.treatment import TreatmentAutoComplete, TreatmentFilter
from app.utils.query import apply_date_range_filter


# 시술 예약 등록
def create_treatment(db: Session, treatment: Treatment) -> Treatment:
    db.add(treatment)
    db.flush()
    return treatment


# 시술 예약 조회
def get_treatment_by_id(db: Session, treatment_id: int) -> Treatment | None:
    return db.query(Treatment).filter(Treatment.id == treatment_id).first()


# 시술 예약 항목 삭제
def delete_treatment_items(db: Session, treatment_id: int) -> None:
    db.query(TreatmentItem).filter(TreatmentItem.treatment_id == treatment_id).delete()


# 시술 예약 항목 등록
def create_treatment_item(db: Session, treatment_item: TreatmentItem) -> TreatmentItem:
    db.add(treatment_item)
    db.flush()
    return treatment_item


def stmt_treatment_list(
    shop_id: int,
    filters: TreatmentFilter,
) -> select:
    stmt = (
        select(Treatment)
        .options(
            joinedload(Treatment.treatment_items).joinedload(TreatmentItem.menu_detail),
            joinedload(Treatment.phonebook),
            joinedload(Treatment.staff_user),
        )
        .where(Treatment.shop_id == shop_id)
    )

    # 날짜 필터
    stmt = apply_date_range_filter(
        stmt,
        Treatment.reserved_at,
        filters.start_date,
        filters.end_date,
    )

    # 상태 필터
    if filters.status:
        stmt = stmt.where(Treatment.status == filters.status)

    # 시술 담당자 필터
    if filters.staff_user_id:
        stmt = stmt.where(Treatment.staff_user_id == filters.staff_user_id)

    # 검색 필터
    if filters.search:
        keyword = f"%{filters.search}%"
        stmt = (
            stmt.outerjoin(Treatment.phonebook)
            .outerjoin(Treatment.treatment_items)
            .outerjoin(TreatmentItem.menu_detail)
            .where(
                or_(
                    # 전화번호부 고객 정보
                    Phonebook.name.ilike(keyword),
                    Phonebook.phone_number.ilike(keyword),
                    # 직접 입력된 고객 정보
                    Treatment.customer_name.ilike(keyword),
                    Treatment.customer_phone.ilike(keyword),
                ),
            )
        )

    # 정렬
    # sort_order 는 {asc, desc} 화이트리스트로 명시 검증한다. 무효값(조작된 값)은
    # getattr(sort_column, sort_order, None)() 로 None 을 호출해 500 크래시가 나므로
    # 기본값 desc 로 안전하게 대체한다. (SPEC-FIX-001 REQ-FIX-005)
    if filters.sort_by and hasattr(Treatment, filters.sort_by):
        sort_column = getattr(Treatment, filters.sort_by)
        sort_order = (
            filters.sort_order if filters.sort_order in {"asc", "desc"} else "desc"
        )
        sort_expr = getattr(sort_column, sort_order)
        stmt = stmt.order_by(sort_expr())

    return stmt


# 시술 예약 목록 조회
def get_treatment_list(
    db: Session,
    shop_id: int,
    filters: TreatmentFilter,
) -> Page[Treatment]:
    """시술 예약 목록 조회.

    :param db: 데이터베이스 세션
    :param shop_id: 샵 ID
    :param filters: 필터링 조건
    :return: 페이지네이션된 Treatment 객체 목록
    """
    # 쿼리 생성
    stmt = stmt_treatment_list(shop_id, filters)
    # 실행 및 페이지네이션
    return paginate(db, stmt)


# @MX:NOTE: [AUTO] 테넌트 경계 — 시술 항목이 참조할 상세는 현재 샵 메뉴 소속뿐이다.
#            id 단독 조회는 타 샵 상세 참조(교차 테넌트 FK)를 허용했으므로
#            TreatmentMenu 조인으로 shop_id 를 항상 강제한다. 호출부는 삭제 상태
#            검사만 끌 수 있다(이미 참조 중인 상세 유지).
def validate_menu_detail_exists(
    db: Session,
    menu_detail_id: int,
    shop_id: int,
    *,
    exclude_deleted: bool = True,
) -> TreatmentMenuDetail | None:
    """현재 샵 메뉴에 속한 시술 상세를 조회한다(없거나 타 샵 소속이면 None).

    :param exclude_deleted: True 면 상세와 상위 메뉴가 모두 소프트삭제되지 않은 경우만
        반환한다(신규 참조). 예약이 이미 참조 중인 상세를 유지할 때는 False 로 호출해
        샵 소유권만 검증한다.
    """
    query = (
        db.query(TreatmentMenuDetail)
        .join(TreatmentMenu, TreatmentMenuDetail.menu_id == TreatmentMenu.id)
        .filter(
            TreatmentMenuDetail.id == menu_detail_id,
            TreatmentMenu.shop_id == shop_id,
        )
    )
    if exclude_deleted:
        query = query.filter(
            TreatmentMenuDetail.deleted_at.is_(None),
            TreatmentMenu.deleted_at.is_(None),
        )
    return query.first()


def get_treatment_items_by_treatment_id(
    db: Session,
    treatment_id: int,
) -> list[TreatmentItem]:
    return (
        db.query(TreatmentItem).filter(TreatmentItem.treatment_id == treatment_id).all()
    )


def get_treatments_to_autocomplete(
    db: Session,
) -> list[TreatmentAutoComplete]:
    return (
        db.query(
            Treatment.id.label("treatment_id"),
            Treatment.reserved_at,
            func.sum(TreatmentItem.duration_min).label("total_duration_min"),
        )
        .join(TreatmentItem, TreatmentItem.treatment_id == Treatment.id)
        .filter(
            Treatment.status.in_(TreatmentStatus.unfinished_statuses()),
            Treatment.finished_at.is_(None),
        )
        .group_by(Treatment.id, Treatment.reserved_at)
        .limit(100)
        .all()
    )
