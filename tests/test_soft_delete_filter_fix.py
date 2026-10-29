"""SPEC-FIX-001 REQ-FIX-001 — 소프트삭제 필터 누수 교정 테스트.

대상:
- app/crud/user_crud.py::get_user_by_email, get_user_by_id (로그인/재발급 조회 경로)
- app/crud/shop_crud.py::get_user_shops, get_user_shop_by_id (샵 목록/단건)

deleted_at IS NULL 필터가 조회 쿼리에 포함되는지 SQL 컴파일로 관찰한다.
DB 없이 db.query().filter() 체인을 기록하는 스파이 세션을 사용한다(기존 fake 패턴).

또한 소프트삭제 미적용 모델(Treatment/TreatmentItem)에 필터가 오적용되지 않았음을
정적으로 확인한다(AC-001-5).
"""

import pytest

from app.crud import shop_crud, user_crud
from app.models.shop import Shop
from app.models.user import User


class SpyQuery:
    """filter 인자를 누적 기록하고, 컴파일 가능한 형태로 노출하는 스파이 Query."""

    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    def filter(self, *criteria: object) -> "SpyQuery":
        self._recorder.extend(criteria)
        return self

    def order_by(self, *_args: object) -> "SpyQuery":
        return self

    def first(self) -> None:
        return None


class SpySession:
    def __init__(self) -> None:
        self.criteria: list = []

    def query(self, *_models: object) -> SpyQuery:
        return SpyQuery(self.criteria)


def _has_deleted_at_is_null(criteria: list, model: type) -> bool:
    """기록된 필터 표현식 중 <model>.deleted_at IS NULL 이 있는지 SQL 로 확인."""
    for c in criteria:
        try:
            sql = str(c.compile(compile_kwargs={"literal_binds": True}))
        except Exception:  # noqa: BLE001, S112 - 컴파일 불가한 표현식은 무시하고 계속
            continue
        if "deleted_at IS NULL" in sql and model.__tablename__ in sql:
            return True
    return False


def test_get_user_by_email_filters_soft_deleted() -> None:
    """AC-001-1: 로그인 조회 경로가 deleted_at IS NULL 로 삭제 유저를 제외한다."""
    db = SpySession()
    user_crud.get_user_by_email(db, "someone@test.com")
    assert _has_deleted_at_is_null(db.criteria, User)


def test_get_user_by_id_filters_soft_deleted() -> None:
    """AC-001-6: 재발급/활성 조회 경로도 deleted_at IS NULL 로 삭제 유저를 제외한다."""
    db = SpySession()
    user_crud.get_user_by_id(db, 1)
    assert _has_deleted_at_is_null(db.criteria, User)


def test_get_user_shop_by_id_filters_soft_deleted() -> None:
    """AC-001-3: 샵 단건 조회가 deleted_at IS NULL 필터를 포함한다."""
    db = SpySession()
    shop_crud.get_user_shop_by_id(db, user_id=1, shop_id=2)
    assert _has_deleted_at_is_null(db.criteria, Shop)


def test_get_user_shops_filters_soft_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-001-2: 샵 목록 조회가 deleted_at IS NULL 필터를 포함한다.

    get_user_shops 는 filter 체인을 구성한 뒤 fastapi_pagination.paginate 로 넘긴다.
    검증 대상은 filter 인자(deleted_at IS NULL)이며, 이는 paginate 호출 이전에 이미
    SpyQuery 에 기록된다. paginate 는 실제 Query/Select 를 요구하고 fake SpyQuery 에는
    관여하지 않는 pagination 내부 로직이므로(버전별 시그니처 디스패치 포함) 여기서는
    stub 하여 CRUD 필터 계약만 관찰한다(테스트 의도 보존).
    """
    monkeypatch.setattr(shop_crud, "paginate", lambda query: query)
    db = SpySession()
    shop_crud.get_user_shops(db, user_id=1)
    assert _has_deleted_at_is_null(db.criteria, Shop)


def test_treatment_models_have_no_deleted_at_column() -> None:
    """AC-001-5: 소프트삭제 미적용 모델에는 deleted_at 컬럼이 없어 오적용을 방지한다."""
    from app.models.treatment import Treatment
    from app.models.treatment_item import TreatmentItem

    assert "deleted_at" not in Treatment.__table__.columns
    assert "deleted_at" not in TreatmentItem.__table__.columns
