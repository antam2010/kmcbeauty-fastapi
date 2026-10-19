import logging
from datetime import UTC, datetime

from fastapi_pagination import Page
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.core.limits import PHONEBOOK_GROUP_ITEMS_MAX
from app.models.phonebook import Phonebook
from app.schemas.phonebook import PhonebookCreate, PhonebookUpdate

logger = logging.getLogger(__name__)


# 전화번호부 리스트 조회
def get_phonebooks_by_user(
    db: Session,
    shop_id: int,
    search: str | None = None,
) -> Page[Phonebook]:
    query = db.query(Phonebook).filter(
        Phonebook.shop_id == shop_id,
        Phonebook.deleted_at.is_(None),
    )

    if search:
        keyword = f"%{search}%"
        search_filter = or_(
            Phonebook.name.ilike(keyword),
            Phonebook.phone_number.ilike(keyword),
            Phonebook.group_name.ilike(keyword),
            Phonebook.memo.ilike(keyword),
        )
        query = query.filter(and_(search_filter, Phonebook.deleted_at.is_(None)))
    else:
        query = query.filter(Phonebook.deleted_at.is_(None))

    # SQLAlchemy 쿼리는 불변(immutable)이므로 order_by 결과를 반드시 재할당해야
    # 정렬이 최종 실행 쿼리에 반영된다. (SPEC-FIX-001 REQ-FIX-002)
    # id 내림차순은 그 자체로 결정적(deterministic) 순서를 보장한다.
    query = query.order_by(Phonebook.id.desc())

    return paginate(query)


# 전화번호부 상세 조회
def get_phonebook_by_id(
    db: Session,
    phonebook_id: int,
    shop_id: int,
) -> Phonebook | None:
    return (
        db.query(Phonebook)
        .filter(
            Phonebook.id == phonebook_id,
            Phonebook.shop_id == shop_id,
            Phonebook.deleted_at.is_(None),
        )
        .first()
    )


# 전화번호부 생성
def create_phonebook(db: Session, data: PhonebookCreate, shop_id: int) -> Phonebook:
    item = Phonebook(**data.model_dump(), shop_id=shop_id)
    db.add(item)
    return item


# 전화번호부 수정
def update_phonebook(
    _db: Session,
    phonebook: Phonebook,
    data: PhonebookUpdate,
) -> Phonebook:
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(phonebook, key, value)
    return phonebook


# 전화번호부 중복 체크
def get_phonebook_by_phone_number(
    db: Session,
    phone_number: str,
    shop_id: int,
) -> Phonebook | None:
    return (
        db.query(Phonebook)
        .filter(
            Phonebook.phone_number == phone_number,
            Phonebook.shop_id == shop_id,
            Phonebook.deleted_at.is_(None),
        )
        .first()
    )


# 전화번호부 삭제
def delete_phonebook(_db: Session, phonebook: Phonebook, shop_id: int) -> Phonebook:
    phonebook.deleted_at = datetime.now(UTC)
    phonebook.shop_id = shop_id
    return phonebook


# 전화번호부 그룹별 개수 조회
def get_group_counts_by_groupname(db: Session, shop_id: int) -> dict[str, int]:
    return (
        db.query(Phonebook.group_name, func.count(Phonebook.id))
        .filter(
            Phonebook.shop_id == shop_id,
            Phonebook.deleted_at.is_(None),
        )
        .group_by(Phonebook.group_name)
        .all()
    )


# 전화번호부 가게별 전체 조회
def get_all_phonebooks_by_shop(db: Session, shop_id: int) -> list[Phonebook]:
    # SPEC-PERF-001 REQ-PERF-004: 페이지네이션 없는 언바운드 조회이므로 방어적
    # 상한을 둔다. 응답 shape(list[...])는 유지한다. 상한에 도달하면 절단
    # 가능성을 경고 로그로 남긴다(운영 관측용).
    items = (
        db.query(Phonebook)
        .filter(
            Phonebook.shop_id == shop_id,
            Phonebook.deleted_at.is_(None),
        )
        .limit(PHONEBOOK_GROUP_ITEMS_MAX)
        .all()
    )
    if len(items) >= PHONEBOOK_GROUP_ITEMS_MAX:
        logger.warning(
            "get_all_phonebooks_by_shop hit row cap (%d) for shop_id=%s; "
            "result may be truncated",
            PHONEBOOK_GROUP_ITEMS_MAX,
            shop_id,
        )
    return items
