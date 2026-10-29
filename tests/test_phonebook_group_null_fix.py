"""전화번호부 그룹 목록의 '그룹 없음' 처리 교정 테스트.

대상:
- app/schemas/phonebook.py::PhonebookGroupedByGroupnameResponse (group_name nullable)
- app/schemas/phonebook.py::PhonebookCreate / PhonebookUpdate (group_name 정규화)
- GET /phonebooks/groups (app/services/phonebook_service.py)

버그 1: 그룹 응답 스키마의 group_name 이 필수 str 이었다. 그래서 group_name 이 NULL 인
연락처('고객 미지정'처럼 그룹 없이 생성된 연락처)가 하나라도 있으면 응답 모델 생성이
실패해 그룹 화면 전체가 500 이었다.
버그 2: 앱 CustomerRegistrationModal 은 "그룹 없음"을 group_name: '' 로 보낸다.
그래서 "그룹 없음"이 '' 와 NULL 두 가지로 저장돼 두 버킷으로 갈라졌다.

요구 동작:
- 그룹 응답의 group_name 은 str | None 이다. "그룹 없음"은 null 버킷 하나다.
- 생성/수정 시 '' 와 공백뿐인 group_name 은 None 으로, 실제 이름은 앞뒤 공백을
  제거해 저장한다.

DB 없이 검증한다. 라우터만 올린 최소 FastAPI 앱에 get_db/get_current_shop 을
오버라이드하고, 서비스가 import 한 CRUD 이름을 monkeypatch 한다(기존 fake 패턴).
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import phonebook as phonebook_api
from app.database import get_db
from app.dependencies.shop import get_current_shop
from app.models.phonebook import Phonebook
from app.schemas.phonebook import PhonebookCreate, PhonebookUpdate
from app.services import phonebook_service

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
CURRENT_SHOP = SimpleNamespace(id=10)


def _contact(contact_id: int, group_name: str | None) -> Phonebook:
    return Phonebook(
        id=contact_id,
        shop_id=CURRENT_SHOP.id,
        group_name=group_name,
        name=f"고객{contact_id}",
        phone_number=f"010-0000-{contact_id:04d}",
        created_at=NOW,
        updated_at=NOW,
    )


class FakeSession:
    """create/update 서비스가 쓰는 Session API(add/commit/rollback/refresh)."""

    def __init__(self) -> None:
        self.added: list = []
        self.commit_count = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        """롤백할 상태 없음."""

    def refresh(self, obj: object) -> None:
        """DB 재조회 없음."""


# ---------------------------------------------------------------------------
# GET /phonebooks/groups: NULL 그룹이 있어도 200 + null 버킷
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """VIP 1명 + 그룹 없는(NULL) 2명이 있는 샵의 그룹 API 클라이언트."""
    contacts = [_contact(1, "VIP"), _contact(2, None), _contact(3, None)]
    # GROUP BY group_name 은 NULL 을 한 그룹으로 묶는다.
    monkeypatch.setattr(
        phonebook_service,
        "get_group_counts_by_groupname",
        lambda _db, _shop_id: [("VIP", 1), (None, 2)],
    )
    monkeypatch.setattr(
        phonebook_service,
        "get_all_phonebooks_by_shop",
        lambda _db, _shop_id: contacts,
    )
    app = FastAPI()
    app.include_router(phonebook_api.router)
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_current_shop] = lambda: CURRENT_SHOP
    return TestClient(app, raise_server_exceptions=False)


def test_groups_with_ungrouped_contacts_returns_null_bucket(client: TestClient) -> None:
    """그룹 없는 연락처가 있어도 200 이고, null 버킷의 개수와 항목이 정확하다."""
    response = client.get("/phonebooks/groups", params={"with_items": True})

    assert response.status_code == 200
    body = response.json()
    assert [(group["group_name"], group["count"]) for group in body] == [
        ("VIP", 1),
        (None, 2),
    ]
    null_bucket = body[1]
    assert [item["id"] for item in null_bucket["items"]] == [2, 3]
    assert all(item["group_name"] is None for item in null_bucket["items"])


def test_groups_without_items_returns_null_bucket(client: TestClient) -> None:
    """항목 없이 개수만 조회해도 null 버킷이 200 으로 내려온다."""
    response = client.get("/phonebooks/groups")

    assert response.status_code == 200
    assert response.json() == [
        {"group_name": "VIP", "count": 1, "items": []},
        {"group_name": None, "count": 2, "items": []},
    ]


# ---------------------------------------------------------------------------
# 생성/수정 입력 정규화: "그룹 없음"은 항상 None
# ---------------------------------------------------------------------------

NORMALIZATION_CASES = [
    ("", None),
    ("   ", None),
    ("\t\n ", None),
    ("  VIP  ", "VIP"),
    ("VIP", "VIP"),
    (None, None),
]
NORMALIZATION_IDS = ["empty", "spaces", "whitespace", "padded", "plain", "null"]


@pytest.mark.parametrize("schema", [PhonebookCreate, PhonebookUpdate])
@pytest.mark.parametrize(
    ("raw", "expected"),
    NORMALIZATION_CASES,
    ids=NORMALIZATION_IDS,
)
def test_group_name_is_normalized(
    schema: type[PhonebookCreate | PhonebookUpdate],
    raw: str | None,
    expected: str | None,
) -> None:
    """'' 와 공백뿐인 값은 None, 실제 이름은 앞뒤 공백을 제거한다."""
    data = schema(name="홍길동", phone_number="01012345678", group_name=raw)

    assert data.group_name == expected


def test_group_name_length_is_checked_after_strip() -> None:
    """앞뒤 공백은 길이 제한(100자)에 포함하지 않는다. 실제 이름이 길면 거절한다."""
    name = "가" * 100

    padded = PhonebookCreate(
        name="홍길동",
        phone_number="01012345678",
        group_name=f"  {name}  ",
    )

    assert padded.group_name == name
    with pytest.raises(ValidationError):
        PhonebookCreate(
            name="홍길동",
            phone_number="01012345678",
            group_name=name + "가",
        )


@pytest.mark.parametrize(
    ("raw", "stored"),
    [("", None), ("   ", None), ("  VIP  ", "VIP")],
    ids=["empty", "spaces", "padded"],
)
def test_create_stores_normalized_group_name(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    stored: str | None,
) -> None:
    """생성 시 '' 는 NULL 로, '  VIP  ' 는 'VIP' 로 저장된다."""
    monkeypatch.setattr(
        phonebook_service,
        "get_phonebook_by_phone_number",
        lambda *_args: None,
    )
    db = FakeSession()
    data = PhonebookCreate(name="홍길동", phone_number="01012345678", group_name=raw)

    phonebook_service.create_phonebook_service(db, data, CURRENT_SHOP)

    [phonebook] = db.added
    assert phonebook.group_name == stored
    assert db.commit_count == 1


@pytest.mark.parametrize(
    ("raw", "stored"),
    [("", None), ("   ", None), ("  VIP  ", "VIP")],
    ids=["empty", "spaces", "padded"],
)
def test_update_stores_normalized_group_name(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    stored: str | None,
) -> None:
    """수정 시 '' 는 그룹 해제(NULL)로, '  VIP  ' 는 'VIP' 로 저장된다."""
    existing = _contact(1, "기존그룹")
    monkeypatch.setattr(
        phonebook_service,
        "get_phonebook_by_phone_number",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        phonebook_service,
        "get_phonebook_by_id",
        lambda *_args: existing,
    )

    phonebook_service.update_phonebook_service(
        FakeSession(),
        existing.id,
        PhonebookUpdate(group_name=raw),
        CURRENT_SHOP,
    )

    assert existing.group_name == stored


def test_update_without_group_name_keeps_existing_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """group_name 을 보내지 않은 수정은 기존 그룹을 건드리지 않는다."""
    existing = _contact(1, "기존그룹")
    monkeypatch.setattr(
        phonebook_service,
        "get_phonebook_by_phone_number",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        phonebook_service,
        "get_phonebook_by_id",
        lambda *_args: existing,
    )

    phonebook_service.update_phonebook_service(
        FakeSession(),
        existing.id,
        PhonebookUpdate(memo="메모만 수정"),
        CURRENT_SHOP,
    )

    assert existing.group_name == "기존그룹"
    assert existing.memo == "메모만 수정"
