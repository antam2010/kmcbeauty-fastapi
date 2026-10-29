"""SPEC-FIX-001 REQ-FIX-003 — TreatmentMenu.details primaryjoin 교정 테스트.

대상: app/models/treatment_menu.py::TreatmentMenu.details

[D1 진단 결과] 이전의 인접 문자열 리터럴 연결 primaryjoin 은 파이썬이 단일 문자열로
이어붙여 SQLAlchemy 가 정상 파싱했다(깨지지 않았음). 본 SPEC 은 동작 변화 없이
가독성을 위해 callable(lambda) + and_()/.is_(None) 형태로 교체했다.

- 매퍼 구성이 런타임/파싱 오류 없이 성공한다 (AC-003-1)
- primaryjoin 이 deleted_at IS NULL 조건을 포함해 삭제 상세를 제외한다 (AC-003-2)
- cascade="all, delete-orphan", back_populates="menu" 가 유지된다 (AC-003-3)
"""

from sqlalchemy.orm import configure_mappers

# configure_mappers() 는 등록된 모든 매퍼를 구성하므로, 문자열 관계 참조
# ("TreatmentItem", "Shop", "User" 등)가 해석되도록 app.models 전체를 로드한다.
import app.models  # noqa: F401
from app.models.treatment_menu import TreatmentMenu


def test_mappers_configure_without_error() -> None:
    """AC-003-1: 매퍼 구성이 오류 없이 성공한다(깨진 primaryjoin 이면 여기서 실패)."""
    configure_mappers()
    rel = TreatmentMenu.__mapper__.relationships["details"]
    assert rel is not None


def test_primaryjoin_excludes_soft_deleted_details() -> None:
    """AC-003-2: primaryjoin 이 deleted_at IS NULL 조건을 포함한다."""
    configure_mappers()
    rel = TreatmentMenu.__mapper__.relationships["details"]
    primaryjoin_sql = str(rel.primaryjoin).upper()
    assert "DELETED_AT IS NULL" in primaryjoin_sql
    assert "MENU_ID" in primaryjoin_sql


def test_relationship_options_preserved() -> None:
    """AC-003-3: cascade / back_populates 설정이 유지된다."""
    configure_mappers()
    rel = TreatmentMenu.__mapper__.relationships["details"]
    assert rel.back_populates == "menu"
    # delete-orphan cascade 유지 확인
    assert rel.cascade.delete_orphan is True
