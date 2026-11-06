"""운영(production) CORS origin 제한 테스트 (보안).

리프레시 토큰 쿠키는 SameSite=None 이다(app/api/auth.py). 그래서 CORS 가
credentials 와 함께 허용한 origin 의 페이지는 /auth/refresh 를 쿠키째 호출하고,
새 토큰이 든 응답을 읽을 수 있다. 사용자 결정(2026-11-06): production 은 코드에
적은 웹 origin 만 허용하고, localhost · 사설 IP 정규식은 개발 환경
(local/debug/stage)에서만 켠다.

대상: app/main.py (CORSMiddleware 의 allow_origin_regex = cors_origin_regex(APP_ENV))

app.main 은 import 시점에 settings.APP_ENV 로 앱을 만든다.
test_api_docs_exposure_security.py 와 같이 APP_ENV 를 바꿔 모듈을 다시
실행(reload)한 실제 앱에 요청한다. 끝나면 원래 설정으로 다시 실행해 다른 테스트에
영향을 주지 않는다. 리로드가 logs/*.log 를 만들지 않도록 파일 로그는 끈다.
"""

import importlib
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from app.core.config import settings

WEB_ORIGINS = ["https://kmc2.daeho2.shop", "https://bueafit.vercel.app"]
DEV_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:8081",
    "http://[::1]:3000",
    "http://10.0.0.5",
    "http://172.16.1.1:8080",
    "http://192.168.0.10:3000",
]


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


def _preflight(client: TestClient, origin: str) -> Response:
    """리프레시 토큰 재발급(POST /auth/refresh) 직전에 브라우저가 보내는 preflight."""
    return client.options(
        "/auth/refresh",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
    )


@pytest.mark.parametrize("origin", DEV_ORIGINS)
def test_production_rejects_dev_origins(
    main_app_for: Callable[[str], FastAPI],
    origin: str,
) -> None:
    """운영에서는 localhost · 사설 IP origin 에 CORS 허용 헤더를 주지 않는다."""
    client = TestClient(main_app_for("production"))

    preflight = _preflight(client, origin)
    simple = client.get("/health", headers={"Origin": origin})

    assert "access-control-allow-origin" not in preflight.headers
    assert "access-control-allow-origin" not in simple.headers


@pytest.mark.parametrize("origin", WEB_ORIGINS)
def test_production_allows_listed_web_origins(
    main_app_for: Callable[[str], FastAPI],
    origin: str,
) -> None:
    """운영에서도 코드에 적은 웹 origin 은 credentials 와 함께 허용한다."""
    client = TestClient(main_app_for("production"))

    response = _preflight(client, origin)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"


@pytest.mark.parametrize("app_env", ["local", "stage"])
def test_non_production_keeps_dev_origins(
    main_app_for: Callable[[str], FastAPI],
    app_env: str,
) -> None:
    """개발 환경은 지금처럼 localhost · 사설 IP origin 을 허용한다."""
    client = TestClient(main_app_for(app_env))

    for origin in ("http://localhost:5173", "http://192.168.0.10:3000"):
        response = _preflight(client, origin)

        assert response.status_code == 200, origin
        assert response.headers["access-control-allow-origin"] == origin
