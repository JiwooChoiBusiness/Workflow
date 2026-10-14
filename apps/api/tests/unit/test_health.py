import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

from app import sysinfo
from app.config import Settings
from app.db.engine import Database
from app.housekeeping import Housekeeping
from app.llm.gemini_adapter import GeminiAdapter
from app.logsetup import configure_logging
from app.main import create_app
from app.runner import recovery
from app.sandbox import E2B_NOT_IMPLEMENTED, allowlist
from app.sandbox.local_workspace import LocalWorkspaceBackend

BASE_URL = "http://127.0.0.1:8500"


@pytest.fixture(autouse=True)
def _quiet_startup(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # 환경 점검은 pip·PowerShell 프로세스를 띄워 느리다. test_sysinfo에서 따로 본다.
    monkeypatch.setattr(sysinfo, "startup_checks", lambda settings: [])
    yield
    # create_app이 붙인 임시 폴더의 로그 파일 핸들러를 닫는다.
    configure_logging("INFO")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {"data_dir": tmp_path / "data", "web_dist_dir": tmp_path / "no-dist"}
    )


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client,
    ):
        yield client


async def test_healthz_reports_process_state(settings: Settings) -> None:
    async with running(create_app(settings)) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["rss_mb"] > 0
    assert isinstance(body["uptime_sec"], int)
    assert body["runner"] == {
        "agent": {"active": 0, "queued": 0},
        "workflow": {"active": 0, "queued": 0},
        "shutting_down": False,
    }


async def test_readyz_turns_503_after_shutdown(settings: Settings) -> None:
    app = create_app(settings)
    async with running(app) as client:
        assert (await client.get("/readyz")).status_code == 200

        # 두 레인과 종료 훅은 종료 코디네이터가 함께 멈춘다(2단계 3.6).
        coordinator = app.state.shutdown
        coordinator.request_shutdown()
        assert await asyncio.to_thread(coordinator.finished.wait, 5)
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "db": "error", "runner": "stopped"}


async def test_rejects_untrusted_host(settings: Settings) -> None:
    async with running(create_app(settings)) as client:
        rejected = await client.get("/healthz", headers={"Host": "evil.example"})
        allowed = await client.get("/healthz", headers={"Host": "localhost:8500"})

    assert rejected.status_code == 400
    assert allowed.status_code == 200


async def test_unknown_api_path_is_json_404(settings: Settings) -> None:
    async with running(create_app(settings)) as client:
        response = await client.get("/api/nope")

    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


async def test_startup_failure_still_closes_database(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_recover(db: object, supervisor: object, bus: object) -> None:
        raise RuntimeError("recovery failed")

    monkeypatch.setattr(recovery, "recover", broken_recover)
    app = create_app(settings)

    with pytest.raises(RuntimeError, match="recovery failed"):
        async with app.router.lifespan_context(app):
            pass

    assert (settings.data_dir / "app.db").is_file()
    assert not (settings.data_dir / "app.db-wal").exists()


async def test_startup_creates_data_dir_and_shutdown_leaves_no_wal(settings: Settings) -> None:
    app = create_app(settings)
    async with running(app):
        async with app.state.db.write() as session:
            await session.execute(text("CREATE TABLE t (x INTEGER)"))
        assert (settings.data_dir / "app.db-wal").exists()

    for name in ("storage", "workspaces", "cache/pip", "logs"):
        assert (settings.data_dir / name).is_dir()
    assert (settings.data_dir / "app.db").is_file()
    assert not (settings.data_dir / "app.db-wal").exists()
    with closing(sqlite3.connect(settings.data_dir / "app.db")) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)


async def test_startup_wires_sandbox_llm_and_default_allowlist(settings: Settings) -> None:
    app = create_app(settings)
    async with running(app) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert isinstance(app.state.sandbox, LocalWorkspaceBackend)
        assert isinstance(app.state.llm, GeminiAdapter)

    allowlist_file = settings.data_dir / "allowlist.txt"
    assert allowlist_file.read_text(encoding="utf-8") == allowlist.DEFAULT_FILE_TEXT


async def test_startup_keeps_an_existing_allowlist_at_the_configured_path(
    settings: Settings, tmp_path: Path
) -> None:
    custom = tmp_path / "conf" / "allow.txt"
    custom.parent.mkdir()
    custom.write_text("example.com\n", encoding="utf-8")
    configured = settings.model_copy(update={"http_allowlist_file": custom})

    async with running(create_app(configured)):
        pass

    assert custom.read_text(encoding="utf-8") == "example.com\n"
    assert not (settings.data_dir / "allowlist.txt").exists()


async def test_shutdown_stops_housekeeping_then_closes_sandbox_llm_and_db(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def spy(name: str, original: Callable[[Any], Awaitable[None]]) -> Callable[..., Any]:
        async def wrapper(self: Any) -> None:
            calls.append(name)
            await original(self)

        return wrapper

    monkeypatch.setattr(Housekeeping, "stop", spy("housekeeping", Housekeeping.stop))
    monkeypatch.setattr(
        LocalWorkspaceBackend, "aclose", spy("sandbox", LocalWorkspaceBackend.aclose)
    )
    monkeypatch.setattr(GeminiAdapter, "aclose", spy("llm", GeminiAdapter.aclose))
    monkeypatch.setattr(Database, "close", spy("db", Database.close))

    async with running(create_app(settings)):
        pass

    # 샌드박스 백엔드는 둘이다(에이전트 작업공간, 워크플로우 런 작업 폴더).
    assert calls == ["housekeeping", "sandbox", "sandbox", "llm", "db"]


async def test_e2b_backend_stops_startup(settings: Settings) -> None:
    e2b = settings.model_copy(update={"sandbox_backend": "e2b"})
    app = create_app(e2b)

    with pytest.raises(NotImplementedError, match=E2B_NOT_IMPLEMENTED):
        async with app.router.lifespan_context(app):
            pass


def test_create_app_masks_configured_keys_in_the_log_file(settings: Settings) -> None:
    # 형식 패턴(AIza·AQ.)에 걸리지 않는 값이어도 설정에 있으면 로그에서 지워진다.
    key = "plain-key-" + "9" * 24
    configured = settings.model_copy(update={"gemini_api_keys": [SecretStr(key)]})
    create_app(configured)

    structlog.get_logger("test").warning("leak.check", detail=f"before {key} after")
    for handler in logging.getLogger().handlers:
        handler.flush()

    text = (settings.data_dir / "logs" / "app.log").read_text(encoding="utf-8")
    assert key not in text
    assert "before *** after" in text
