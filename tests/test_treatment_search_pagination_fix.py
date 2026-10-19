"""시술 예약 목록 검색·페이지네이션 회귀 테스트.

대상: app/crud/treatment_crud.py::stmt_treatment_list / get_treatment_list

결함:
- 검색어가 있으면 treatment_items 와 menu_detail 을 OUTER JOIN 했지만 조건에는 쓰지
  않았다. 일대다 조인으로 예약 행이 항목 수만큼 늘어 total 이 부풀었다. 컬렉션
  joinedload + LIMIT 조합에서는 LIMIT 가 중복 행에 적용되어 페이지에서 예약이 빠지거나
  다음 페이지에 중복으로 나왔다. 앱(BookingListScreen)은 items.length === size 로 다음
  페이지 유무를 판정하므로 무한 스크롤이 끊기거나 같은 예약을 두 번 보여준다.
- 스키마 계약(TreatmentFilter.search: "예약자 이름, 전화번호, 시술 항목")과 달리 시술
  항목명은 검색되지 않았다.
- 정렬 키가 같은 예약(같은 reserved_at)의 순서는 DB 가 보장하지 않는다. 그래서 OFFSET
  페이지끼리 겹치거나 빠질 수 있었다.

요구 동작: 검색 조건은 EXISTS(has/any)로 표현해 행을 늘리지 않는다. 정렬에는
Treatment.id 를 같은 방향의 2차 키로 둔다.

tests/fake_sql.py 평가기로 실제 문과 fastapi-pagination 의 count/LIMIT 경로를 DB
없이 실행한다.
"""

import re
from datetime import datetime

import pytest
from fastapi_pagination import Page, Params, set_params
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.crud import treatment_crud
from app.enum.treatment_status import TreatmentStatus
from app.models.phonebook import Phonebook
from app.models.treatment import Treatment
from app.models.treatment_item import TreatmentItem
from app.models.treatment_menu_detail import TreatmentMenuDetail
from app.schemas.treatment import TreatmentFilter
from tests.fake_sql import Context, FakeSQLSession

SHOP, OTHER_SHOP = 10, 20
RESERVED_AT = datetime(2026, 10, 15, 1, 0)  # noqa: DTZ001  # naive UTC 컬럼 규약


def _treatment(treatment_id: int, shop_id: int = SHOP, **fields: object) -> Treatment:
    return Treatment(
        id=treatment_id,
        shop_id=shop_id,
        reserved_at=RESERVED_AT,
        status=TreatmentStatus.RESERVED,
        **fields,
    )


def _item(item_id: int, treatment_id: int, menu_detail_id: int) -> TreatmentItem:
    return TreatmentItem(
        id=item_id,
        treatment_id=treatment_id,
        menu_detail_id=menu_detail_id,
        base_price=10000,
        duration_min=30,
        session_no=1,
    )


def _detail(detail_id: int, name: str) -> TreatmentMenuDetail:
    return TreatmentMenuDetail(
        id=detail_id,
        menu_id=detail_id // 100,
        name=name,
        duration_min=30,
        base_price=10000,
    )


def _world() -> FakeSQLSession:
    return FakeSQLSession(
        Phonebook(id=100, shop_id=SHOP, name="김민지", phone_number="01011112222"),
        Phonebook(
            id=200,
            shop_id=OTHER_SHOP,
            name="김민지",
            phone_number="01099998888",
        ),
        _detail(1100, "젤네일"),
        _detail(1101, "속눈썹펌"),
        _detail(1102, "패디큐어"),
        _detail(2100, "속눈썹펌"),  # 타 상점 메뉴
        # 예약 1: 전화번호부 고객, 시술 항목 3개
        _treatment(1, phonebook_id=100),
        _item(11, 1, 1100),
        _item(12, 1, 1101),
        _item(13, 1, 1102),
        # 예약 2/3: 직접 입력 고객
        _treatment(2, customer_name="박서준", customer_phone="01033334444"),
        _item(21, 2, 1100),
        _treatment(3, customer_name="이도윤"),
        _item(31, 3, 1101),
        # 타 상점 예약(같은 고객명·항목명): 어떤 검색에도 나오면 안 된다.
        _treatment(9, shop_id=OTHER_SHOP, phonebook_id=200),
        _item(91, 9, 2100),
    )


def _list(
    db: FakeSQLSession,
    *,
    page: int = 1,
    size: int = 20,
    **filters: object,
) -> Page[Treatment]:
    with set_params(Params(page=page, size=size)):
        return treatment_crud.get_treatment_list(db, SHOP, TreatmentFilter(**filters))


def _ids(page: Page[Treatment]) -> list[int]:
    return [treatment.id for treatment in page.items]


def test_search_counts_treatment_once_even_with_many_items() -> None:
    """항목 3개짜리 예약이 검색되어도 total 은 1이다(항목 수만큼 부풀지 않음)."""
    page = _list(_world(), search="김민지")

    assert page.total == 1
    assert _ids(page) == [1]


def test_search_matches_menu_detail_name() -> None:
    """시술 항목명(메뉴 상세명)으로도 검색된다(스키마 계약). 타 상점은 제외."""
    page = _list(_world(), search="속눈썹")

    assert page.total == 2
    assert sorted(_ids(page)) == [1, 3]


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [("민지", [1]), ("1111", [1]), ("박서준", [2]), ("3333", [2]), ("도윤", [3])],
    ids=["phonebook-name", "phonebook-phone", "walk-in-name", "walk-in-phone", "name"],
)
def test_search_still_matches_customer_name_and_phone(
    keyword: str,
    expected: list[int],
) -> None:
    """기존 검색 대상(전화번호부/직접 입력 고객의 이름·전화번호)은 그대로 검색된다."""
    page = _list(_world(), search=keyword)

    assert page.total == len(expected)
    assert _ids(page) == expected


def test_paginated_inner_query_does_not_join_items() -> None:
    """LIMIT 가 걸리는 안쪽 쿼리는 예약 1건당 1행이다(일대다 JOIN 없음).

    시술 항목·고객 조건은 상관 EXISTS 로만 들어가고, 화면용 eager load JOIN 은
    LIMIT 바깥에 그대로 남는다(N+1 방지 유지).
    """
    filters = TreatmentFilter(search="김민지")
    stmt = treatment_crud.stmt_treatment_list(SHOP, filters).limit(10).offset(0)
    sql = str(stmt.compile(dialect=postgresql.dialect()))

    inner = re.search(r"FROM \((SELECT .*?)\) AS anon_1", sql, re.DOTALL)
    assert inner is not None, sql
    assert "JOIN" not in inner.group(1), inner.group(1)
    assert "EXISTS (SELECT 1 \nFROM treatment_item" in inner.group(1)
    assert "LEFT OUTER JOIN treatment_item AS treatment_item_1" in sql


def _rotating_tie_order(execution: int, contexts: list[Context]) -> list[Context]:
    """실행마다 물리 순서가 달라지는 엔진. SQL 은 동률 행의 순서를 보장하지 않는다."""
    if not contexts:
        return contexts
    shift = execution % len(contexts)
    return contexts[shift:] + contexts[:shift]


@pytest.mark.parametrize("sort_order", ["desc", "asc"])
def test_tied_reserved_at_pages_cover_every_row_once(sort_order: str) -> None:
    """reserved_at 이 모두 같은 25건을 10건씩 넘겨도 누락·중복 없이 25건이다."""
    db = FakeSQLSession(
        *(_treatment(treatment_id) for treatment_id in range(1, 26)),
        tie_order=_rotating_tie_order,
    )

    pages = [
        _list(db, page=page, size=10, sort_by="reserved_at", sort_order=sort_order)
        for page in (1, 2, 3)
    ]
    seen = [treatment_id for page in pages for treatment_id in _ids(page)]

    assert [page.total for page in pages] == [25, 25, 25]
    assert len(seen) == 25
    assert sorted(seen) == list(range(1, 26))


@pytest.mark.parametrize("sort_order", ["asc", "desc"])
def test_sort_adds_id_tie_breaker_in_same_direction(sort_order: str) -> None:
    """정렬 키 뒤에 같은 방향의 treatment.id 가 2차 키로 붙는다."""
    filters = TreatmentFilter(sort_by="reserved_at", sort_order=sort_order)
    stmt = treatment_crud.stmt_treatment_list(SHOP, filters)
    sql = str(stmt.compile(dialect=postgresql.dialect()))

    direction = sort_order.upper()
    assert (
        f"ORDER BY treatment.reserved_at {direction}, treatment.id {direction}" in sql
    )
