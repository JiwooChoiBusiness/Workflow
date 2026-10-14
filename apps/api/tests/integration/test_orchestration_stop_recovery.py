"""하위 에이전트 호출의 중지·재기동 복구·만료·reconcile(오케스트레이션 3.6, 3.8, O-A3·O-A4).

재기동은 같은 DATA_DIR로 서버를 두 번 띄워 본다(test_recovery.py와 같은 방식).
"""

import asyncio
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app import sysinfo
from app.config import Settings
from app.db.engine import Database
from app.db.models import Run, RunEvent
from app.db.types import utcnow
from app.llm.fake_adapter import FakeAdapter, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ModelEvent, ToolResultBlock
from app.logsetup import configure_logging
from app.main import create_app
from app.services.child_runs import ChildRunService
from app.services.runs import RunMaintenance, RunService

BASE_URL = "http://127.0.0.1:8500"
EMAIL = "owner@example.com"
PASSWORD = "correct-horse-battery"  # noqa: S105  테스트 계정
SLEEP = "Start-Sleep 30" if sys.platform == "win32" else "sleep 30"
DRAFT_FIELDS = (
    "instructions",
    "model",
    "tools",
    "limits",
    "sandbox",
    "approval_mode",
    "skill_version_ids",
    "file_ids",
    "sub_agents",
    "updated_at",
)
Events = Callable[[str], list[dict[str, Any]]]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


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


async def make_agent(
    client: AsyncClient,
    name: str,
    slug: str,
    *,
    publish: bool = True,
    **draft: Any,
) -> dict[str, Any]:
    created = await client.post(
        "/api/console/agents", json={"name": name, "slug": slug, "description": f"{name} 설명"}
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    if draft:
        current = (await client.get(f"/api/console/agents/{agent['id']}/draft")).json()
        body = {key: current[key] for key in DRAFT_FIELDS}
        body["env_vars"] = []
        body.update(draft)
        saved = await client.put(f"/api/console/agents/{agent['id']}/draft", json=body)
        assert saved.status_code == 200, saved.text
    if publish:
        published = await client.post(f"/api/console/agents/{agent['id']}/publish", json={})
        assert published.status_code == 201, published.text
    return agent


def ref(agent: dict[str, Any]) -> dict[str, Any]:
    return {"agent_id": agent["id"]}


async def conductor_session(client: AsyncClient, *children: dict[str, Any]) -> str:
    conductor = await make_agent(
        client, "지휘자", "conductor", publish=False, sub_agents=[ref(item) for item in children]
    )
    response = await client.post(f"/api/console/agents/{conductor['id']}/preview/sessions", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def wait_for(check: Callable[[], Any], timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("조건이 시간 안에 맞지 않았다")


async def run_of(client: AsyncClient, run_id: str) -> dict[str, Any]:
    return (await client.get(f"/api/console/runs/{run_id}")).json()


async def status_is(client: AsyncClient, run_id: str, status: str) -> dict[str, Any]:
    async def check() -> Any:
        run = await run_of(client, run_id)
        return run if run["status"] == status else None

    return await wait_for(check)


async def waiting_child(client: AsyncClient, session_id: str) -> dict[str, Any]:
    """부모 세션의 active_run.child이고 그 자식이 승인을 기다리면 (부모, 자식)."""

    async def check() -> Any:
        detail = (await client.get(f"/api/console/sessions/{session_id}")).json()
        active = detail.get("active_run") or {}
        child = active.get("child")
        if child is None:
            return None
        run = await run_of(client, child["run_id"])
        return {"parent": active, "child": run} if run["status"] == "waiting_input" else None

    return await wait_for(check)


def results_of(request: ChatRequest) -> dict[str, ToolResultBlock]:
    return {
        block.tool_use_id: block
        for message in request.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    }


def call(agent: str, call_id: str, task: str = "일") -> list[ModelEvent]:
    return fake_tool_call("call_agent", {"agent": agent, "task": task}, call_id=call_id)


def shell(command: str, call_id: str, **extra: Any) -> list[ModelEvent]:
    return fake_tool_call("shell", {"command": command, **extra}, call_id=call_id)


async def test_stopping_the_parent_cancels_the_waiting_subtree(
    logged_in: AsyncClient, app: FastAPI, script: Any, sse: Events
) -> None:
    client = logged_in
    shellbot = await make_agent(client, "셸", "shellbot", approval_mode="all_shell")
    middle = await make_agent(client, "중간", "middle", sub_agents=[ref(shellbot)])
    session_id = await conductor_session(client, middle)
    script.add(call("middle", "c1"), call("shellbot", "m1"), shell("echo hi", "s1"))
    stream = asyncio.create_task(
        client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "깊게"})
    )

    async def grandchild_waiting() -> Any:
        detail = (await client.get(f"/api/console/sessions/{session_id}")).json()
        child = (detail.get("active_run") or {}).get("child")
        if child is None:
            return None
        middle_run = await run_of(client, child["run_id"])
        grand = middle_run.get("child")
        if grand is None:
            return None
        grand_run = await run_of(client, grand["run_id"])
        if grand_run["status"] != "waiting_input":
            return None
        return detail["active_run"], middle_run, grand_run

    parent, middle_run, grand_run = await wait_for(grandchild_waiting)
    assert middle_run["status"] == "running" and middle_run["depth"] == 1
    assert grand_run["depth"] == 2

    stopped = await client.post(f"/api/console/runs/{parent['id']}/stop", json={})

    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
    events = sse((await asyncio.wait_for(stream, 10)).text)
    assert events[-1]["type"] == "run.completed" and events[-1]["data"]["status"] == "cancelled"
    assert (await run_of(client, middle_run["id"]))["status"] == "cancelled"
    assert (await run_of(client, grand_run["id"]))["status"] == "cancelled"
    # 끝난 부모를 자식의 종료가 다시 깨우지 않는다.
    assert (await run_of(client, parent["id"]))["status"] == "cancelled"
    assert app.state.supervisor.stats()["queued"] == 0


async def test_stopping_the_child_resumes_the_parent_with_a_cancelled_result(
    logged_in: AsyncClient, app: FastAPI, script: Any, sse: Events
) -> None:
    client = logged_in
    shellbot = await make_agent(client, "셸", "shellbot", approval_mode="all_shell")
    session_id = await conductor_session(client, shellbot)
    seen: list[ChatRequest] = []

    def final(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return fake_text("하위 에이전트가 중지되어 직접 했다")

    script.add(call("shellbot", "c1"), shell("echo hi", "s1"), final)
    stream = asyncio.create_task(
        client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "해"})
    )
    found = await waiting_child(client, session_id)

    await client.post(f"/api/console/runs/{found['child']['id']}/stop", json={})

    events = sse((await asyncio.wait_for(stream, 10)).text)
    assert events[-1]["data"]["status"] == "succeeded"
    completed = next(e["data"] for e in events if e["type"] == "child_run.completed")
    assert completed["status"] == "cancelled"
    result = results_of(seen[0])["c1"]
    assert result.is_error and "취소: cancelled" in result.content


async def test_stop_at_the_pausing_step_closes_the_child_without_running_it(
    logged_in: AsyncClient,
    app: FastAPI,
    script: Any,
    sse: Events,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = logged_in
    researcher = await make_agent(client, "조사", "researcher")
    session_id = await conductor_session(client, researcher)
    state = app.state
    runs = RunService(
        state.db,
        state.settings,
        bus=state.bus,
        supervisor=state.supervisor,
        storage=state.storage,
        backend=state.sandbox,
    )
    original = ChildRunService.create_child
    created: list[Any] = []

    async def create_then_stop(self: ChildRunService, parent: Any, call_input: Any) -> Any:
        started = await original(self, parent, call_input)
        created.append(started)
        # 자식 행을 만든 바로 그 스텝에 중지가 온다. 부모는 아직 running이다(3.6의 둘째 줄).
        await runs.stop(parent.workspace_id, parent.run_id)
        return started

    monkeypatch.setattr(ChildRunService, "create_child", create_then_stop)
    script.add(call("researcher", "c1"), fake_text("자식은 돌면 안 된다"))

    events = sse(
        (await client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "x"})).text
    )

    assert events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["status"] == "cancelled"
    child_id = str(created[0].child_run_id)
    child = await run_of(client, child_id)
    assert child["status"] == "cancelled" and child["error"]["code"] == "orphaned"
    assert child["started_at"] is None
    await state.bus.flush()
    async with state.db.read() as session:
        types = (
            await session.execute(
                select(RunEvent.type).where(RunEvent.run_id == uuid.UUID(child_id))
            )
        ).scalars()
        child_types = list(types)
    assert "run.started" not in child_types and "run.queued" not in child_types
    assert state.supervisor.stats()["queued"] == 0
    # 자식 턴은 쓰지 않았다.
    assert len(script.turns) == 1


async def test_child_input_timeout_resumes_the_parent(
    logged_in: AsyncClient, app: FastAPI, script: Any, sse: Events
) -> None:
    client = logged_in
    shellbot = await make_agent(client, "셸", "shellbot", approval_mode="all_shell")
    session_id = await conductor_session(client, shellbot)
    seen: list[ChatRequest] = []

    def final(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return fake_text("시간이 지나 직접 했다")

    script.add(call("shellbot", "c1"), shell("echo hi", "s1"), final)
    stream = asyncio.create_task(
        client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "해"})
    )
    found = await waiting_child(client, session_id)
    state = app.state
    later = utcnow() + timedelta(hours=state.settings.waiting_input_ttl_hours + 1)
    maintenance = RunMaintenance(
        state.db,
        state.settings,
        bus=state.bus,
        storage=state.storage,
        supervisor=state.supervisor,
        clock=lambda: later,
    )

    expired = await maintenance.expire_waiting()

    # 자식만 만료되고 하위 호출을 기다리는 부모는 건너뛴다(3.8).
    assert expired == 1
    child = await run_of(client, found["child"]["id"])
    assert child["status"] == "cancelled" and child["error"]["code"] == "input_timeout"
    events = sse((await asyncio.wait_for(stream, 10)).text)
    assert events[-1]["data"]["status"] == "succeeded"
    result = results_of(seen[0])["c1"]
    assert result.is_error and "input_timeout" in result.content


async def test_reconcile_requeues_a_parent_whose_child_already_ended(
    logged_in: AsyncClient,
    app: FastAPI,
    script: Any,
    sse: Events,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = logged_in
    researcher = await make_agent(client, "조사", "researcher")
    session_id = await conductor_session(client, researcher)

    async def no_launch(self: ChildRunService, ref: Any) -> None:
        return None

    monkeypatch.setattr(ChildRunService, "launch_child", no_launch)
    script.add(call("researcher", "c1"), fake_text("부모 끝"))
    stream = asyncio.create_task(
        client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "x"})
    )

    async def parked() -> Any:
        detail = (await client.get(f"/api/console/sessions/{session_id}")).json()
        return (detail.get("active_run") or {}).get("child")

    child = await wait_for(parked)
    # 훅을 부르기 전에 프로세스가 죽은 것처럼 자식만 끝난 상태로 둔다.
    async with app.state.db.write() as session:
        await session.execute(
            update(Run)
            .where(Run.id == uuid.UUID(child["run_id"]))
            .values(status="succeeded", ended_at=utcnow())
        )
    state = app.state
    maintenance = RunMaintenance(
        state.db, state.settings, bus=state.bus, storage=state.storage, supervisor=state.supervisor
    )

    assert await maintenance.reconcile_children() == 1
    assert await maintenance.reconcile_children() == 0

    events = sse((await asyncio.wait_for(stream, 10)).text)
    assert events[-1]["data"]["status"] == "succeeded"
    assert [e["type"] for e in events].count("child_run.completed") == 1


async def test_child_queued_at_restart_runs_after_recovery(
    settings: Settings, script: Any, sse: Events, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_launch(self: ChildRunService, ref: Any) -> None:
        return None

    with monkeypatch.context() as patch:
        patch.setattr(ChildRunService, "launch_child", no_launch)
        async with serve(settings, script, monkeypatch, first=True) as (app, client):
            researcher = await make_agent(client, "조사", "researcher")
            session_id = await conductor_session(client, researcher)
            script.add(call("researcher", "c1"))
            stream = asyncio.create_task(
                client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "x"})
            )

            async def parked() -> Any:
                detail = (await client.get(f"/api/console/sessions/{session_id}")).json()
                return (detail.get("active_run") or {}).get("child")

            child = await wait_for(parked)
            parent_id = (await client.get(f"/api/console/sessions/{session_id}")).json()[
                "active_run"
            ]["id"]
            assert (await run_of(client, child["run_id"]))["status"] == "queued"
            stream.cancel()

    script.add(fake_text("자식 결과"), fake_text("부모 끝"))
    async with serve(settings, script, monkeypatch) as (app, client):
        # 부모는 대기 그대로, 자식은 복구가 다시 넣어 끝까지 간다(O-A4).
        done = await status_is(client, parent_id, "succeeded")
        assert done["child"] is None
        assert (await run_of(client, child["run_id"]))["status"] == "succeeded"


async def test_child_running_at_shutdown_fails_and_the_parent_carries_on(
    settings: Settings, script: Any, sse: Events, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[ChatRequest] = []

    def final(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return fake_text("하위 에이전트가 종료로 멈춰 직접 마쳤다")

    async with serve(settings, script, monkeypatch, first=True) as (app, client):
        researcher = await make_agent(client, "조사", "researcher", approval_mode="off")
        session_id = await conductor_session(client, researcher)
        script.add(call("researcher", "c1"), shell(SLEEP, "s1", timeout_sec=60))
        stream = asyncio.create_task(
            client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "x"})
        )

        async def child_running() -> Any:
            detail = (await client.get(f"/api/console/sessions/{session_id}")).json()
            child = (detail.get("active_run") or {}).get("child")
            if child is None:
                return None
            status = (
                await client.get(f"/api/console/sessions/{child['session_id']}/workspace")
            ).json()
            return (detail["active_run"]["id"], child) if status.get("running") else None

        parent_id, child = await wait_for(child_running)
        # 서버 종료(Ctrl+C) 경로. 자식은 failed(shutdown)이고 그 끝이 부모를 queued로 돌리지만
        # 레인이 닫혀 투입은 건너뛴다(3.6).
        await app.state.shutdown.shutdown()
        stream.cancel()

    db = Database(settings.data_dir / "app.db")
    try:
        async with db.read() as session:
            parent = await session.get(Run, uuid.UUID(parent_id))
            failed_child = await session.get(Run, uuid.UUID(child["run_id"]))
            assert parent is not None and failed_child is not None
            assert failed_child.status == "failed"
            assert failed_child.error is not None and failed_child.error["code"] == "shutdown"
            assert parent.status == "queued"
            assert parent.resume_payload is not None
            assert parent.resume_payload["kind"] == "agent_call"
            assert parent.resume_payload["child"]["status"] == "failed"
    finally:
        await db.close()

    script.add(final)
    async with serve(settings, script, monkeypatch) as (app, client):
        await status_is(client, parent_id, "succeeded")
    result = results_of(seen[0])["c1"]
    assert result.is_error and "실패: shutdown" in result.content


async def test_crashed_child_fails_on_recovery_and_the_parent_resumes(
    settings: Settings, script: Any, sse: Events, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_launch(self: ChildRunService, ref: Any) -> None:
        return None

    with monkeypatch.context() as patch:
        patch.setattr(ChildRunService, "launch_child", no_launch)
        async with serve(settings, script, monkeypatch, first=True) as (app, client):
            researcher = await make_agent(client, "조사", "researcher")
            session_id = await conductor_session(client, researcher)
            script.add(call("researcher", "c1"))
            stream = asyncio.create_task(
                client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "x"})
            )

            async def parked() -> Any:
                detail = (await client.get(f"/api/console/sessions/{session_id}")).json()
                active = detail.get("active_run") or {}
                return (active["id"], active["child"]) if active.get("child") else None

            parent_id, child = await wait_for(parked)
            stream.cancel()

    # 프로세스가 자식을 실행하던 중에 죽은 것처럼 둔다.
    db = Database(settings.data_dir / "app.db")
    try:
        async with db.write() as session:
            await session.execute(
                update(Run)
                .where(Run.id == uuid.UUID(child["run_id"]))
                .values(status="running", started_at=utcnow())
            )
    finally:
        await db.close()

    seen: list[ChatRequest] = []

    def final(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return fake_text("직접 마쳤다")

    script.add(final)
    async with serve(settings, script, monkeypatch) as (app, client):
        await status_is(client, parent_id, "succeeded")
        crashed = await run_of(client, child["run_id"])
        assert crashed["status"] == "failed" and crashed["error"]["code"] == "shutdown"
        events = (await client.get(f"/api/console/runs/{parent_id}/events")).json()["items"]
        types = [event["type"] for event in events]
        # 재개 부모에는 child_run.completed가 run.queued보다 먼저 온다(3.8의 5번).
        index = types.index("child_run.completed")
        assert types[index + 1] == "run.queued"
    assert "실패: shutdown" in results_of(seen[0])["c1"].content


async def test_parent_session_stream_is_live_after_restart(
    settings: Settings, script: Any, sse: Events, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with serve(settings, script, monkeypatch, first=True) as (app, client):
        shellbot = await make_agent(client, "셸", "shellbot", approval_mode="all_shell")
        session_id = await conductor_session(client, shellbot)
        script.add(call("shellbot", "c1"), shell("echo restart-ok", "s1"))
        stream = asyncio.create_task(
            client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "x"})
        )
        found = await waiting_child(client, session_id)
        stream.cancel()

    script.add(fake_text("자식 끝"), fake_text("부모 끝"))
    async with serve(settings, script, monkeypatch) as (app, client):
        detail = (await client.get(f"/api/console/sessions/{session_id}")).json()
        assert detail["active_run"]["status"] == "running"
        assert detail["active_run"]["child"]["run_id"] == found["child"]["id"]
        # 재기동 뒤 부모 채널이 없어도 열린 런이라 live로 붙는다(6.1). 기록만 주고 닫히지 않는다.
        reader = asyncio.create_task(
            client.get(f"/api/console/sessions/{session_id}/events", params={"after": 0})
        )
        await asyncio.sleep(0.5)
        assert not reader.done()

        approved = await client.post(
            f"/api/console/runs/{found['child']['id']}/input", json={"decision": "approve"}
        )
        assert approved.status_code == 202, approved.text
        events = sse((await asyncio.wait_for(reader, 20)).text)

    types = [event["type"] for event in events]
    assert "child_run.completed" in types
    assert types[-1] == "run.completed" and events[-1]["data"]["status"] == "succeeded"
    assert types.index("child_run.completed") > types.index("child_run.started")
