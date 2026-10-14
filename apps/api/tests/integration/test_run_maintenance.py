"""하우스키핑의 런 정리.

waiting_input TTL(3.10), run_event·tool-outputs 보존 정리(5.4), 서버 종료로 남은 ephemeral 과업
작업공간(3.3).
"""

import sys
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, update

from app.agent.task_service import AgentTaskSpec
from app.config import Settings
from app.db.engine import Database
from app.db.models import ChatSession, Run, WorkspaceInstance
from app.db.models.enums import RunStatus
from app.db.repositories.runs import RunRepository
from app.db.types import utcnow
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.services.runs import RunMaintenance, RunService
from app.services.tasks import TaskService

Events = Callable[[str], list[dict[str, Any]]]
BIG = (
    "1..4000 | ForEach-Object { 'x' * 100 }"
    if sys.platform == "win32"
    else "for i in $(seq 1 4000); do printf '%0100d\n' 0; done"
)


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


def maintenance(app: FastAPI) -> RunMaintenance:
    state = app.state
    return RunMaintenance(state.db, state.settings, bus=state.bus, storage=state.storage)


async def test_waiting_input_expires_after_ttl(
    app: FastAPI, logged_in: AsyncClient, make_session: Any, script: Any, sse: Events, db: Database
) -> None:
    _, session_id = await make_session(logged_in)
    script.add([*fake_tool_call("shell", {"command": "echo x"}), fake_end("tool_use")])
    events = sse(
        (
            await logged_in.post(
                f"/api/console/sessions/{session_id}/messages", json={"text": "대기"}
            )
        ).text
    )
    run_id = uuid.UUID(events[0]["run_id"])
    assert await maintenance(app).expire_waiting() == 0

    workspace_id = uuid.UUID(
        (await logged_in.get("/api/console/auth/me")).json()["workspace"]["id"]
    )
    async with db.write() as session:
        run = await RunRepository(session, workspace_id).get(run_id)
        assert run is not None and run.pending_interaction is not None
        old = (utcnow() - timedelta(hours=25)).isoformat()
        run.pending_interaction = {**run.pending_interaction, "created_at": old}

    assert await maintenance(app).expire_waiting() == 1

    detail = (await logged_in.get(f"/api/console/runs/{run_id}")).json()
    assert detail["status"] == "cancelled"
    assert detail["error"]["code"] == "input_timeout"
    persisted = (await logged_in.get(f"/api/console/runs/{run_id}/events")).json()["items"]
    assert persisted[-1]["type"] == "run.completed"
    assert persisted[-1]["data"]["status"] == "cancelled"
    assert persisted[-1]["data"]["error"]["code"] == "input_timeout"
    # 취소라 run.error는 없다. run.error 뒤에는 언제나 failed가 온다는 규칙을 지킨다.
    assert "run.error" not in [event["type"] for event in persisted]
    late = await logged_in.post(f"/api/console/runs/{run_id}/input", json={"decision": "approve"})
    assert late.status_code == 409


async def test_old_run_events_and_tool_outputs_are_purged(
    app: FastAPI,
    logged_in: AsyncClient,
    make_session: Any,
    script: Any,
    sse: Events,
    db: Database,
    settings: Settings,
) -> None:
    _, session_id = await make_session(logged_in, approval_mode="off")
    script.add(
        [*fake_tool_call("shell", {"command": BIG}, call_id="call_big"), fake_end("tool_use")],
        fake_text("많다"),
    )
    events = sse(
        (
            await logged_in.post(
                f"/api/console/sessions/{session_id}/messages", json={"text": "출력"}
            )
        ).text
    )
    run_id = uuid.UUID(events[0]["run_id"])
    tool = next(event for event in events if event["type"] == "tool_call.completed")["data"]
    output = settings.data_dir / "storage" / tool["output_storage_key"]
    assert output.is_file()
    assert await maintenance(app).purge_events() == 0

    async with db.write() as session:
        await session.execute(
            update(Run).where(Run.id == run_id).values(ended_at=utcnow() - timedelta(days=91))
        )

    assert await maintenance(app).purge_events() == 1

    assert (await logged_in.get(f"/api/console/runs/{run_id}/events")).json()["items"] == []
    assert not output.exists()
    # 메시지와 런 요약은 남는다.
    assert (await logged_in.get(f"/api/console/runs/{run_id}")).json()["status"] == "succeeded"
    messages = (await logged_in.get(f"/api/console/sessions/{session_id}/messages")).json()
    assert len(messages["items"]) == 3


async def send(client: AsyncClient, session_id: str, text: str, sse: Events) -> uuid.UUID:
    path = f"/api/console/sessions/{session_id}/messages"
    response = await client.post(path, json={"text": text})
    return uuid.UUID(sse(response.text)[0]["run_id"])


async def test_tool_output_folders_without_a_live_run_are_removed(
    app: FastAPI,
    logged_in: AsyncClient,
    make_session: Any,
    script: Any,
    sse: Events,
    db: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, finished_session = await make_session(logged_in, approval_mode="off")
    _, waiting_session = await make_session(logged_in)  # 승인 모드 all_shell
    script.add(
        [*fake_tool_call("shell", {"command": BIG}, call_id="call_big"), fake_end("tool_use")],
        fake_text("많다"),
        [*fake_tool_call("shell", {"command": "echo x"}), fake_end("tool_use")],
    )
    finished = await send(logged_in, finished_session, "출력", sse)
    waiting = await send(logged_in, waiting_session, "대기", sse)
    outputs = settings.data_dir / "storage" / "tool-outputs"
    big = outputs / str(finished) / "call_big.txt"
    assert big.is_file()
    unfinished = outputs / str(waiting)
    unfinished.mkdir()
    (unfinished / "call_wait.txt").write_text("partial", encoding="utf-8")
    orphan = outputs / str(uuid.uuid4())
    orphan.mkdir()
    (orphan / "call_gone.txt").write_text("gone", encoding="utf-8")
    foreign = outputs / "not-a-run"
    foreign.mkdir()

    # 런 행이 없는 폴더만 지운다. 보존 기간 안의 런, 끝나지 않은 런, 런 id가 아닌 이름은 둔다.
    assert await maintenance(app).purge_tool_outputs() == 1
    assert not orphan.exists()
    assert big.is_file() and unfinished.is_dir() and foreign.is_dir()

    # 보존 기간이 지나 이벤트는 지웠는데 백신이 잡고 있어 파일 삭제만 실패한 상태.
    async with db.write() as session:
        await session.execute(
            update(Run).where(Run.id == finished).values(ended_at=utcnow() - timedelta(days=91))
        )

    async def locked(key: str) -> bool:
        raise PermissionError(f"잠김: {key}")

    with monkeypatch.context() as patch:
        patch.setattr(app.state.storage, "delete", locked)
        assert await maintenance(app).purge_events() == 1
    assert big.is_file()

    assert await maintenance(app).purge_tool_outputs() == 1
    assert not big.parent.exists()
    assert unfinished.is_dir() and foreign.is_dir()


async def publish_agent(client: AsyncClient) -> uuid.UUID:
    response = await client.post("/api/console/agents", json={"name": "Task"})
    assert response.status_code == 201, response.text
    agent_id = response.json()["id"]
    published = await client.post(f"/api/console/agents/{agent_id}/publish", json={})
    assert published.status_code == 201, published.text
    return uuid.UUID(agent_id)


def task_service(app: FastAPI) -> TaskService:
    state = app.state
    runs = RunService(
        state.db,
        state.settings,
        bus=state.bus,
        supervisor=state.supervisor,
        storage=state.storage,
        backend=state.sandbox,
    )
    return TaskService(state.db, state.settings, runs=runs, bus=state.bus)


async def test_ephemeral_task_workspaces_left_by_a_shutdown_are_released(
    app: FastAPI, logged_in: AsyncClient, script: Any, db: Database, settings: Settings
) -> None:
    agent_id = await publish_agent(logged_in)  # 승인 모드 all_shell
    workspace_id = uuid.UUID(
        (await logged_in.get("/api/console/auth/me")).json()["workspace"]["id"]
    )
    tasks = task_service(app)
    script.add(
        [*fake_tool_call("shell", {"command": "echo x"}), fake_end("tool_use")],
        fake_text("작업공간을 남긴다"),
    )
    ephemeral = await tasks.wait(
        await tasks.start(
            AgentTaskSpec(workspace_id=workspace_id, agent_id=agent_id, instruction="셸을 쓴다")
        ),
        timeout=30,
    )
    reused = await tasks.wait(
        await tasks.start(
            AgentTaskSpec(
                workspace_id=workspace_id,
                agent_id=agent_id,
                instruction="이어 쓴다",
                session_mode="reuse",
            )
        ),
        timeout=30,
    )
    workspaces = settings.data_dir / "workspaces"
    assert (ephemeral.status, reused.status) == ("waiting_input", "succeeded")
    assert (workspaces / str(ephemeral.session_id)).is_dir()

    # 승인을 기다리는 런이 있으면 ephemeral이어도 건드리지 않는다.
    assert await maintenance(app).release_task_workspaces() == 0
    assert (workspaces / str(ephemeral.session_id)).is_dir()

    # 서버 종료로 끝난 런과 같은 상태로 만든다. 러너는 종료 중에는 작업공간을 지우지 않는다.
    async with db.write() as session:
        run = await RunRepository(session, workspace_id).get(ephemeral.run_id)
        assert run is not None
        run.status = RunStatus.FAILED
        run.error = {"code": "shutdown", "message": "서버 종료", "retryable": True}
        run.pending_interaction = None
        run.ended_at = utcnow()

    assert await maintenance(app).release_task_workspaces() == 1
    assert await maintenance(app).release_task_workspaces() == 0

    assert not (workspaces / str(ephemeral.session_id)).exists()
    assert (workspaces / str(reused.session_id)).is_dir()
    async with db.read() as session:
        instances = set(await session.scalars(select(WorkspaceInstance.session_id)))
        chat = await session.scalar(
            select(ChatSession).where(ChatSession.id == ephemeral.session_id)
        )
    assert instances == {reused.session_id}
    assert chat is not None and chat.workspace_instance_id is None
