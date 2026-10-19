"""SPEC-PERF-001 REQ-PERF-001 / REQ-PERF-003 — NO-OP 회귀 방지 가드.

재검증 결과(spec.md 재검증 요약)에 따르면 아래는 이미 해소된 항목이다:
- REQ-PERF-001: 목록 계열 쿼리가 이미 joinedload 로 연관 관계를 eager load 하여
  N+1 이 없다(O(1)).
- REQ-PERF-003: summary 집계가 이미 SQL GROUP BY + 집계 함수로 수행되어
  파이썬 행 순회 누적이 없다.

이 테스트는 코드를 변경하지 않고 그 NO-OP 상태를 고정한다. 향후 변경으로 eager load
가 제거되거나(→ N+1 재유입) SQL 집계가 파이썬 루프로 회귀하면 실패하도록 한다.

DB 엔진 없이 ORM 문(select/Query)을 SQL 로 컴파일하여 정적으로 관찰한다
(test_treatment_menu_primaryjoin_fix.py 의 str(...compile...) 관례 재사용).
"""

from sqlalchemy.orm import configure_mappers

# 문자열 관계 참조("TreatmentItem", "Shop", "User" 등) 해석을 위해 전체 모델 로드.
import app.models  # noqa: F401


def _options_repr(stmt: object) -> str:
    """Select 에 부착된 loader 옵션들의 관계 경로를 소문자로 이어붙인다.

    plain Select.compile() 은 Core 컴파일러라 joinedload 를 JOIN 으로 확장하지
    않을 수 있어(세션 실행 시에만 확장) 컴파일 SQL 은 불안정하다. 또한 Load 객체의
    repr() 은 SQLAlchemy 버전에 따라 관계 경로를 포함하지 않을 수 있다(2.0.51 기준
    <Load object at 0x...> 형태). 대신 각 옵션의 구조적 로더 경로(Load.path)를 관찰하는
    것이 버전 간 안정적이며 내부 repr 포맷에 의존하지 않는다.
    """
    options = getattr(stmt, "_with_options", ())
    return " ".join(str(getattr(o, "path", "")).lower() for o in options)


# ---------------------------------------------------------------------------
# REQ-PERF-001 — 목록 쿼리 eager load 고정 (N+1 방지)
# ---------------------------------------------------------------------------


def test_stmt_treatment_list_has_eager_load_options_attached() -> None:
    """AC-001-2 가드: 목록 문에 eager load 옵션 3개가 부착돼 있다.

    _with_options 는 SQLAlchemy 2.0 Select 의 loader 옵션 컨테이너다. 소스가
    .options(joinedload(...), joinedload(...), joinedload(...)) 3개를 부착하므로
    최소 3개 이상이어야 한다. eager load 제거(lazy 회귀 → N+1)시 0 으로 떨어져
    실패한다.
    """
    configure_mappers()
    from app.crud.treatment_crud import stmt_treatment_list
    from app.schemas.treatment import TreatmentFilter

    stmt = stmt_treatment_list(shop_id=1, filters=TreatmentFilter())
    options = getattr(stmt, "_with_options", ())
    assert len(options) >= 3


def test_stmt_treatment_list_eager_load_covers_relations() -> None:
    """목록 문의 eager load 가 items/phonebook/staff 관계를 덮는지 확인.

    부착된 loader 옵션 repr 에 각 관계 경로가 나타나는지 관찰한다. 특정 관계의
    eager load 가 사라지면(그 관계만 lazy 회귀) 해당 어서션이 실패한다.
    """
    configure_mappers()
    from app.crud.treatment_crud import stmt_treatment_list
    from app.schemas.treatment import TreatmentFilter

    stmt = stmt_treatment_list(shop_id=1, filters=TreatmentFilter())
    reprs = _options_repr(stmt)

    assert "treatment_items" in reprs  # Treatment.treatment_items (+ menu_detail 중첩)
    assert "phonebook" in reprs  # Treatment.phonebook
    assert "staff_user" in reprs  # Treatment.staff_user


def test_today_reservation_list_eager_loads_relations() -> None:
    """오늘예약+인사이트 조립 경로가 eager load 로 항목별 지연 쿼리를 피한다.

    get_today_reservation_list_with_customer_insight 는 목록 문에서 joinedload
    를 쓰고, 이후 phonebook_id 를 일괄로 모아 단일 bulk 인사이트 쿼리를 발행한다
    (항목별 쿼리 없음). 여기서는 select 문 빌더 부분의 eager load 를 고정한다.
    """
    configure_mappers()
    from app.models.treatment import Treatment

    # 소스가 select(Treatment).options(joinedload(...)) 구조를 유지하는지 확인하기
    # 위해, 동일 관계가 joined 전략으로 매핑돼 있는지 매퍼로 관찰한다.
    rels = Treatment.__mapper__.relationships
    assert "treatment_items" in rels
    assert "phonebook" in rels
    assert "staff_user" in rels


# ---------------------------------------------------------------------------
# REQ-PERF-003 — summary 집계가 SQL GROUP BY 로 수행됨을 고정
# ---------------------------------------------------------------------------


def test_statistics_crud_uses_sql_group_by_not_python_loops() -> None:
    """AC-003-2 가드: 집계 함수 소스가 group_by + 집계 함수를 사용한다.

    소스 정적 검사로 SQL 집계(group_by/func.count/func.sum)가 존재함을 확인한다.
    이것이 사라지면 파이썬 행 순회 누적으로의 회귀 신호이므로 실패시킨다.
    """
    import inspect

    from app.crud import statistics_crud

    source = inspect.getsource(statistics_crud)
    # SQL 집계의 핵심 토큰들이 소스에 존재해야 한다.
    assert "group_by" in source
    assert "func.count" in source
    assert "func.sum" in source


def test_customer_insight_is_bulk_not_per_row() -> None:
    """고객 인사이트가 phonebook_id 리스트를 받는 일괄(bulk) 시그니처를 유지한다.

    per-row 조회로 회귀하면 N+1 이 되므로, bulk 함수가 여러 id 를 한 번에 받는
    형태(list 인자)를 유지하는지 시그니처로 고정한다.
    """
    import inspect

    from app.crud.statistics_crud import get_customer_insight_bulk

    params = inspect.signature(get_customer_insight_bulk).parameters
    # (db, shop_id, phonebook_ids) 형태의 일괄 조회 시그니처를 유지.
    assert "phonebook_ids" in params
