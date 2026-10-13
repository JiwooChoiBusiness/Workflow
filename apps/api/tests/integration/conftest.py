from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import sysinfo
from app.config import Settings
from app.db.engine import Database
from app.logging import configure_logging
from app.main import create_app

BASE_URL = "http://127.0.0.1:8500"
EMAIL = "owner@example.com"
PASSWORD = "correct-horse-battery"  # noqa: S105  테스트 계정


def fake_gemini_keys(count: int = 3) -> list[str]:
    # 비밀 탐지 도구에 걸리지 않도록 실행 시에 만든다. 형식만 Google API 키와 같다.
    return ["AIza" + str(i) * 31 + f"k{i:03d}" for i in range(count)]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "data_dir": tmp_path / "data",
            "web_dist_dir": tmp_path / "no-dist",
            "gemini_api_keys": fake_gemini_keys(),
        }
    )


@pytest.fixture
async def app(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    # 환경 점검은 pip·PowerShell 프로세스를 띄워 느리다. test_sysinfo에서 따로 본다.
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    application = create_app(settings)
    try:
        # lifespan이 임시 폴더의 app.db에 마이그레이션을 적용한다.
        async with application.router.lifespan_context(application):
            yield application
    finally:
        # create_app이 붙인 임시 폴더의 로그 파일 핸들러를 닫아야 Windows에서 폴더를 지운다.
        configure_logging("INFO")


@pytest.fixture
def db(app: FastAPI) -> Database:
    return app.state.db


@pytest.fixture
async def make_client(app: FastAPI) -> AsyncIterator[Callable[[], AsyncClient]]:
    # 클라이언트마다 쿠키 저장소가 따로라 브라우저 여러 개(로그인 세션 여러 개)처럼 쓸 수 있다.
    clients: list[AsyncClient] = []

    def factory() -> AsyncClient:
        client = AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.aclose()


@pytest.fixture
def client(make_client: Callable[[], AsyncClient]) -> AsyncClient:
    return make_client()


Login = Callable[..., Awaitable[httpx.Response]]


@pytest.fixture
def login() -> Login:
    async def do_login(
        client: AsyncClient, email: str = EMAIL, password: str = PASSWORD
    ) -> httpx.Response:
        return await client.post(
            "/api/console/auth/login", json={"email": email, "password": password}
        )

    return do_login


@pytest.fixture
async def account(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/api/console/setup", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def logged_in(client: AsyncClient, account: dict[str, Any], login: Login) -> AsyncClient:
    response = await login(client)
    assert response.status_code == 200, response.text
    return client
