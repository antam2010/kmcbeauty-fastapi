from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy import ColumnElement, and_
from sqlalchemy.orm import Session

from app.models.shop import Shop
from app.models.shop_user import ShopUser
from app.schemas.shop import ShopCreate

# 소유권 미검증 헬퍼 get_shop_by_id 는 호출부 0건의 미사용 코드였으며,
# 향후 shop-scoped 접근에서 소유권 없는 조회 경로로 오용될 위험이 있어 제거했다
# (SPEC-SECURITY-001 REQ-SEC-003, 죽은 코드 심층방어 하드닝).
# 샵 범위 접근은 두 가지 검증 헬퍼만 쓴다.
# - 샵 데이터 접근(선택·목록·shop-scoped API): 멤버십을 검증하는
#   get_member_shop_by_id / get_member_shops
# - 샵 관리(샵 수정): 생성자(Shop.user_id)를 검증하는 get_user_shop_by_id


# @MX:NOTE: [AUTO] 활성 샵 조회 불변식 — 소프트삭제된 샵(deleted_at IS NOT NULL)은
#            목록/단건 조회에서 제외한다. (SPEC-FIX-001 REQ-FIX-001)


def _member_join(user_id: int) -> ColumnElement[bool]:
    """shop_user 를 (shop_id, user_id) 로 묶는 조인 조건.

    uq_shop_user(shop_id, user_id) 덕분에 샵당 최대 한 행만 붙어 목록이 중복되지 않는다.
    """
    return and_(ShopUser.shop_id == Shop.id, ShopUser.user_id == user_id)


# @MX:ANCHOR: [AUTO] 샵 데이터 접근의 멤버십 판정 단일 지점
# @MX:REASON: get_current_shop(모든 shop-scoped API), 샵 선택, 선택 샵 조회가 의존한다.
#             shop_user 멤버(MASTER/MANAGER 무관)인 활성 샵만 반환해야 한다. 생성자
#             여부로 판정하면 초대 직원이 샵을 못 쓰고, 멤버십 해제가 접근을 회수하지
#             못한다.
def get_member_shop_by_id(db: Session, user_id: int, shop_id: int) -> Shop | None:
    """유저가 멤버(shop_user)로 속한 특정 샵 조회 (소프트삭제 제외)."""
    return (
        db.query(Shop)
        .join(ShopUser, _member_join(user_id))
        .filter(Shop.id == shop_id, Shop.deleted_at.is_(None))
        .first()
    )


# 유저가 멤버(shop_user)로 속한 모든 샵 조회 (소프트삭제 제외)
def get_member_shops(db: Session, user_id: int) -> Page[Shop]:
    query = (
        db.query(Shop)
        .join(ShopUser, _member_join(user_id))
        .filter(Shop.deleted_at.is_(None))
        .order_by(Shop.id.desc())
    )
    return paginate(query)


# 유저가 생성(소유)한 특정 샵 조회 (소프트삭제 제외). 샵 수정 등 관리 경로 전용.
def get_user_shop_by_id(db: Session, user_id: int, shop_id: int) -> Shop | None:
    return (
        db.query(Shop)
        .filter(
            Shop.id == shop_id,
            Shop.user_id == user_id,
            Shop.deleted_at.is_(None),
        )
        .first()
    )


# 유저가 생성(소유)한 모든 샵 조회 (소프트삭제 제외).
# GET /shops 는 멤버십 기준 get_member_shops 를 쓴다(현재 호출부 없음).
def get_user_shops(db: Session, user_id: int) -> Page[Shop]:
    query = (
        db.query(Shop)
        .filter(Shop.user_id == user_id, Shop.deleted_at.is_(None))
        .order_by(Shop.id.desc())
    )
    return paginate(query)


# 샵 생성
def create_shop(db: Session, shop_data: ShopCreate, user_id: int) -> Shop:
    shop = Shop(**shop_data.model_dump())
    shop.user_id = user_id
    db.add(shop)
    db.flush()
    return shop
