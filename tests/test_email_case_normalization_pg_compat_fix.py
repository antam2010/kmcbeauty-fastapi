"""MySQL → PostgreSQL(psycopg v3) 전환 호환성 — 이메일 대소문자 무시 회귀 테스트.

MySQL 의 대소문자 무시 collation 에서는 users.email unique 제약과 모든 이메일
조회가 사실상 대소문자를 무시했다. PostgreSQL 은 대소문자를 구분해 비교하므로
`A@x.com` 과 `a@x.com` 이 별개 계정으로 가입될 수 있고, 첫 글자를 대문자로
바꾸는 모바일 키보드에서는 로그인이 실패한다.

결정(DB·모델·Alembic 변경 없음): 앱에서 이메일을 소문자로 정규화한다.
- 쓰기: 저장/식별용 스키마 필드(UserBase.email 계열, ShopUserAssociateRequest.email)
- 읽기: app/crud/user_crud.py::get_user_by_email
  (로그인·이메일 중복 체크·샵 유저 연결의 공통 조회)

CI 에는 실DB 가 없으므로, 세션이 실행하려는 SELECT 를 DB 없이 가로채
PostgreSQL 방언으로 컴파일한 뒤 users.email 비교에 바인딩되는 값을 검사한다.
"""

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Query

from app.crud import user_crud
from app.enum.role import UserRole
from app.schemas.shop_user import ShopUserAssociateRequest
from app.schemas.user import UserCreate, UserResponse, UserUpdate
from app.services import auth_service, user_service

_STORED = "user@example.com"  # 정규화(소문자) 규약으로 저장된 값
_MIXED = "Mixed.Case@Example.COM"
_MIXED_NORMALIZED = "mixed.case@example.com"
_PLAINTEXT = "pass1234"  # 테스트용 더미 비밀번호

# 함수로 감싸지 않은 users.email 단순 비교(unique 인덱스 사용 가능)의 바인딩 이름.
_EMAIL_BIND = re.compile(r"\busers\.email = %\((\w+)\)s")


class FakeUser:
    """조회 결과로 돌려줄 최소 User 스텁."""

    def __init__(self, email: str = _STORED) -> None:
        self.id = 1
        self.email = email
        self.password = "stored-hash"  # noqa: S105  # 테스트용 더미 해시


class _CaptureQuery(Query):
    """db.query().filter().first() 의 최종 SELECT 를 기록하는 Query."""

    def first(self) -> object | None:
        self.capture.statements.append(self.statement)
        return self.capture.rows.get(_bound_email(self.statement))


class CaptureSession:
    """query() 로 실행하려는 문장을 기록하는 DB-free 스파이 세션.

    rows 는 {저장된 이메일: row} 이다. PostgreSQL 처럼 바인딩 값이 저장 값과
    대소문자까지 같을 때만 row 를 돌려준다.
    """

    def __init__(self, rows: dict[str, object] | None = None) -> None:
        self.statements: list[Select] = []
        self.rows = rows or {}

    def query(self, *entities: object) -> _CaptureQuery:
        query = _CaptureQuery(entities)
        # Query 는 generative 복사 시 __dict__ 를 복제하므로 체인 끝까지 전달된다.
        query.capture = self
        return query


def _pg_compile(stmt: Select) -> tuple[str, dict[str, object]]:
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


def _bound_email(stmt: Select) -> object:
    """users.email 비교에 실제로 바인딩되는 값을 반환한다."""
    sql, params = _pg_compile(stmt)
    match = _EMAIL_BIND.search(sql)
    assert match, f"users.email = :값 형태의 비교가 없음:\n{sql}"
    return params[match.group(1)]


# ---------------------------------------------------------------------------
# 단일 소스: normalize_email
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("User@Example.COM", "user@example.com"),
        ("  USER@EXAMPLE.COM\t", "user@example.com"),
        # 대소문자·앞뒤 공백 외의 변형(점, +태그 제거 등)은 하지 않는다.
        ("First.Last+Tag@Example.com", "first.last+tag@example.com"),
        ("user@example.com", "user@example.com"),
    ],
    ids=["mixed-case", "upper-with-whitespace", "keeps-dot-and-plus", "normalized"],
)
def test_normalize_email_strips_and_lowercases(raw: str, expected: str) -> None:
    """정규형은 앞뒤 공백 제거 + 소문자이며 멱등이다."""
    from app.utils.email import normalize_email

    assert normalize_email(raw) == expected
    assert normalize_email(expected) == expected


# ---------------------------------------------------------------------------
# 읽기: get_user_by_email (로그인·이메일 중복 체크·샵 유저 연결의 공통 조회)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw",
    ["User@Example.COM", "USER@EXAMPLE.COM", "  user@example.com ", "user@example.com"],
    ids=["mixed-case", "upper-case", "surrounding-whitespace", "normalized"],
)
def test_get_user_by_email_binds_normalized_value(raw: str) -> None:
    """(a) 조회 바인딩 값은 소문자 정규형이어야 한다."""
    db = CaptureSession()
    user_crud.get_user_by_email(db, raw)

    (stmt,) = db.statements
    assert _bound_email(stmt) == _STORED
    # lower(users.email) 로 감싸면 기존 unique 인덱스를 타지 못한다(함수 인덱스 없음).
    sql, _ = _pg_compile(stmt)
    assert "lower(" not in sql


def test_login_with_mixed_case_email_resolves_stored_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) 로그인: OAuth2 폼 username 은 스키마를 거치지 않아도 같은 유저를 찾는다."""
    user = FakeUser()
    db = CaptureSession(rows={_STORED: user})
    # 비밀번호 검증은 이 테스트의 관심사가 아니므로 성공(승급 없음)으로 고정한다.
    monkeypatch.setattr(
        auth_service,
        "verify_and_upgrade_password",
        lambda _password, _hash: (True, None),
    )

    result = auth_service.authenticate_user_service(db, "User@Example.COM", _PLAINTEXT)

    assert result is user
    (stmt,) = db.statements
    assert _bound_email(stmt) == _STORED


def test_email_check_treats_case_variant_as_existing() -> None:
    """이메일 중복 체크: 대소문자만 다른 이메일도 '이미 존재'로 판정한다.

    Query 파라미터의 EmailStr 은 도메인만 소문자화하므로 로컬 파트 대문자가 남는다.
    """
    db = CaptureSession(rows={_STORED: FakeUser()})

    result = user_service.check_user_email_service(db, email="User@example.com")

    assert result.exists is True


# ---------------------------------------------------------------------------
# 쓰기: 저장/식별용 스키마 필드는 소문자로 정규화한다
# ---------------------------------------------------------------------------
def test_user_create_lowercases_email() -> None:
    """(c) 가입: create_user_service 가 저장에 쓰는 model_dump 까지 소문자다."""
    user = UserCreate(name="홍길동", email=_MIXED, password=_PLAINTEXT)

    assert user.email == _MIXED_NORMALIZED
    assert user.model_dump(exclude={"invite_code"})["email"] == _MIXED_NORMALIZED


def test_user_update_lowercases_email() -> None:
    """회원 수정(update_user_db 화이트리스트에 email 포함)도 같은 정규화를 거친다."""
    update = UserUpdate(name="홍길동", email=_MIXED)

    assert update.model_dump(exclude_unset=True)["email"] == _MIXED_NORMALIZED


def test_shop_user_associate_request_lowercases_email() -> None:
    """(c) 샵 유저 연결: 대상 식별용 email 도 소문자로 정규화된다."""
    payload = ShopUserAssociateRequest(email=_MIXED)

    assert payload.email == _MIXED_NORMALIZED


@pytest.mark.parametrize(
    "build",
    [
        lambda email: UserCreate(name="홍길동", email=email, password=_PLAINTEXT),
        lambda email: UserUpdate(name="홍길동", email=email),
        lambda email: ShopUserAssociateRequest(email=email),
    ],
    ids=["UserCreate", "UserUpdate", "ShopUserAssociateRequest"],
)
def test_email_fields_still_reject_invalid_address(
    build: Callable[[str], object],
) -> None:
    """가드: 정규화는 EmailStr 형식 검증 이후에만 적용된다(형식 검증 무약화)."""
    with pytest.raises(ValidationError):
        build("Not-An-Email")


def test_response_schema_normalizes_email_alongside_attach_utc() -> None:
    """attach_utc("*", before) 와 이메일 정규화(after)가 충돌 없이 함께 적용된다."""
    naive = datetime(2026, 10, 15, 3, 0)  # noqa: DTZ001  # attach_utc 검증용 naive 값
    row = SimpleNamespace(
        id=1,
        name="홍길동",
        email=_MIXED,
        role=UserRole.MASTER,
        role_name=None,
        created_at=naive,
        updated_at=naive,
    )

    response = UserResponse.model_validate(row)

    assert response.email == _MIXED_NORMALIZED
    assert response.created_at.utcoffset() == timedelta(0)
