"""운영(production) API 문서 비노출 테스트 (보안).

사용자 결정: APP_ENV == "production" 이면 /docs · /redoc · /openapi.json 을 끄고
(FastAPI docs_url / redoc_url / openapi_url = None) /changelog 도 노출하지 않는다.
local / debug / stage 는 지금과 같다.

대상: app/main.py (api_docs_kwargs · api_docs_enabled 로 앱 구성)

app.main 은 import 시점에 settings.APP_ENV 로 앱을 만든다. 그래서 APP_ENV 를 바꿔
모듈을 다시 실행(reload)한 실제 앱에 요청한다. 끝나면 원래 설정으로 다시 실행해 다른
테스트에 영향을 주지 않는다. 리로드가 logs/*.log 를 만들지 않도록 파일 로그는 끈다.
"""

import importlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATHS = ["/docs", "/redoc", "/openapi.json", "/changelog"]


@pytest.fixture
def main_app_for() -> Iterator[Callable[[str], FastAPI]]:
    """APP_ENV 를 바꿔 app.main 을 다시 실행한 앱을 돌려준다(끝나면 원래대로 복구)."""
    import app.main as main_module

    original = (settings.APP_ENV, settings.LOG_TO_FILE)

    def build(app_env: str) -> FastAPI:
        settings.APP_ENV, settings.LOG_TO_FILE = app_env, False
        return importlib.reload(main_module).app

    yield build

    settings.APP_ENV, settings.LOG_TO_FILE = original
    importlib.reload(main_module)


@pytest.mark.parametrize("path", DOC_PATHS)
def test_production_hides_api_docs(
    main_app_for: Callable[[str], FastAPI],
    path: str,
) -> None:
    """운영(production)에서는 문서 UI · 스키마 · 변경 이력이 모두 404 다."""
    client = TestClient(main_app_for("production"))

    assert client.get(path).status_code == 404


def test_production_still_serves_api(main_app_for: Callable[[str], FastAPI]) -> None:
    """문서만 끈다: production 에서도 API(헬스체크)는 그대로 응답한다."""
    client = TestClient(main_app_for("production"))

    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("app_env", ["local", "debug", "stage"])
def test_non_production_keeps_api_docs(
    main_app_for: Callable[[str], FastAPI],
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    """운영 외 환경(local / debug / stage)은 기존처럼 문서 · 변경 이력을 준다."""
    # /changelog 는 CWD 기준 app/docs/api-changelog.md 를 읽는다(기존 동작).
    monkeypatch.chdir(REPO_ROOT)
    client = TestClient(main_app_for(app_env))

    assert [client.get(path).status_code for path in DOC_PATHS] == [200] * 4
