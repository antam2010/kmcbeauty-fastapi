"""REQ-SEC-003 죽은 코드 하드닝 테스트.

대상: app/crud/shop_crud.py
- 소유권 미검증 헬퍼 get_shop_by_id 가 제거되어 오용 경로가 존재하지 않음 (AC-003-4)
- 소유권 검증 헬퍼 get_user_shop_by_id 는 유지됨
"""

from app.crud import shop_crud


def test_get_shop_by_id_removed() -> None:
    """AC-003-4: 소유권 미검증 get_shop_by_id 는 제거되었다."""
    assert not hasattr(shop_crud, "get_shop_by_id")


def test_ownership_checked_helper_present() -> None:
    """소유권 검증형 헬퍼는 유지된다."""
    assert hasattr(shop_crud, "get_user_shop_by_id")
