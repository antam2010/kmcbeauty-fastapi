"""조회 날짜 · 페이지 번호 범위 교정 테스트 (500 → 422).

대상:
- app/schemas/limits.py (FILTER_DATE_MIN · FILTER_DATE_MAX · PAGE_MAX)
- app/schemas/treatment.py::TreatmentFilter (GET /treatments 의 start_date · end_date)
- app/schemas/dashboard.py::DashboardFilter (GET /summary/dashboard 의 target_date)
- app/schemas/pagination.py::Page (GET /shops · /treatments · /phonebooks ·
  /treatment-menus 의 page)

결함(실 PostgreSQL 로 확인):
- 날짜 필터는 KST 날짜를 UTC 경계로 바꾸고 끝 날짜에 하루를 더한다
  (app/utils/query.py::apply_date_range_filter, 대시보드는 월말까지 본다).
  0001-01-01 · 9999-12-31 같은 값은 파이썬 date 범위(1~9999년)를 넘어
  OverflowError 로 500 이었다. 대시보드는 9999년 12월의 어느 날이든 월말 + 1일에서
  넘쳤다.
- page 에 상한이 없었다. OFFSET = size * (page - 1) 이 bigint 를 넘으면(SQLAlchemy 는
  2^31 이상 정수를 ::BIGINT 로 바인드) NumericValueOutOfRange(DataError)로 500 이었다.

요구 동작(사용자 결정): 날짜 필터는 1900-01-01 ~ 2100-12-31, page 는 1 ~ 100000
(size 는 라이브러리가 이미 1 ~ 100)이다. 밖의 값은 그 쿼리 파라미터의 422 이고
서비스까지 가지 않는다. 경계 날짜는 실제 서비스 · CRUD(tests/fake_sql.py)에서 오류 없이
처리된다.
"""

import importlib
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_pagination import add_pagination, create_page, resolve_params

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.api import phonebook as phonebook_api
from app.api import shop as shop_api
from app.api import summary as summary_api
from app.api import treatment as treatment_api
from app.api import treatment_menu as treatment_menu_api
from app.core.config import settings
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.shop import get_current_shop
from app.enum.treatment_status import PaymentMethod, TreatmentStatus
from app.models.treatment import Treatment
from tests.fake_sql import FakeSQLSession

SHOP_ID = 10
CREATED_AT = datetime(2026, 10, 1, tzinfo=UTC)

# 계약 값(사용자 결정). 바꾸면 앱 · 문서와 함께 바꾼다.
DATE_MIN, DATE_MAX = date(1900, 1, 1), date(2100, 12, 31)
PAGE_MAX = 100_000
SIZE_MAX = 100  # fastapi-pagination Params 기본 상한
PG_INT_MAX = 2**31 - 1

PAGINATED_PATHS = ["/shops", "/treatments", "/phonebooks", "/treatment-menus"]


def _treatment(treatment_id: int, reserved_at: datetime) -> Treatment:
    return Treatment(
        id=treatment_id,
        shop_id=SHOP_ID,
        reserved_at=reserved_at,
        status=TreatmentStatus.RESERVED,
        payment_method=PaymentMethod.CARD,
        customer_name=f"고객{treatment_id}",
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def _world() -> FakeSQLSession:
    """허용 범위 양 끝(KST)과 그 바로 바깥의 예약. 저장은 naive UTC 규약이다."""
    return FakeSQLSession(
        # 1899-12-31 23:59 KST(범위 밖)
        _treatment(1, datetime(1899, 12, 31, 14, 59)),  # noqa: DTZ001
        # 1900-01-01 00:00 KST(범위 첫 순간)
        _treatment(2, datetime(1899, 12, 31, 15, 0)),  # noqa: DTZ001
        # 2100-12-31 23:59 KST(범위 마지막 날)
        _treatment(3, datetime(2100, 12, 31, 14, 59)),  # noqa: DTZ001
    )


def _client(db: object = None) -> TestClient:
    """목록 · 대시보드 라우터를 main.py 처럼 페이지네이션과 함께 올린 클라이언트."""
    api = FastAPI()
    for router in (
        shop_api.router,
        treatment_api.router,
        phonebook_api.router,
        treatment_menu_api.router,
        summary_api.router,
    ):
        api.include_router(router)
    add_pagination(api)
    api.dependency_overrides[get_db] = lambda: db
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    api.dependency_overrides[get_current_shop] = lambda: SimpleNamespace(
        id=SHOP_ID,
        user_id=1,
    )
    return TestClient(api, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 날짜 필터: 범위 밖은 422(실제 서비스 경로였다면 500)
# ---------------------------------------------------------------------------

OUT_OF_WINDOW_DATES = [
    ("/treatments", "start_date", "0001-01-01"),
    ("/treatments", "start_date", "1899-12-31"),
    ("/treatments", "end_date", "9999-12-31"),
    ("/treatments", "end_date", "2101-01-01"),
    ("/summary/dashboard", "target_date", "9999-12-31"),
    ("/summary/dashboard", "target_date", "9999-12-01"),  # 월말 + 1일이 넘친다
    ("/summary/dashboard", "target_date", "2101-01-01"),
    ("/summary/dashboard", "target_date", "0001-01-01"),
    ("/summary/dashboard", "target_date", "1899-12-31"),
]


@pytest.mark.parametrize(
    ("path", "param", "value"),
    OUT_OF_WINDOW_DATES,
    ids=[f"{path}-{param}-{value}" for path, param, value in OUT_OF_WINDOW_DATES],
)
def test_out_of_window_date_is_422(path: str, param: str, value: str) -> None:
    """허용 범위 밖 날짜는 그 쿼리 파라미터의 422 다(서비스 · DB 까지 가지 않는다)."""
    db = _world()

    response = _client(db).get(path, params={param: value})

    assert response.status_code == 422, response.text
    assert [tuple(e["loc"]) for e in response.json()["detail"]] == [("query", param)]
    assert db.statements == []


def test_window_edges_list_exactly_the_kst_range() -> None:
    """양 끝 날짜에서도 날짜 계산이 넘치지 않고 KST [시작일, 종료일]을 그대로 고른다."""
    response = _client(_world()).get(
        "/treatments",
        params={"start_date": DATE_MIN.isoformat(), "end_date": DATE_MAX.isoformat()},
    )

    assert response.status_code == 200, response.text
    assert sorted(item["id"] for item in response.json()["items"]) == [2, 3]


@pytest.mark.parametrize(
    ("target_date", "treatment_id"),
    [(DATE_MIN, 2), (DATE_MAX, 3)],
    ids=["first-day", "last-day"],
)
def test_dashboard_window_edges_are_served(
    target_date: date,
    treatment_id: int,
) -> None:
    """대시보드 양 끝 날짜(월초 · 월말 계산 포함)도 200 이고 그날 예약을 센다."""
    response = _client(_world()).get(
        "/summary/dashboard",
        params={"target_date": target_date.isoformat()},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["target_date"]["total_reservations"] == 1
    assert body["summary"]["month"]["total_reservations"] == 1
    assert [item["id"] for item in body["customer_insights"]] == [treatment_id]


@pytest.fixture
def main_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """실제 앱(app.main). 처음 import 되더라도 logs/*.log 를 만들지 않게 한다."""
    monkeypatch.setattr(settings, "LOG_TO_FILE", False)
    return importlib.import_module("app.main").app


def _query_params(spec: dict) -> list[tuple[str, str, dict]]:
    return [
        (path, param["name"], param["schema"])
        for path, item in spec["paths"].items()
        for operation in item.values()
        for param in operation.get("parameters", [])
        if param["in"] == "query"
    ]


def _formats(schema: dict) -> set[str]:
    return {s.get("format") for s in [schema, *schema.get("anyOf", [])]}


def test_bounded_date_params_are_all_date_query_params(main_app: FastAPI) -> None:
    """날짜/일시 쿼리 파라미터는 위에서 범위를 검증한 세 개뿐이다.

    새 날짜 필터가 생기면 여기서 드러난다(같은 범위를 걸고 이 목록에 더한다).
    """
    date_params = {
        (path, name)
        for path, name, schema in _query_params(main_app.openapi())
        if _formats(schema) & {"date", "date-time"}
    }

    assert date_params == {(path, param) for path, param, _ in OUT_OF_WINDOW_DATES}


# ---------------------------------------------------------------------------
# 페이지 번호: 상한 밖은 422
# ---------------------------------------------------------------------------


@pytest.fixture
def page_offsets(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """목록 서비스를 빈 페이지 스텁으로 바꾸고, 각 요청의 OFFSET 을 기록한다."""
    offsets: list[int] = []

    def _empty_page(*_args: object, **_kwargs: object) -> object:
        params = resolve_params()
        offsets.append(params.to_raw_params().offset)
        return create_page([], total=0, params=params)

    for module, name in [
        (shop_api, "get_my_shops_service"),
        (treatment_api, "get_treatment_list_service"),
        (phonebook_api, "get_phonebook_list_service"),
        (treatment_menu_api, "get_treatment_menus_service"),
    ]:
        monkeypatch.setattr(module, name, _empty_page)
    return offsets


@pytest.mark.parametrize("path", PAGINATED_PATHS)
@pytest.mark.parametrize(
    "page",
    [PAGE_MAX + 1, 2 * 10**17, 10**30],
    ids=["max+1", "offset-over-bigint", "huge"],
)
def test_page_over_limit_is_422(
    page_offsets: list[int],
    path: str,
    page: int,
) -> None:
    """상한(100000)을 넘는 page 는 422 이고 목록 서비스 · DB 까지 가지 않는다."""
    response = _client().get(path, params={"page": page})

    assert response.status_code == 422, response.text
    assert [tuple(e["loc"]) for e in response.json()["detail"]] == [("query", "page")]
    assert page_offsets == []


@pytest.mark.parametrize("path", PAGINATED_PATHS)
def test_page_at_limit_keeps_offset_inside_int4(
    page_offsets: list[int],
    path: str,
) -> None:
    """페이지 번호 · 크기 상한에서도 OFFSET 은 int4 안쪽이다(그대로 조회)."""
    response = _client().get(path, params={"page": PAGE_MAX, "size": SIZE_MAX})

    assert response.status_code == 200, response.text
    assert page_offsets == [SIZE_MAX * (PAGE_MAX - 1)]
    assert page_offsets[0] < PG_INT_MAX


@pytest.mark.parametrize("path", PAGINATED_PATHS)
def test_size_over_limit_is_still_422(page_offsets: list[int], path: str) -> None:
    """페이지 크기(size) 상한 100 은 그대로다."""
    response = _client().get(path, params={"size": SIZE_MAX + 1})

    assert response.status_code == 422, response.text
    assert page_offsets == []


def _get_response_ref(path_item: dict) -> str:
    """GET 200 응답 스키마의 $ref(없으면 빈 문자열)."""
    content = path_item.get("get", {}).get("responses", {}).get("200", {})
    schema = content.get("content", {}).get("application/json", {}).get("schema", {})
    return schema.get("$ref", "")


def test_every_paginated_route_documents_and_caps_page(main_app: FastAPI) -> None:
    """실제 앱의 모든 Page 응답 엔드포인트가 page ≤ 100000 · size ≤ 100 을 쓴다.

    fastapi_pagination.Page 를 직접 쓰는 엔드포인트가 생기면 page 상한이 빠져 여기서
    실패한다(app.schemas.pagination.Page 를 쓴다).
    """
    spec = main_app.openapi()
    paginated = {
        path
        for path, item in spec["paths"].items()
        if _get_response_ref(item).startswith("#/components/schemas/Page_")
    }
    params = {
        (path, name): schema
        for path, name, schema in _query_params(spec)
        if path in paginated
    }

    assert set(PAGINATED_PATHS) <= paginated
    for path in paginated:
        assert params[(path, "page")]["maximum"] == PAGE_MAX, path
        assert params[(path, "size")]["maximum"] == SIZE_MAX, path


def test_page_limit_constant_keeps_offset_inside_int4() -> None:
    """설정값으로도 최대 OFFSET(size 상한 * (PAGE_MAX - 1))은 int4 안쪽이다."""
    from app.schemas import limits

    assert limits.PAGE_MAX == PAGE_MAX
    assert (limits.FILTER_DATE_MIN, limits.FILTER_DATE_MAX) == (DATE_MIN, DATE_MAX)
    assert SIZE_MAX * (limits.PAGE_MAX - 1) < limits.PG_INT_MAX
