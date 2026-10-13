"""작업공간 목록과 세션별 관리(설계 3.14, 6.2 GET /workspaces, POST .../workspace/*)."""

import asyncio
import sys
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call

Events = Callable[[str], list[dict[str, Any]]]
SLEEP = "Start-Sleep 30" if sys.platform == "win32" else "sleep 30"


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


async def send(client: AsyncClient, session_id: str, text: str) -> Any:
    response = await client.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": text}
    )
    assert response.status_code == 200, response.text
    return response


async def listed(client: AsyncClient, session_id: str) -> dict[str, Any]:
    body = (await client.get("/api/console/workspaces")).json()
    [item] = [item for item in body["items"] if item["session_id"] == session_id]
    return {**item, "_list": body}


async def wait_listed_running(
    client: AsyncClient, session_id: str, timeout: float = 20
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = (await client.get("/api/console/workspaces")).json()
        for item in body["items"]:
            if item["session_id"] == session_id and item["running"]:
                return item
        await asyncio.sleep(0.1)
    raise AssertionError("목록에 실행 중인 도구가 나오지 않았다")


async def test_list_shows_the_running_tool_and_kill_lets_the_run_go_on(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    agent_id, session_id = await make_session(logged_in, approval_mode="off")
    await logged_in.patch(f"/api/console/agents/{agent_id}", json={"name": "작업 담당"})
    script.add(
        [
            *fake_tool_call("shell", {"command": SLEEP, "timeout_sec": 60}, call_id="call_sleep"),
            fake_end("tool_use"),
        ],
        fake_text("멈춘 것을 확인했다"),
    )

    stream = asyncio.create_task(
        logged_in.post(f"/api/console/sessions/{session_id}/messages", json={"text": "기다려"})
    )
    item = await wait_listed_running(logged_in, session_id)

    assert (item["agent_id"], item["agent_name"]) == (agent_id, "작업 담당")
    assert item["session_source"] == "preview"
    assert item["active_run_status"] == "running"
    [running] = item["running"]
    assert running["exec_id"] == "call_sleep" and running["pid"] > 0
    # Job Object가 있는 Windows에서만 값이 있다(POSIX 폴백은 None).
    assert item["peak_memory_bytes"] == running["peak_memory_bytes"]
    if sys.platform == "win32":
        assert running["peak_memory_bytes"] > 0

    killed = await logged_in.post(f"/api/console/sessions/{session_id}/workspace/kill", json={})
    assert killed.json() == {"killed": ["call_sleep"]}
    events = sse((await asyncio.wait_for(stream, 15)).text)

    tool = next(event for event in events if event["type"] == "tool_call.completed")["data"]
    assert tool["status"] == "error"
    assert "중지" in tool["output_excerpt"]
    # 프로세스만 끝나고 런은 다음 스텝으로 이어진다.
    assert events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["status"] == "succeeded"
    after = await listed(logged_in, session_id)
    assert after["running"] == [] and after["active_run_status"] is None
    assert after["peak_memory_bytes"] is None


async def test_clean_and_reset_then_the_session_goes_on(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events, settings: Settings
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(
        [
            *fake_tool_call("write_file", {"path": "outputs/a.txt", "content": "A" * 2000}),
            fake_end("tool_use"),
        ],
        fake_text("썼다"),
    )
    await send(logged_in, session_id, "써")
    folder = settings.data_dir / "workspaces" / session_id
    first = await listed(logged_in, session_id)
    assert first["disk_bytes"] >= 2000 and first["disk_measured_at"] is not None
    assert first["_list"]["total_disk_bytes"] == first["disk_bytes"]
    assert first["_list"]["disk_limit_bytes"] == settings.workspace_max_bytes

    (folder / ".packages" / "big").mkdir(parents=True)
    (folder / ".packages" / "big" / "blob.bin").write_bytes(b"x" * 50_000)
    (folder / ".cache" / "c.bin").write_bytes(b"y" * 10_000)
    status = (await logged_in.get(f"/api/console/sessions/{session_id}/workspace")).json()
    assert status["disk_bytes"] >= 60_000
    assert ".packages" in [entry["path"] for entry in status["largest_dirs"]]

    cleaned = await logged_in.post(
        f"/api/console/sessions/{session_id}/workspace/clean-packages", json={}
    )
    assert cleaned.status_code == 200
    assert cleaned.json()["disk_bytes"] < 60_000
    assert list((folder / ".packages").iterdir()) == []
    # 다시 잰 크기가 목록에도 반영된다.
    assert (await listed(logged_in, session_id))["disk_bytes"] == cleaned.json()["disk_bytes"]

    reset = await logged_in.post(f"/api/console/sessions/{session_id}/workspace/reset", json={})
    assert reset.status_code == 200
    assert not (folder / "outputs" / "a.txt").exists()
    assert (await listed(logged_in, session_id))["disk_bytes"] == 0

    script.add(fake_text("이어서 한다"))
    events = sse((await send(logged_in, session_id, "계속")).text)
    assert events[-1]["data"]["status"] == "succeeded"
    assert "workspace.recreated" not in [event["type"] for event in events]
    messages = (await logged_in.get(f"/api/console/sessions/{session_id}/messages")).json()
    assert [item["text"] for item in messages["items"] if item["role"] == "user"] == ["써", "계속"]


async def test_over_limit_is_flagged_and_new_runs_are_refused(
    app: FastAPI, logged_in: AsyncClient, make_session: Any, script: Any
) -> None:
    app.state.settings.workspace_max_bytes = 1000
    _, session_id = await make_session(logged_in)
    script.add(
        [
            *fake_tool_call("write_file", {"path": "outputs/big.txt", "content": "B" * 5000}),
            fake_end("tool_use"),
        ],
        fake_text("크게 썼다"),
    )
    await send(logged_in, session_id, "크게 써")

    item = await listed(logged_in, session_id)
    assert item["over_limit"] is True
    assert item["_list"]["disk_limit_bytes"] == 1000
    refused = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "더"}
    )
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "workspace_disk_limit"
    assert "초기화" in error["message"]
    assert error["details"]["limit_bytes"] == 1000

    reset = await logged_in.post(f"/api/console/sessions/{session_id}/workspace/reset", json={})
    assert reset.status_code == 200
    assert (await listed(logged_in, session_id))["over_limit"] is False
    script.add(fake_text("다시 된다"))
    await send(logged_in, session_id, "이제 돼?")


async def test_workspace_routes_need_login(client: AsyncClient, account: dict[str, Any]) -> None:
    assert (await client.get("/api/console/workspaces")).status_code == 401
