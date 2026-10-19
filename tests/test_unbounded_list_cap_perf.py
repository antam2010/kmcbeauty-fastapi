"""SPEC-PERF-001 REQ-PERF-004 — 언바운드 list[...] 엔드포인트 조회 캡 테스트.

대상 CRUD(페이지네이션 없는 list[...] 응답 경로):
- app/crud/phonebook_crud.py::get_all_phonebooks_by_shop
  (GET /phonebooks/groups?with_items=true)
- app/crud/shop_user_crud.py::get_shop_users_by_shop_id
  (GET /shops/{shop_id}/users)
- app/crud/treatment_menu_crud.py::get_treatment_menu_details_by_user
  (GET /treatment-menus/{menu_id}/details)
- app/crud/device_push_token_crud.py::get_device_tokens_by_user
  (GET /device-tokens/me)

측정 가능한 AC: 각 조회가 방어적 .limit(<상한>) 을 적용하여 단일 쿼리로 상한을
초과하는 행을 반환할 수 없음을 관찰한다. 또한 반환 행 수가 상한과 같으면
절단 가능성 경고 로그를 남김을 확인한다.

DB 없이 filter/join/options/order_by/limit 체인을 기록하는 스파이 세션을 사용한다
(기존 fake/spy 패턴 재사용). 응답 shape(list[...])는 변경하지 않으므로 계약(API-001)이
보존된다.
"""

import logging

import pytest

from app.core.limits import (
    MENU_DETAILS_MAX,
    PHONEBOOK_GROUP_ITEMS_MAX,
    SHOP_USERS_MAX,
    USER_DEVICE_TOKENS_MAX,
)
from app.crud import (
    device_push_token_crud,
    phonebook_crud,
    shop_user_crud,
    treatment_menu_crud,
)


class SpyQuery:
    """filter/join/options/order_by/limit 호출을 기록하고, all() 로 고정 행을 반환.

    rows_to_return 는 .all() 호출 시 반환할 리스트다. 캡(.limit(N)) 이 SQL 수준에서
    적용되는지를 관찰하기 위한 것이므로, 실제 잘라내기(slicing)는 하지 않고 CRUD 가
    limit 값을 올바르게 전달했는지만 기록한다.
    """

    def __init__(self, recorder: dict, rows_to_return: list) -> None:
        self._recorder = recorder
        self._rows = rows_to_return

    def filter(self, *_criteria: object) -> "SpyQuery":
        return self

    def join(self, *_args: object, **_kwargs: object) -> "SpyQuery":
        return self

    def options(self, *_args: object) -> "SpyQuery":
        self._recorder["options_called"] = True
        return self

    def order_by(self, *_args: object) -> "SpyQuery":
        return self

    def limit(self, value: int) -> "SpyQuery":
        self._recorder["limit"] = value
        return self

    def all(self) -> list:
        return self._rows


class SpySession:
    def __init__(self, rows_to_return: list) -> None:
        self.recorder: dict = {}
        self._rows = rows_to_return

    def query(self, *_models: object) -> SpyQuery:
        return SpyQuery(self.recorder, self._rows)


# ---------------------------------------------------------------------------
# 각 언바운드 조회가 정확한 상한값으로 .limit() 을 적용하는지 (AC-004-3 FIX)
# ---------------------------------------------------------------------------


def test_get_all_phonebooks_by_shop_applies_cap() -> None:
    """전화번호부 전체 조회가 PHONEBOOK_GROUP_ITEMS_MAX 상한을 적용한다."""
    db = SpySession(rows_to_return=[])
    phonebook_crud.get_all_phonebooks_by_shop(db, shop_id=1)
    assert db.recorder["limit"] == PHONEBOOK_GROUP_ITEMS_MAX


def test_get_shop_users_by_shop_id_applies_cap() -> None:
    """샵 유저 목록 조회가 SHOP_USERS_MAX 상한을 적용한다."""
    db = SpySession(rows_to_return=[])
    shop_user_crud.get_shop_users_by_shop_id(db, shop_id=1)
    assert db.recorder["limit"] == SHOP_USERS_MAX


def test_get_treatment_menu_details_applies_cap() -> None:
    """메뉴 상세 목록 조회가 MENU_DETAILS_MAX 상한을 적용한다."""
    db = SpySession(rows_to_return=[])
    treatment_menu_crud.get_treatment_menu_details_by_user(
        db,
        menu_id=1,
        shop_id=1,
    )
    assert db.recorder["limit"] == MENU_DETAILS_MAX


def test_get_device_tokens_by_user_applies_cap() -> None:
    """내 디바이스 토큰 목록 조회가 USER_DEVICE_TOKENS_MAX 상한을 적용한다."""
    db = SpySession(rows_to_return=[])
    device_push_token_crud.get_device_tokens_by_user(db, user_id=1)
    assert db.recorder["limit"] == USER_DEVICE_TOKENS_MAX


# ---------------------------------------------------------------------------
# 상한 미만이면 경고 로그가 없음 (정상 경로)
# ---------------------------------------------------------------------------


def test_no_warning_below_cap(caplog: pytest.LogCaptureFixture) -> None:
    """반환 행 수가 상한 미만이면 절단 경고를 남기지 않는다."""
    db = SpySession(rows_to_return=[object(), object()])  # 2 rows << cap
    with caplog.at_level(logging.WARNING):
        device_push_token_crud.get_device_tokens_by_user(db, user_id=1)
    assert "row cap" not in caplog.text


# ---------------------------------------------------------------------------
# 반환 행 수가 상한과 같으면 절단 가능성 경고 로그 (운영 관측)
# ---------------------------------------------------------------------------


def test_warning_emitted_when_cap_hit_phonebook(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """전화번호부 조회가 상한만큼 반환하면 절단 가능성 경고를 남긴다."""
    rows = [object()] * PHONEBOOK_GROUP_ITEMS_MAX
    db = SpySession(rows_to_return=rows)
    with caplog.at_level(logging.WARNING):
        result = phonebook_crud.get_all_phonebooks_by_shop(db, shop_id=1)
    assert len(result) == PHONEBOOK_GROUP_ITEMS_MAX
    assert "row cap" in caplog.text


def test_warning_emitted_when_cap_hit_shop_users(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """샵 유저 조회가 상한만큼 반환하면 절단 가능성 경고를 남긴다."""
    rows = [object()] * SHOP_USERS_MAX
    db = SpySession(rows_to_return=rows)
    with caplog.at_level(logging.WARNING):
        shop_user_crud.get_shop_users_by_shop_id(db, shop_id=1)
    assert "row cap" in caplog.text


def test_warning_emitted_when_cap_hit_menu_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """메뉴 상세 조회가 상한만큼 반환하면 절단 가능성 경고를 남긴다."""
    rows = [object()] * MENU_DETAILS_MAX
    db = SpySession(rows_to_return=rows)
    with caplog.at_level(logging.WARNING):
        treatment_menu_crud.get_treatment_menu_details_by_user(
            db,
            menu_id=1,
            shop_id=1,
        )
    assert "row cap" in caplog.text


def test_warning_emitted_when_cap_hit_device_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """디바이스 토큰 조회가 상한만큼 반환하면 절단 가능성 경고를 남긴다."""
    rows = [object()] * USER_DEVICE_TOKENS_MAX
    db = SpySession(rows_to_return=rows)
    with caplog.at_level(logging.WARNING):
        device_push_token_crud.get_device_tokens_by_user(db, user_id=1)
    assert "row cap" in caplog.text


# ---------------------------------------------------------------------------
# 상한 상수 자체의 sanity (양수이며 무한대가 아님)
# ---------------------------------------------------------------------------


def test_caps_are_positive_finite() -> None:
    """모든 상한은 양의 유한값이어야 한다(언바운드 방지의 핵심)."""
    for cap in (
        PHONEBOOK_GROUP_ITEMS_MAX,
        SHOP_USERS_MAX,
        MENU_DETAILS_MAX,
        USER_DEVICE_TOKENS_MAX,
    ):
        assert isinstance(cap, int)
        assert cap > 0
