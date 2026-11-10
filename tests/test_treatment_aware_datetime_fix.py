"""시간대 없는 예약 일시 거절 테스트 (예약 시각 9시간 밀림).

대상: app/schemas/treatment.py::TreatmentBase 의 reserved_at · finished_at
(POST /treatments · PUT /treatments/{treatment_id})

결함: 시간대(오프셋) 없는 일시를 그대로 받아 UTC 벽시계로 저장했다. 앱의 예약
생성이 KST 벽시계 시각('2026-11-10T14:00:00')을 오프셋 없이 보내 14:00 예약이
14:00Z 로 저장됐고, 앱은 이를 23:00 KST 로 보여 줬다. 응답은 예전부터 Z 를 붙여
나간다(BaseResponseModel).

요구 동작(사용자 결정, 2026-11-10 · 앱 D1 수정 kmcbeauty-native PR #3 이후 적용):
- 시간대 없는 reserved_at · finished_at 은 그 필드 오류(timezone_aware) 422 다.
  서비스는 호출되지 않는다.
- Z · +00:00 · +09:00 처럼 시간대가 있는 값은 그대로 받는다. 같은 순간이면 같은
  UTC 시각이다(세션 TZ=UTC 라 UTC 로 저장된다).
- 응답은 그대로다. DB 의 naive UTC 일시에 Z 를 붙여 직렬화한다.

DB 없이 검증한다. 시술 라우터를 올리고 upsert 서비스는 받은 데이터를 기록하는
스텁이다. 인증 · 선택 샵 의존성은 고정값으로 바꾼다.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.models  # noqa: F401  # 문자열 관계 참조 해석을 위해 전체 모델 로드
from app.api import treatment as treatment_api
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.shop import get_current_shop
from app.schemas.treatment import (
    TreatmentCreate,
    TreatmentInDBBase,
    TreatmentSimpleResponse,
    TreatmentUpdate,
)

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
# 앱 화면의 2026-11-10 14:00 KST 와 같은 순간.
EXPECTED_UTC = datetime(2026, 11, 10, 5, 0, tzinfo=UTC)
BODY = {"reserved_at": "2026-11-10T05:00:00Z", "status": "RESERVED"}
WRITES = [
    pytest.param("post", "/treatments", 201, id="create"),
    pytest.param("put", "/treatments/1", 200, id="update"),
]


@pytest.fixture
def calls() -> list[TreatmentCreate | TreatmentUpdate]:
    """Upsert 서비스가 받은 요청 데이터."""
    return []


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[TreatmentCreate | TreatmentUpdate],
) -> TestClient:
    """시술 라우터. upsert 서비스는 받은 data 를 calls 에 남기는 스텁이다."""

    def _upsert(
        *,
        data: TreatmentCreate | TreatmentUpdate,
        **_kwargs: object,
    ) -> TreatmentSimpleResponse:
        calls.append(data)
        return TreatmentSimpleResponse(id=1, created_at=NOW, updated_at=NOW)

    monkeypatch.setattr(treatment_api, "upsert_treatment_service", _upsert)

    api = FastAPI()
    api.include_router(treatment_api.router)
    api.dependency_overrides[get_db] = lambda: None
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    api.dependency_overrides[get_current_shop] = lambda: SimpleNamespace(
        id=10,
        user_id=1,
    )
    return TestClient(api)


# ---------------------------------------------------------------------------
# 시간대 없는 일시: 그 필드 오류 422, 서비스 호출 없음
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "_status"), WRITES)
@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        # 고치기 전 앱의 예약 생성이 보내던 모양(KST 벽시계, 오프셋 없음).
        pytest.param(
            {"reserved_at": "2026-11-10T14:00:00"},
            "reserved_at",
            id="reserved-naive",
        ),
        pytest.param(
            {"reserved_at": "2026-11-10T14:00:00.000"},
            "reserved_at",
            id="reserved-naive-ms",
        ),
        pytest.param(
            {"finished_at": "2026-11-10T15:00:00"},
            "finished_at",
            id="finished-naive",
        ),
    ],
)
def test_datetime_without_offset_is_422(  # noqa: PLR0913  # pytest 파라미터
    client: TestClient,
    calls: list[TreatmentCreate | TreatmentUpdate],
    method: str,
    path: str,
    _status: int,
    overrides: dict[str, str],
    field: str,
) -> None:
    """시간대 없는 일시는 무엇을 기준으로 한 시각인지 알 수 없어 거절한다."""
    response = getattr(client, method)(path, json={**BODY, **overrides})

    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert [(e["loc"][-1], e["type"]) for e in errors] == [
        (field, "timezone_aware"),
    ]
    assert calls == []


# ---------------------------------------------------------------------------
# 시간대가 있는 일시: 그대로 받고, 같은 순간은 같은 UTC 시각이다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "status"), WRITES)
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("2026-11-10T05:00:00Z", id="z"),
        # 앱 D1 수정 뒤 예약 생성이 보내는 모양(밀리초 + Z).
        pytest.param("2026-11-10T05:00:00.000Z", id="app-ms-z"),
        pytest.param("2026-11-10T05:00:00+00:00", id="utc-offset"),
        pytest.param("2026-11-10T14:00:00+09:00", id="kst-offset"),
    ],
)
def test_datetime_with_offset_is_accepted(  # noqa: PLR0913  # pytest 파라미터
    client: TestClient,
    calls: list[TreatmentCreate | TreatmentUpdate],
    method: str,
    path: str,
    status: int,
    value: str,
) -> None:
    """Z · +00:00 · +09:00 모두 받고, 같은 순간(14:00 KST = 05:00 UTC)이 된다."""
    response = getattr(client, method)(
        path,
        json={**BODY, "reserved_at": value, "finished_at": value},
    )

    assert response.status_code == status, response.text
    (data,) = calls
    assert data.finished_at is not None
    assert data.reserved_at.astimezone(UTC) == EXPECTED_UTC
    assert data.finished_at.astimezone(UTC) == EXPECTED_UTC


@pytest.mark.parametrize(("method", "path", "status"), WRITES)
def test_finished_at_may_be_omitted_or_null(
    client: TestClient,
    method: str,
    path: str,
    status: int,
) -> None:
    """대조군: 완료일시는 지금처럼 생략하거나 null 로 보낼 수 있다."""
    omitted = getattr(client, method)(path, json=BODY)
    null = getattr(client, method)(path, json={**BODY, "finished_at": None})

    assert (omitted.status_code, null.status_code) == (status, status)


# ---------------------------------------------------------------------------
# 응답 · 문서
# ---------------------------------------------------------------------------


def test_response_still_marks_stored_naive_utc_with_z() -> None:
    """대조군: DB 의 naive UTC 일시는 응답에서 그대로 Z 가 붙는다."""
    stored = TreatmentInDBBase.model_validate(
        {
            "id": 1,
            "shop_id": 10,
            "created_user_id": 1,
            "reserved_at": datetime(2026, 11, 10, 5, 0),  # noqa: DTZ001  # DB 값
            "finished_at": datetime(2026, 11, 10, 6, 0),  # noqa: DTZ001  # DB 값
            "status": "COMPLETED",
            "created_at": datetime(2026, 11, 10, 4, 0),  # noqa: DTZ001  # DB 값
            "updated_at": datetime(2026, 11, 10, 4, 0),  # noqa: DTZ001  # DB 값
        },
    )

    dumped = stored.model_dump(mode="json")
    assert (dumped["reserved_at"], dumped["finished_at"]) == (
        "2026-11-10T05:00:00Z",
        "2026-11-10T06:00:00Z",
    )


def test_offset_requirement_is_documented_in_openapi() -> None:
    """API 문서(OpenAPI)의 두 일시 설명에 시간대가 필요하다고 적혀 있다."""
    for schema in (TreatmentCreate, TreatmentUpdate):
        properties = schema.model_json_schema()["properties"]
        for field in ("reserved_at", "finished_at"):
            assert "시간대" in properties[field]["description"], (schema, field)
