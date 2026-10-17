"""요청 스키마 길이 · 범위 검증 교정 테스트 (500 → 422).

대상:
- app/schemas/limits.py (DB 정수 컬럼 범위 상수)
- app/schemas/user.py::NormalizedEmailStr (UserCreate · UserUpdate ·
  ShopUserAssociateRequest)
- app/schemas/treatment.py (TreatmentCreate · TreatmentUpdate · TreatmentFilter)
- app/schemas/treatment_item.py (TreatmentItemCreate · TreatmentItemUpdate)
- app/schemas/treatment_menu.py::TreatmentMenuDetailCreate
- app/schemas/device_push_token.py (등록 · 수정 · FCM 전송 요청)
- app/schemas/shop.py::ShopSelect
- app/schemas/shop_invite.py::ShopInviteCreateRequest

결함: PostgreSQL 은 컬럼 크기 · 범위를 넘는 값을 DataError 로 거절한다. 스키마가
검증하지 않은 다음 입력은 500 이었다(실 PG 로 확인).
- 문자열 길이 초과: users.email String(50), treatment.customer_name String(100) ·
  customer_phone String(20), device_push_tokens.platform String(10) · device_id
  String(255).
- 정수 범위 초과: psycopg 방언은 정수 바인드에 ::INTEGER(::SMALLINT) 캐스트를 붙인다.
  int4 범위 밖 id 는 INSERT 뿐 아니라 조회 조건(WHERE id = ...)에서도 500 이다.
- device_push_tokens.token 은 Text 지만 UNIQUE btree 인덱스 항목 한도(2704바이트)를
  넘으면 500 이다.
- 오프셋이 있는 일시가 UTC 변환 후 1~9999년을 벗어나면 INSERT 는 되지만 다시 읽을 때
  드라이버가 실패한다. 그 예약이 든 목록 조회는 계속 500 이다.
- 초대코드 expire_in(초 단위)에 범위가 없었다.

요구 동작: 위 값은 요청 검증 단계에서 422 이고 경계값은 통과한다. 앱이 보내는 초대
만료 값(1시간~30일)은 그대로 동작하고, 생략하면 기본값 7일이다.

라우터만 올린 최소 FastAPI 앱에서 인증 · 상점 · DB 의존성을 바꾸고 서비스는 스텁한다.
422 는 서비스 호출 전에 나므로 스텁은 경계값이 통과하는지 확인하는 데만 쓰인다.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api import device_push_token as device_push_token_api
from app.api import shop as shop_api
from app.api import treatment as treatment_api
from app.api import treatment_menu as treatment_menu_api
from app.api import user as user_api
from app.core.rate_limit import limiter
from app.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.shop import get_current_shop
from app.enum.role import UserRole
from app.schemas.device_push_token import (
    DevicePushTokenCreate,
    DevicePushTokenResponse,
    DevicePushTokenUpdate,
    FCMMessageRequest,
    FCMMessageResponse,
)
from app.schemas.shop import ShopSelect
from app.schemas.shop_invite import ShopInviteResponse
from app.schemas.shop_user import ShopUserAssociateRequest, ShopUserUserResponse
from app.schemas.treatment import (
    TreatmentCreate,
    TreatmentFilter,
    TreatmentSimpleResponse,
    TreatmentUpdate,
)
from app.schemas.treatment_item import TreatmentItemCreate, TreatmentItemUpdate
from app.schemas.treatment_menu import (
    TreatmentMenuDetailCreate,
    TreatmentMenuDetailResponse,
)
from app.schemas.user import UserCreate, UserResponse, UserUpdate

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
SHOP_ID = 10
INT4_MAX = 2**31 - 1
INT2_MAX = 2**15 - 1

# "@example.com" 은 12자다. 로컬 파트 38자 + 12 = 50자(컬럼 한도).
EMAIL_50 = "a" * 38 + "@example.com"
EMAIL_51 = "b" + EMAIL_50

USER = UserResponse(
    id=1,
    name="홍길동",
    email="owner@example.com",
    role=UserRole.MASTER,
    created_at=NOW,
    updated_at=NOW,
)
SIGNUP = {"name": "홍길동", "email": "new@example.com", "password": "pass1234"}
ITEM = {"menu_detail_id": 1, "base_price": 1000, "duration_min": 30, "session_no": 1}
TREATMENT = {
    "reserved_at": "2026-10-15T01:00:00Z",
    "status": "RESERVED",
    "treatment_items": [ITEM],
}
MENU_DETAIL = {"name": "젤네일", "duration_min": 60, "base_price": 30000}
DEVICE_TOKEN = {"token": "fcm-registration-a", "platform": "android"}
PUSH = {"title": "예약 알림", "body": "10시 예약"}

# 앱(InviteCodeGeneratorModal)의 만료 선택지(초): 1시간 · 6시간 · 12시간 · 1일 · 3일 ·
# 7일 · 30일.
APP_EXPIRE_IN_OPTIONS = [3600, 21600, 43200, 86400, 259200, 604800, 2592000]


def _returning(value: object) -> Callable[..., object]:
    """호출 인자와 무관하게 value 를 돌려주는 서비스 스텁."""
    return lambda *_args, **_kwargs: value


@pytest.fixture
def invite_calls(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """초대코드 생성 서비스가 받은 expire_in 기록."""
    calls: list[int | None] = []

    def _generate(**kwargs: object) -> ShopInviteResponse:
        calls.append(kwargs["invite_data"].expire_in)
        return ShopInviteResponse(
            shop_id=SHOP_ID,
            invite_code="AbCdEfGhIjK",
            expired_at=NOW,
        )

    monkeypatch.setattr(shop_api, "generate_invite_code_service", _generate)
    return calls


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    invite_calls: list[int | None],  # noqa: ARG001  # 초대 서비스 스텁 적용
) -> TestClient:
    """대상 라우터 + limiter 를 main.py 와 같이 올리고 서비스를 스텁한 클라이언트."""
    stubs: list[tuple[object, str, object]] = [
        (user_api, "create_user_service", USER),
        (user_api, "update_user_service", USER),
        (
            shop_api,
            "create_shop_user_service",
            ShopUserUserResponse(
                shop_id=SHOP_ID,
                user_id=1,
                is_primary_owner=0,
                user=USER,
            ),
        ),
        (shop_api, "set_selected_shop_service", None),
        (
            treatment_api,
            "upsert_treatment_service",
            TreatmentSimpleResponse(id=1, created_at=NOW, updated_at=NOW),
        ),
        (treatment_api, "get_treatment_list_service", None),
        (
            treatment_menu_api,
            "create_treatment_menu_detail_service",
            TreatmentMenuDetailResponse(
                id=1,
                menu_id=1,
                **MENU_DETAIL,
                created_at=NOW,
                updated_at=NOW,
            ),
        ),
    ]
    token_response = DevicePushTokenResponse(id=1, user_id=1, **DEVICE_TOKEN)
    stubs += [
        (device_push_token_api, "register_device_token_service", token_response),
        (device_push_token_api, "update_device_token_service", token_response),
        (
            device_push_token_api,
            "send_fcm_notification_service",
            FCMMessageResponse(success=True, message_id="m-1"),
        ),
    ]
    for module, name, value in stubs:
        monkeypatch.setattr(module, name, _returning(value))

    api = FastAPI()
    for router in (
        user_api.router,
        shop_api.router,
        treatment_api.router,
        treatment_menu_api.router,
        device_push_token_api.router,
    ):
        api.include_router(router)
    api.state.limiter = limiter
    api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    api.dependency_overrides[get_db] = lambda: None
    api.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    api.dependency_overrides[get_current_shop] = lambda: SimpleNamespace(
        id=SHOP_ID,
        user_id=1,
    )
    return TestClient(api, raise_server_exceptions=False)


def _treatment(**overrides: object) -> dict:
    return {**TREATMENT, **overrides}


def _item(**overrides: object) -> dict:
    return {**TREATMENT, "treatment_items": [{**ITEM, **overrides}]}


# ---------------------------------------------------------------------------
# 범위를 넘는 값은 해당 필드의 422 다(서비스 · DB 까지 가지 않는다)
# ---------------------------------------------------------------------------

OVER_LIMIT_CASES = [
    # 이메일: users.email String(50). 정규화(소문자 · 공백 제거) 뒤 길이로 센다.
    ("POST", "/users", {"json": {**SIGNUP, "email": EMAIL_51}}, ("body", "email")),
    (
        "PUT",
        "/users/me",
        {"json": {"name": "홍길동", "email": EMAIL_51}},
        ("body", "email"),
    ),
    (
        "POST",
        f"/shops/{SHOP_ID}/users",
        {"json": {"email": EMAIL_51}},
        ("body", "email"),
    ),
    # 시술 예약: 직접 입력 고객 정보 String(100) / String(20)
    (
        "POST",
        "/treatments",
        {"json": _treatment(customer_name="가" * 101)},
        ("body", "customer_name"),
    ),
    (
        "POST",
        "/treatments",
        {"json": _treatment(customer_phone="0" * 21)},
        ("body", "customer_phone"),
    ),
    (
        "PUT",
        "/treatments/1",
        {"json": _treatment(customer_name="가" * 101)},
        ("body", "customer_name"),
    ),
    # 참조 id: int4 범위(조회 조건 바인드 캐스트). 시리얼 PK 라 1 이상.
    (
        "POST",
        "/treatments",
        {"json": _treatment(phonebook_id=2**31)},
        ("body", "phonebook_id"),
    ),
    (
        "POST",
        "/treatments",
        {"json": _treatment(phonebook_id=0)},
        ("body", "phonebook_id"),
    ),
    (
        "POST",
        "/treatments",
        {"json": _treatment(staff_user_id=2**31)},
        ("body", "staff_user_id"),
    ),
    (
        "POST",
        "/treatments",
        {"json": _item(menu_detail_id=2**31)},
        ("body", "treatment_items", 0, "menu_detail_id"),
    ),
    (
        "PUT",
        "/treatments/1",
        {"json": _item(menu_detail_id=2**31)},
        ("body", "treatment_items", 0, "menu_detail_id"),
    ),
    # 시술 회차: treatment_item.session_no 는 smallint
    (
        "POST",
        "/treatments",
        {"json": _item(session_no=2**15)},
        ("body", "treatment_items", 0, "session_no"),
    ),
    (
        "PUT",
        "/treatments/1",
        {"json": _item(session_no=2**15)},
        ("body", "treatment_items", 0, "session_no"),
    ),
    # 오프셋 일시: UTC 로 바꾸면 1년 이전 / 9999년 이후(저장 후 재조회 불가)
    (
        "POST",
        "/treatments",
        {"json": _treatment(reserved_at="0001-01-01T00:00:00+09:00")},
        ("body", "reserved_at"),
    ),
    (
        "POST",
        "/treatments",
        {"json": _treatment(reserved_at="9999-12-31T23:00:00-09:00")},
        ("body", "reserved_at"),
    ),
    (
        "PUT",
        "/treatments/1",
        {"json": _treatment(finished_at="0001-01-01T00:00:00+09:00")},
        ("body", "finished_at"),
    ),
    # 목록 필터(쿼리)의 담당자 id
    (
        "GET",
        "/treatments",
        {"params": {"staff_user_id": 2**31}},
        ("query", "staff_user_id"),
    ),
    # 시술 메뉴 상세: integer 컬럼
    (
        "POST",
        "/treatment-menus/1/details",
        {"json": {**MENU_DETAIL, "duration_min": 2**31}},
        ("body", "duration_min"),
    ),
    (
        "PUT",
        "/treatment-menus/1/details/2",
        {"json": {**MENU_DETAIL, "base_price": 2**31}},
        ("body", "base_price"),
    ),
    # 디바이스 토큰 등록 · 수정
    (
        "POST",
        "/device-tokens",
        {"json": {**DEVICE_TOKEN, "platform": "a" * 11}},
        ("body", "platform"),
    ),
    (
        "POST",
        "/device-tokens",
        {"json": {**DEVICE_TOKEN, "device_id": "d" * 256}},
        ("body", "device_id"),
    ),
    (
        "POST",
        "/device-tokens",
        {"json": {**DEVICE_TOKEN, "token": "t" * 513}},
        ("body", "token"),
    ),
    (
        "POST",
        "/device-tokens",
        {"json": {**DEVICE_TOKEN, "shop_id": 2**31}},
        ("body", "shop_id"),
    ),
    (
        "PATCH",
        "/device-tokens/1",
        {"json": {"platform": "a" * 11}},
        ("body", "platform"),
    ),
    (
        "PATCH",
        "/device-tokens/1",
        {"json": {"device_id": "d" * 256}},
        ("body", "device_id"),
    ),
    ("PATCH", "/device-tokens/1", {"json": {"token": "t" * 513}}, ("body", "token")),
    # FCM 전송 대상 id
    (
        "POST",
        "/device-tokens/send-fcm",
        {"json": {**PUSH, "user_id": 2**31}},
        ("body", "user_id"),
    ),
    (
        "POST",
        "/device-tokens/send-fcm",
        {"json": {**PUSH, "shop_id": 2**31}},
        ("body", "shop_id"),
    ),
    # 샵 선택
    ("POST", "/shops/selected", {"json": {"shop_id": 2**31}}, ("body", "shop_id")),
    # 초대코드 만료(초): 60초 이상 30일 이하
    (
        "POST",
        f"/shops/{SHOP_ID}/invites",
        {"params": {"expire_in": 59}},
        ("query", "expire_in"),
    ),
    (
        "POST",
        f"/shops/{SHOP_ID}/invites",
        {"params": {"expire_in": 0}},
        ("query", "expire_in"),
    ),
    (
        "POST",
        f"/shops/{SHOP_ID}/invites",
        {"params": {"expire_in": 2_592_001}},
        ("query", "expire_in"),
    ),
]
OVER_LIMIT_IDS = [
    "signup-email-51",
    "user-update-email-51",
    "shop-user-email-51",
    "treatment-customer-name-101",
    "treatment-customer-phone-21",
    "treatment-update-customer-name-101",
    "treatment-phonebook-id-int4",
    "treatment-phonebook-id-zero",
    "treatment-staff-user-id-int4",
    "item-create-menu-detail-id-int4",
    "item-update-menu-detail-id-int4",
    "item-create-session-no-int2",
    "item-update-session-no-int2",
    "reserved-at-before-year-1-utc",
    "reserved-at-after-year-9999-utc",
    "finished-at-before-year-1-utc",
    "treatment-filter-staff-user-id-int4",
    "menu-detail-duration-int4",
    "menu-detail-update-price-int4",
    "token-register-platform-11",
    "token-register-device-id-256",
    "token-register-token-513",
    "token-register-shop-id-int4",
    "token-update-platform-11",
    "token-update-device-id-256",
    "token-update-token-513",
    "fcm-user-id-int4",
    "fcm-shop-id-int4",
    "shop-select-int4",
    "invite-expire-59s",
    "invite-expire-0s",
    "invite-expire-over-30d",
]


@pytest.mark.parametrize(
    ("method", "path", "request_kwargs", "loc"),
    OVER_LIMIT_CASES,
    ids=OVER_LIMIT_IDS,
)
def test_over_limit_value_is_422_for_that_field(
    client: TestClient,
    method: str,
    path: str,
    request_kwargs: dict,
    loc: tuple,
) -> None:
    """컬럼 크기 · 범위를 넘는 값은 500 이 아니라 그 필드의 422 다."""
    response = client.request(method, path, **request_kwargs)

    assert response.status_code == 422, response.text
    assert [tuple(error["loc"]) for error in response.json()["detail"]] == [loc]


def test_email_over_limit_reports_length_error(client: TestClient) -> None:
    """이메일 길이 초과는 길이 오류(too_long, 최대 50)로 보고된다."""
    response = client.post("/users", json={**SIGNUP, "email": EMAIL_51})

    [error] = response.json()["detail"]
    assert error["type"] == "too_long"
    assert error["ctx"]["max_length"] == 50


# ---------------------------------------------------------------------------
# 경계값은 통과한다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schema", "fields"),
    [
        (UserCreate, {**SIGNUP, "email": EMAIL_50}),
        (UserUpdate, {"name": "홍길동", "email": EMAIL_50}),
        (ShopUserAssociateRequest, {"email": EMAIL_50}),
    ],
    ids=["signup", "user-update", "shop-user"],
)
def test_email_at_limit_is_accepted(schema: type[BaseModel], fields: dict) -> None:
    assert schema(**fields).email == EMAIL_50


def test_email_length_is_checked_after_normalization() -> None:
    """앞뒤 공백 · 대문자는 정규화 뒤 길이로 센다(저장값 기준 50자)."""
    padded = f"   {EMAIL_50.upper()}   "  # 원문 56자

    assert UserCreate(**{**SIGNUP, "email": padded}).email == EMAIL_50


def test_email_max_length_is_documented_in_openapi_schema() -> None:
    assert UserCreate.model_json_schema()["properties"]["email"]["maxLength"] == 50


@pytest.mark.parametrize(
    "overrides",
    [
        {"customer_name": "가" * 100, "customer_phone": "0" * 20},
        {"phonebook_id": INT4_MAX, "staff_user_id": INT4_MAX},
        # UTC 로 정확히 0001-01-01 00:00 / 9999-12-31 23:59:59
        {"reserved_at": "0001-01-01T09:00:00+09:00"},
        {"reserved_at": "9999-12-31T14:59:59-09:00"},
        # 오프셋 없는 값은 변환 없이 그대로 저장된다
        {"reserved_at": "0001-01-01T00:00:00", "finished_at": "9999-12-31T23:59:59"},
    ],
    ids=["customer-fields", "reference-ids", "utc-min", "utc-max", "naive-edges"],
)
@pytest.mark.parametrize("schema", [TreatmentCreate, TreatmentUpdate])
def test_treatment_boundary_values_are_accepted(
    schema: type[BaseModel],
    overrides: dict,
) -> None:
    schema(**_treatment(**overrides))


@pytest.mark.parametrize("schema", [TreatmentItemCreate, TreatmentItemUpdate])
def test_item_boundary_values_are_accepted(schema: type[BaseModel]) -> None:
    item = schema(**{**ITEM, "menu_detail_id": INT4_MAX, "session_no": INT2_MAX})

    assert (item.menu_detail_id, item.session_no) == (INT4_MAX, INT2_MAX)


@pytest.mark.parametrize(
    ("schema", "fields"),
    [
        (TreatmentFilter, {"staff_user_id": INT4_MAX}),
        (
            TreatmentMenuDetailCreate,
            {**MENU_DETAIL, "duration_min": INT4_MAX, "base_price": INT4_MAX},
        ),
        (
            DevicePushTokenCreate,
            {
                "token": "t" * 512,
                "platform": "a" * 10,
                "device_id": "d" * 255,
                "shop_id": INT4_MAX,
            },
        ),
        (
            DevicePushTokenUpdate,
            {"token": "t" * 512, "platform": "a" * 10, "device_id": "d" * 255},
        ),
        (FCMMessageRequest, {**PUSH, "user_id": INT4_MAX, "shop_id": INT4_MAX}),
        (ShopSelect, {"shop_id": INT4_MAX}),
    ],
    ids=[
        "treatment-filter",
        "menu-detail",
        "token-register",
        "token-update",
        "fcm",
        "shop-select",
    ],
)
def test_boundary_values_are_accepted(schema: type[BaseModel], fields: dict) -> None:
    schema(**fields)


def test_push_token_limit_fits_btree_index_in_worst_case_utf8() -> None:
    """토큰 상한은 4바이트 문자만으로 채워도 btree 항목 한도(2704바이트) 안이다."""
    token = "\U0001f600" * 512

    assert len(DevicePushTokenCreate(token=token, platform="ios").token.encode()) < 2704
    with pytest.raises(ValidationError):
        DevicePushTokenCreate(token=token + "a", platform="ios")


@pytest.mark.parametrize(
    "expire_in",
    [60, *APP_EXPIRE_IN_OPTIONS],
    ids=["min-60s", "1h", "6h", "12h", "1d", "3d", "7d", "30d"],
)
def test_invite_expire_in_boundary_and_app_values_pass(
    client: TestClient,
    invite_calls: list[int | None],
    expire_in: int,
) -> None:
    """60초와 앱이 보내는 만료 값(1시간~30일)은 그대로 서비스에 전달된다."""
    response = client.post(
        f"/shops/{SHOP_ID}/invites",
        params={"expire_in": expire_in},
    )

    assert response.status_code == 201, response.text
    assert invite_calls == [expire_in]


def test_invite_expire_in_defaults_to_seven_days(
    client: TestClient,
    invite_calls: list[int | None],
) -> None:
    response = client.post(f"/shops/{SHOP_ID}/invites")

    assert response.status_code == 201, response.text
    assert invite_calls == [7 * 24 * 60 * 60]


@pytest.mark.parametrize(
    ("method", "path", "request_kwargs"),
    [
        ("POST", "/users", {"json": {**SIGNUP, "email": EMAIL_50}}),
        ("PUT", "/users/me", {"json": {"name": "홍길동", "email": EMAIL_50}}),
        (
            "POST",
            "/treatments",
            {"json": _treatment(customer_name="가" * 100, customer_phone="0" * 20)},
        ),
        ("POST", "/device-tokens", {"json": {**DEVICE_TOKEN, "platform": "a" * 10}}),
        ("POST", "/shops/selected", {"json": {"shop_id": INT4_MAX}}),
    ],
    ids=["signup", "user-update", "treatment", "token-register", "shop-select"],
)
def test_boundary_request_reaches_service(
    client: TestClient,
    method: str,
    path: str,
    request_kwargs: dict,
) -> None:
    """경계값 요청은 검증을 통과해 서비스까지 간다(2xx)."""
    response = client.request(method, path, **request_kwargs)

    assert 200 <= response.status_code < 300, response.text
