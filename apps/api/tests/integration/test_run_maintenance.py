"""하우스키핑의 런 정리: waiting_input TTL(3.10)과 run_event·tool-outputs 보존 정리(5.4)."""

import sys
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update

from app.config import Settings
from app.db.engine import Database
from app.db.models import Run
from app.db.repositories.runs import RunRepository
from app.db.types import utcnow
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.services.runs import RunMaintenance

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
