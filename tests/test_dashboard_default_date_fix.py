"""대시보드 조회 날짜 생략 시 422 교정 테스트.

대상: app/api/summary.py::get_dashboard_summary (GET /summary/dashboard)

결함: DashboardFilter.target_date 는 default_factory(오늘 KST)인데 엔드포인트가
`params: DashboardFilter = Depends()` 였다. FastAPI 는 이 의존성을 클래스 시그니처로
만들고, 시그니처에는 기본값이 `<factory>` 로만 보여 target_date 를 생략하면 422 였다.

요구 동작(사용자 결정): 이 엔드포인트만 쿼리 파라미터 모델
(`Annotated[DashboardFilter, Query()]`)로 받는다. 생략하면 오늘(KST)이고, 값을 주면
그대로 받는다. 범위 밖 날짜는 여전히 422 이고 문서(OpenAPI)도 두 쿼리 파라미터 그대로다.
DashboardFilter 의 필드 · 범위는 바꾸지 않는다.

서비스는 받은 필터를 기록하는 스텁으로 바꾼다(DB · Redis 없음).
"""

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import summary as summary_api
from app.database import get_db
from app.dependencies.shop import get_current_shop
from app.schemas.dashboard import DashboardFilter
from app.utils.datetime import now_kst_today

SHOP_ID = 10
_ZERO_SUMMARY = {
    "total_reservations": 0,
    "completed": 0,
    "reserved": 0,
    "visited": 0,
    "canceled": 0,
    "no_show": 0,
    "expected_sales": 0,
    "actual_sales": 0,
    "unpaid_total": 0,
}


def _empty_dashboard(day: date) -> dict:
    """응답 스키마(DashboardSummaryResponse)를 통과하는 빈 대시보드."""
    return {
        "target_date": day,
        "summary": {"target_date": _ZERO_SUMMARY, "month": _ZERO_SUMMARY},
        "sales": {"target_date": [], "month": []},
        "customer_insights": [],
        "staff_summary": {"target_date": [], "month": []},
    }


@pytest.fixture
def received(monkeypatch: pytest.MonkeyPatch) -> list[DashboardFilter]:
    """서비스가 받은 필터를 기록한다(빈 대시보드를 돌려준다)."""
    calls: list[DashboardFilter] = []

    def _service(_db: object, _shop: object, params: DashboardFilter) -> dict:
        calls.append(params)
        return _empty_dashboard(params.target_date)

    monkeypatch.setattr(summary_api, "get_dashboard_summary_service", _service)
    return calls


def _api() -> FastAPI:
    api = FastAPI()
    api.include_router(summary_api.router)
    api.dependency_overrides[get_db] = lambda: None
    api.dependency_overrides[get_current_shop] = lambda: SimpleNamespace(id=SHOP_ID)
    return api


def test_dashboard_without_target_date_is_today_kst(
    received: list[DashboardFilter],
) -> None:
    """target_date 를 생략하면 200 이고 서비스는 오늘(KST) 날짜를 받는다."""
    before = now_kst_today()
    response = TestClient(_api()).get("/summary/dashboard")
    after = now_kst_today()

    assert response.status_code == 200, response.text
    [params] = received
    assert before <= params.target_date <= after  # 자정을 걸친 실행도 허용
    assert params.force_refresh is False
    assert response.json()["target_date"] == params.target_date.isoformat()


def test_explicit_target_date_and_force_refresh_still_parse(
    received: list[DashboardFilter],
) -> None:
    """대조군: 쿼리로 준 날짜 · force_refresh=true 는 그대로 서비스까지 간다."""
    response = TestClient(_api()).get(
        "/summary/dashboard",
        params={"target_date": "2026-10-15", "force_refresh": "true"},
    )

    assert response.status_code == 200, response.text
    [params] = received
    assert (params.target_date, params.force_refresh) == (date(2026, 10, 15), True)


def test_out_of_range_target_date_is_still_422(
    received: list[DashboardFilter],
) -> None:
    """대조군: 범위 밖 날짜는 그 쿼리 파라미터의 422 다(서비스까지 가지 않는다)."""
    response = TestClient(_api()).get(
        "/summary/dashboard",
        params={"target_date": "1800-01-01"},
    )

    assert response.status_code == 422, response.text
    assert [tuple(e["loc"]) for e in response.json()["detail"]] == [
        ("query", "target_date"),
    ]
    assert received == []


def test_openapi_lists_both_query_parameters_as_optional() -> None:
    """문서도 그대로다: target_date · force_refresh 는 생략 가능한 쿼리 파라미터다."""
    operation = _api().openapi()["paths"]["/summary/dashboard"]["get"]

    parameters = {p["name"]: p for p in operation["parameters"]}
    assert sorted((name, p["in"]) for name, p in parameters.items()) == [
        ("force_refresh", "query"),
        ("target_date", "query"),
    ]
    assert not [name for name, p in parameters.items() if p.get("required")]
    assert parameters["target_date"]["schema"]["format"] == "date"
