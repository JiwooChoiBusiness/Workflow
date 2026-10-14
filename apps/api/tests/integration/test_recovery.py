"""종료 중인 런과 기동 복구(설계 2.6, 10장 M6). 같은 DATA_DIR로 서버를 두 번 띄운다."""

import asyncio
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app import sysinfo
from app.config import Settings
from app.db.engine import Database
from app.db.models import Run, WorkspaceInstance
from app.db.repositories.runs import RunRepository
from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text, fake_tool_call
from app.logsetup import configure_logging
from app.main import create_app

BASE_URL = "http://127.0.0.1:8500"
EMAIL = "owner@example.com"
PASSWORD = "correct-horse-battery"  # noqa: S105  테스트 계정
SLEEP = "Start-Sleep 30" if sys.platform == "win32" else "sleep 30"
Events = Callable[[str], list[dict[str, Any]]]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@asynccontextmanager
async def serve(
    settings: Settings, script: Any, monkeypatch: pytest.MonkeyPatch, *, first: bool = False
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    application = create_app(settings)
    application.state.llm = FakeAdapter([script] * 1000)
    try:
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url=BASE_URL
            ) as client:
                if first:
                    setup = await client.post(
                        "/api/console/setup", json={"email": EMAIL, "password": PASSWORD}
                    )
                    assert setup.status_code == 201, setup.text
                login = await client.post(
                    "/api/console/auth/login", json={"email": EMAIL, "password": PASSWORD}
                )
                assert login.status_code == 200, login.text
                yield application, client
    finally:
        configure_logging("INFO")


async def wait_status(client: AsyncClient, run_id: str, status: str, timeout: float = 15) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = (await client.get(f"/api/console/runs/{run_id}")).json()
        if run["status"] == status:
            return run
        await asyncio.sleep(0.05)
    raise AssertionError(f"런이 {status}가 되지 않았다")


async def test_restart_recovers_running_queued_and_busy_workspaces(
    settings: Settings,
    script: Any,
    make_session: Any,
    sse: Events,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with serve(settings, script, monkeypatch, first=True) as (app, client):
        workspace_id = uuid.UUID(
            (await client.get("/api/console/auth/me")).json()["workspace"]["id"]
        )
        _, waiting_session = await make_session(client)
        _, crashed_session = await make_session(client)
        _, queued_session = await make_session(client)
        script.add([*fake_tool_call("shell", {"command": "echo x"}), fake_end("tool_use")])
        waiting = sse(
            (
                await client.post(
                    f"/api/console/sessions/{waiting_session}/messages", json={"text": "승인 대기"}
                )
            ).text
        )
        script.add(fake_text("첫 답"))
        crashed = sse(
            (
                await client.post(
                    f"/api/console/sessions/{crashed_session}/messages", json={"text": "곧 끊긴다"}
                )
            ).text
        )
        script.add(fake_text("둘째 답"))
        queued = sse(
            (
                await client.post(
                    f"/api/console/sessions/{queued_session}/messages", json={"text": "다시 돈다"}
                )
            ).text
        )
    waiting_id, crashed_id, queued_id = (
        uuid.UUID(events[0]["run_id"]) for events in (waiting, crashed, queued)
    )

    # 프로세스가 런 도중 죽은 것처럼 DB를 바꾼다.
    db = Database(settings.data_dir / "app.db")
    try:
        async with db.write() as session:
            await session.execute(
                update(Run).where(Run.id == crashed_id).values(status="running", ended_at=None)
            )
            await session.execute(
                update(Run)
                .where(Run.id == queued_id)
                .values(status="queued", ended_at=None, started_at=None, steps=0)
            )
            await session.execute(
                update(WorkspaceInstance)
                .where(WorkspaceInstance.session_id == uuid.UUID(crashed_session))
                .values(status="busy")
            )
    finally:
        await db.close()

    script.add(fake_text("복구 뒤 다시 실행했다"))
    async with serve(settings, script, monkeypatch) as (app, client):
        failed = (await client.get(f"/api/console/runs/{crashed_id}")).json()
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "shutdown"
        events = (await client.get(f"/api/console/runs/{crashed_id}/events")).json()["items"]
        assert [event["type"] for event in events][-2:] == ["run.error", "run.completed"]
        assert events[-1]["data"]["status"] == "failed"
        assert events[-1]["data"]["error"]["code"] == "shutdown"
        assert events[-1]["seq"] == crashed[-1]["seq"] + 2

        rerun = await wait_status(client, str(queued_id), "succeeded")
        assert rerun["steps"] == 1
        replay = sse(
            (
                await client.get(
                    f"/api/console/sessions/{queued_session}/events",
                    params={"after": queued[-1]["seq"]},
                )
            ).text
        )
        assert replay[0]["type"] == "run.queued"
        assert replay[-1]["data"]["status"] == "succeeded"

        still = (await client.get(f"/api/console/runs/{waiting_id}")).json()
        assert still["status"] == "waiting_input"
        assert still["pending_interaction"]["kind"] == "approval"

        workspaces = (await client.get("/api/console/workspaces")).json()["items"]
        assert {item["status"] for item in workspaces} == {"ready"}
        assert app.state.supervisor.stats()["queued"] == 0
        async with app.state.db.read() as session:
            runs = await RunRepository(session, workspace_id).list_by_status(["running"])
        assert runs == []


async def test_shutdown_during_a_run_fails_it_and_keeps_the_workspace(
    settings: Settings,
    script: Any,
    make_session: Any,
    sse: Events,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script.add(
        [
            *fake_tool_call("shell", {"command": SLEEP, "timeout_sec": 60}, call_id="call_sleep"),
            fake_end("tool_use"),
        ]
    )
    async with serve(settings, script, monkeypatch, first=True) as (app, client):
        _, session_id = await make_session(client, approval_mode="off")
        stream = asyncio.create_task(
            client.post(
                f"/api/console/sessions/{session_id}/messages", json={"text": "오래 걸린다"}
            )
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = (await client.get(f"/api/console/sessions/{session_id}/workspace")).json()
            if status["running"]:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("도구가 뜨지 않았다")
        started = time.monotonic()
        # 서버 종료(Ctrl+C·Ctrl+Break)와 같은 경로. 유예 안에 런이 failed(shutdown)으로 끝난다.
        await app.state.supervisor.shutdown()
        response = await asyncio.wait_for(stream, 10)
        elapsed = time.monotonic() - started

    events = sse(response.text)
    assert [event["type"] for event in events][-2:] == ["run.error", "run.completed"]
    assert events[-2]["data"] == {
        "code": "shutdown",
        "message": "서버가 종료되어 런을 멈췄다",
        "retryable": True,
    }
    assert events[-1]["data"]["status"] == "failed"
    assert elapsed < 10
    folder = settings.data_dir / "workspaces" / session_id
    assert folder.is_dir()
    assert not (settings.data_dir / "app.db-wal").exists()

    async with serve(settings, script, monkeypatch) as (app, client):
        run = (await client.get(f"/api/console/runs/{events[0]['run_id']}")).json()
        assert run["status"] == "failed" and run["error"]["code"] == "shutdown"
        status = (await client.get(f"/api/console/sessions/{session_id}/workspace")).json()
        assert status["exists"] is True
        assert status["instance"]["status"] == "ready"
