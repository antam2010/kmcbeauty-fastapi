"""SPEC-FIX-001 REQ-FIX-005 — sort_order 화이트리스트 검증 테스트.

대상: app/crud/treatment_crud.py::stmt_treatment_list
- 조작된 sort_order 값으로 크래시(None 호출)하지 않고 기본 desc 로 안전 대체 (AC-005-1)
- 유효 sort_order(asc/desc) 는 지정 방향으로 정렬 (AC-005-2)

stmt_treatment_list 는 순수 함수(DB 불필요)이므로 SQLAlchemy Core select 를
직접 컴파일하여 ORDER BY 절을 관찰한다.
"""

from app.crud.treatment_crud import stmt_treatment_list
from app.schemas.treatment import TreatmentFilter


def _order_by_sql(sort_by: str, sort_order: str) -> str:
    filters = TreatmentFilter(sort_by=sort_by, sort_order=sort_order)
    stmt = stmt_treatment_list(shop_id=1, filters=filters)
    return str(stmt.compile(compile_kwargs={"literal_binds": False})).upper()


def test_invalid_sort_order_does_not_crash_and_defaults_desc() -> None:
    """AC-005-1: 조작된 sort_order 는 크래시 없이 기본 desc 로 대체된다."""
    # 이전 구현은 getattr(col, "drop", None)() 로 None 호출 → TypeError(500).
    sql = _order_by_sql("reserved_at", "drop")
    assert "ORDER BY" in sql
    assert "DESC" in sql


def test_valid_sort_order_asc_is_applied() -> None:
    """AC-005-2: 유효한 asc 는 오름차순으로 반영된다."""
    sql = _order_by_sql("reserved_at", "asc")
    assert "ORDER BY" in sql
    assert "ASC" in sql


def test_valid_sort_order_desc_is_applied() -> None:
    """AC-005-2(보강): 유효한 desc 는 내림차순으로 반영된다."""
    sql = _order_by_sql("reserved_at", "desc")
    assert "ORDER BY" in sql
    assert "DESC" in sql


def test_unknown_sort_by_skips_order_by() -> None:
    """엣지: sort_by 가 모델 속성이 아니면 정렬을 생략한다(기존 동작 유지)."""
    sql = _order_by_sql("nonexistent_column", "asc")
    assert "ORDER BY" not in sql
