import json
import shutil
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import sysinfo
from app.config import Settings
from app.db.engine import Database
from app.llm.fake_adapter import FakeAdapter, FakeTurn
from app.llm.types import ChatRequest, ErrorEvent, ModelEvent
from app.logsetup import configure_logging
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


class Script:
    """가짜 어댑터의 턴을 테스트 도중에 넣는다. 턴은 이벤트 목록이나 요청을 받는 함수다."""

    def __init__(self) -> None:
        self.turns: deque[FakeTurn] = deque()

    def add(self, *turns: FakeTurn) -> None:
        self.turns.extend(turns)

    def __call__(self, request: ChatRequest) -> list[ModelEvent]:
        if not self.turns:
            return [ErrorEvent(code="fake_script_exhausted", message="각본 끝", retryable=False)]
        turn = self.turns.popleft()
        return list(turn(request) if callable(turn) else turn)


@pytest.fixture
def short_settings() -> Iterator[Settings]:
    """임시 폴더 경로가 짧은 설정.

    pytest의 tmp_path는 길어서 storage의 session-files/<세션>/<런>/<경로> 복사본이 Windows 기본 경로
    길이(260자)를 넘는다. 실제 DATA_DIR(짧은 경로를 권장한다, 설계 12.3)과 비슷하게 맞춘다.
    """
    root = Path(tempfile.mkdtemp(prefix="wf")).resolve()
    try:
        yield Settings.model_validate(
            {
                "data_dir": root / "data",
                "web_dist_dir": root / "no-dist",
                "gemini_api_keys": fake_gemini_keys(),
            }
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def script() -> Script:
    return Script()


@pytest.fixture
async def scripted_app(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, script: Script
) -> AsyncIterator[FastAPI]:
    """app과 같지만 모델 호출이 script의 각본을 따른다. 쓰려면 모듈에서 app을 이것으로 덮는다."""
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    application = create_app(settings)
    application.state.llm = FakeAdapter([script] * 10_000)
    try:
        async with application.router.lifespan_context(application):
            yield application
    finally:
        configure_logging("INFO")


def parse_sse(body: str) -> list[dict[str, Any]]:
    """SSE 본문을 이벤트 목록으로. ping은 type만 남긴다."""
    events: list[dict[str, Any]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if ": " in line:
                name, value = line.split(": ", 1)
                fields[name] = value
        if "data" not in fields:
            continue
        data = json.loads(fields["data"])
        if fields.get("id") is not None:
            assert int(fields["id"]) == data["seq"]
            assert fields["event"] == data["type"]
        events.append(data)
    return events


@pytest.fixture
def sse() -> Callable[[str], list[dict[str, Any]]]:
    return parse_sse


DRAFT_FIELDS = (
    "instructions",
    "model",
    "tools",
    "limits",
    "sandbox",
    "approval_mode",
    "skill_version_ids",
    "file_ids",
    "updated_at",
)

MakeSession = Callable[..., Awaitable[tuple[str, str]]]


@pytest.fixture
def make_session() -> MakeSession:
    async def create(client: AsyncClient, **draft_changes: Any) -> tuple[str, str]:
        """에이전트를 만들고(초안을 고치고) 미리보기 세션을 연다. (agent_id, session_id)."""
        response = await client.post("/api/console/agents", json={"name": "Runner"})
        assert response.status_code == 201, response.text
        agent_id = response.json()["id"]
        if draft_changes:
            draft = (await client.get(f"/api/console/agents/{agent_id}/draft")).json()
            body = {key: draft[key] for key in DRAFT_FIELDS}
            body["env_vars"] = []
            body.update(draft_changes)
            saved = await client.put(f"/api/console/agents/{agent_id}/draft", json=body)
            assert saved.status_code == 200, saved.text
        response = await client.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
        assert response.status_code == 201, response.text
        return agent_id, response.json()["id"]

    return create
