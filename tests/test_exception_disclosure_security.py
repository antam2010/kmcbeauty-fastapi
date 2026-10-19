"""REQ-SEC-004 정보 노출 관리 테스트.

대상: app/exceptions.py
- 비-debug 환경에서 raw exception 문자열이 응답에 노출되지 않음 (AC-004-2)
- debug 환경에서는 노출 허용 (AC-004-3)
"""

import pytest

from app.core.config import settings
from app.exceptions import CustomException


def _build_exc(monkeypatch: pytest.MonkeyPatch, app_env: str) -> dict:
    # exceptions 모듈은 중앙 settings.APP_ENV 를 참조하므로 싱글톤 속성을 교체한다.
    monkeypatch.setattr(settings, "APP_ENV", app_env)
    exc = CustomException(
        status_code=400,
        domain="TEST",
        detail="something failed",
        exception=ValueError("internal db password leak details"),
    )
    return exc.detail


def test_non_debug_hides_raw_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-004-2: local(비-debug) 환경에서는 exception 필드 미노출."""
    detail = _build_exc(monkeypatch, "local")
    assert "exception" not in detail


def test_production_hides_raw_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-004-2: production 환경에서도 exception 필드 미노출."""
    detail = _build_exc(monkeypatch, "production")
    assert "exception" not in detail


def test_debug_exposes_raw_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-004-3: debug 환경에서는 exception 필드가 노출될 수 있다."""
    detail = _build_exc(monkeypatch, "debug")
    assert "exception" in detail
    assert "internal db password leak details" in detail["exception"]
