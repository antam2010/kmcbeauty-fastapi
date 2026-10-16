import logging

from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.core.limits import MENU_DETAILS_MAX
from app.models.treatment_menu import TreatmentMenu
from app.models.treatment_menu_detail import TreatmentMenuDetail

logger = logging.getLogger(__name__)


def create_treatment_menu(
    db: Session,  # noqa: ARG001  # 호출부가 db= 키워드로 전달하므로 시그니처를 유지한다
    name: str,
    shop_id: int,
) -> TreatmentMenu:
    return TreatmentMenu(
        name=name,
        shop_id=shop_id,
    )


def create_treatment_menu_detail(
    db: Session,
    menu_id: int,
    name: str,
    duration_min: int,
    base_price: int,
) -> TreatmentMenuDetail:
    # 트랜잭션 경계는 서비스 계층이 소유한다(SPEC-FIX-001 REQ-FIX-005).
    # CRUD 는 인스턴스 생성 + 영속화(add/flush)까지만 수행하고 commit 은 하지 않는다.
    # 커밋/refresh 는 호출부
    # (treatment_menu_service.create_treatment_menu_detail_service)가 담당한다.
    detail = TreatmentMenuDetail(
        menu_id=menu_id,
        name=name,
        duration_min=duration_min,
        base_price=base_price,
    )
    db.add(detail)
    db.flush()
    return detail


def get_treatment_menus_by_user(
    db: Session,
    shop_id: int,
    search: str | None = None,
) -> Page[TreatmentMenu]:
    query = (
        db.query(TreatmentMenu)
        .options(joinedload(TreatmentMenu.details))
        .filter(
            TreatmentMenu.shop_id == shop_id,
            TreatmentMenu.deleted_at.is_(None),
        )
    )

    if search:
        query = query.filter(
            or_(
                TreatmentMenu.name.ilike(f"%{search}%"),
                TreatmentMenu.details.any(
                    TreatmentMenuDetail.name.ilike(f"%{search}%"),
                ),
            ),
        )

    # SQLAlchemy 쿼리는 불변(immutable)이므로 order_by 결과를 반드시 재할당해야
    # 정렬이 최종 실행 쿼리에 반영된다. (SPEC-FIX-001 REQ-FIX-002)
    # id 내림차순은 그 자체로 결정적(deterministic) 순서를 보장한다.
    query = query.order_by(TreatmentMenu.id.desc())

    return paginate(query)


def get_treatment_menu_details_by_user(
    db: Session,
    menu_id: int,
    shop_id: int,
) -> list[TreatmentMenuDetail]:
    # SPEC-PERF-001 REQ-PERF-004: 언바운드 조회 방어 상한. 구조적으로 소규모
    # (메뉴당 상세 수)지만 코드상 상한이 없으므로 방어 캡을 둔다. 응답 shape 는
    # 유지한다. 상한 도달 시 절단 가능성을 경고 로그로 남긴다.
    details = (
        db.query(TreatmentMenuDetail)
        .join(TreatmentMenu, TreatmentMenuDetail.menu_id == TreatmentMenu.id)
        .filter(
            TreatmentMenuDetail.menu_id == menu_id,
            TreatmentMenu.shop_id == shop_id,
            TreatmentMenuDetail.deleted_at.is_(None),
        )
        .order_by(TreatmentMenuDetail.id.desc())
        .limit(MENU_DETAILS_MAX)
        .all()
    )
    if len(details) >= MENU_DETAILS_MAX:
        logger.warning(
            "get_treatment_menu_details_by_user hit row cap (%d) for "
            "menu_id=%s shop_id=%s; result may be truncated",
            MENU_DETAILS_MAX,
            menu_id,
            shop_id,
        )
    return details


def get_menu_by_id(
    db: Session,
    menu_id: int,
    shop_id: int,
    exclude_deleted: bool = True,
) -> TreatmentMenu | None:
    query = db.query(TreatmentMenu).filter(
        TreatmentMenu.id == menu_id,
        TreatmentMenu.shop_id == shop_id,
    )
    if exclude_deleted:
        query = query.filter(TreatmentMenu.deleted_at.is_(None))

    return query.first()


# @MX:NOTE: [AUTO] 테넌트 경계 — 상세 수정/삭제 대상은 현재 샵 메뉴 소속뿐이다.
#            이전에는 shop_id 를 받고도 무시해 타 샵 상세를 수정/삭제할 수 있었다.
#            읽기 경로(get_treatment_menu_details_by_user)와 같은 조인/삭제 필터를
#            써서 읽기와 쓰기의 대상 집합을 일치시킨다.
def get_menu_detail_by_id(
    db: Session,
    menu_id: int,
    detail_id: int,
    shop_id: int,
) -> TreatmentMenuDetail | None:
    return (
        db.query(TreatmentMenuDetail)
        .join(TreatmentMenu, TreatmentMenuDetail.menu_id == TreatmentMenu.id)
        .filter(
            TreatmentMenuDetail.id == detail_id,
            TreatmentMenuDetail.menu_id == menu_id,
            TreatmentMenu.shop_id == shop_id,
            TreatmentMenuDetail.deleted_at.is_(None),
        )
        .first()
    )
