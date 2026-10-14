"""기동 복구의 워크플로우 부분(runner/recovery.py recover_workflows, 2단계 설계 3.7의 5번).

running 워크플로우 런은 failed(shutdown), 끝난 워크플로우 런의 queued·waiting_input 자식 런은
cancelled(orphaned)이고 이 일이 1단계 복구보다 먼저라 1단계 복구가 그 자식을 다시 넣지 않는다.
"""

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import (
    Agent,
    AgentVersion,
    ChatSession,
    Message,
    Run,
    RunEvent,
    WfNodeRun,
    WfRun,
    WfRunEvent,
    WfVersion,
    Workflow,
)
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.runner import recovery
from app.runner.supervisor import CancelSignal, Supervisor
from app.streaming.event_bus import EventBus
from app.streaming.stores import sqlite_stores


async def never(run_id: str, cancel: CancelSignal) -> None:
    raise AssertionError("복구는 실행하지 않는다")


class Seed:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.workspace_id = new_id()
        self.workflow_id = new_id()
        self.version_id = new_id()
        self.agent_version_id = new_id()
        self.session_id = new_id()
        self.seq = 0

    async def setup(self) -> None:
        async with self.db.write() as session:
            workspace = WorkspaceRepository(session).add(name="w", slug="w", settings={})
            workspace.id = self.workspace_id
            await session.flush()
            agent = Agent(id=new_id(), workspace_id=self.workspace_id, slug="a", name="a")
            session.add(agent)
            await session.flush()
            session.add(
                AgentVersion(
                    id=self.agent_version_id,
                    workspace_id=self.workspace_id,
                    agent_id=agent.id,
                    status="draft",
                    approval_mode="all_shell",
                )
            )
            await session.flush()
            session.add(
                ChatSession(
                    id=self.session_id,
                    workspace_id=self.workspace_id,
                    agent_id=agent.id,
                    agent_version_id=self.agent_version_id,
                    source="task",
                )
            )
            session.add(
                Workflow(
                    id=self.workflow_id,
                    workspace_id=self.workspace_id,
                    slug="wf",
                    name="wf",
                    kind="workflow",
                )
            )
            await session.flush()
            session.add(
                WfVersion(
                    id=self.version_id,
                    workspace_id=self.workspace_id,
                    workflow_id=self.workflow_id,
                    status="draft",
                    graph={},
                    settings={},
                    graph_hash="0" * 64,
                )
            )

    async def child(self, status: str, *, parent: uuid.UUID | None = None) -> uuid.UUID:
        """과업 런. parent를 주면 그 런이 call_agent로 부른 자식(손자)이다."""
        self.seq += 1
        async with self.db.write() as session:
            message = Message(
                id=new_id(),
                workspace_id=self.workspace_id,
                session_id=self.session_id,
                seq=self.seq,
                role="user",
            )
            session.add(message)
            await session.flush()
            run = Run(
                id=new_id(),
                workspace_id=self.workspace_id,
                session_id=self.session_id,
                agent_version_id=self.agent_version_id,
                trigger="task",
                status=status,
                user_message_id=message.id,
                approval_mode="all_shell",
                parent_run_id=parent,
            )
            session.add(run)
            return run.id

    async def wf_run(
        self,
        status: str,
        *,
        child: uuid.UUID | None = None,
        node_status: str = "succeeded",
        pending: bool = False,
    ) -> uuid.UUID:
        run_id = new_id()
        pending_interaction: dict[str, Any] | None = None
        if pending:
            pending_interaction = {
                "node_id": "agent_1",
                "child_run_id": str(child),
                "interaction": {
                    "interaction_id": "int_1",
                    "kind": "approval",
                    "created_at": utcnow().isoformat(),
                },
            }
        async with self.db.write() as session:
            session.add(
                WfRun(
                    id=run_id,
                    workspace_id=self.workspace_id,
                    workflow_id=self.workflow_id,
                    wf_version_id=self.version_id,
                    kind="workflow",
                    trigger="preview",
                    status=status,
                    pending_interaction=pending_interaction,
                    graph_hash="0" * 64,
                )
            )
            await session.flush()
            session.add(
                WfNodeRun(
                    id=new_id(),
                    workspace_id=self.workspace_id,
                    wf_run_id=run_id,
                    node_id="agent_1",
                    node_type="agent",
                    index=0,
                    status=node_status,
                    child_run_id=child,
                    started_at=utcnow(),
                )
            )
        return run_id

    async def status(self, model: type[Run] | type[WfRun], row_id: uuid.UUID) -> Any:
        async with self.db.read() as session:
            row = await session.get(model, row_id)
            assert row is not None
            return row


@pytest.fixture
async def setup(tmp_path: Path) -> AsyncIterator[tuple[Database, EventBus, Seed]]:
    db = Database(tmp_path / "recover.db")
    await upgrade_head(db)
    bus = EventBus(sqlite_stores(db), batch_window_sec=0.01)
    bus.start()
    seed = Seed(db)
    await seed.setup()
    try:
        yield db, bus, seed
    finally:
        await bus.close()
        await db.close()


async def test_running_runs_fail_and_their_waiting_children_are_orphaned(
    setup: tuple[Database, EventBus, Seed],
) -> None:
    db, bus, seed = setup
    queued_child = await seed.child("queued")
    crashed = await seed.wf_run("running", child=queued_child, node_status="running")
    waiting_child = await seed.child("waiting_input")
    finished = await seed.wf_run("failed", child=waiting_child, node_status="failed")
    lane = Supervisor(never, name="workflow")
    agents = Supervisor(never)

    result = await recovery.recover_workflows(db, lane, bus)
    agent_result = await recovery.recover(db, agents, bus)

    assert result == {"failed": 1, "orphaned": 2, "resumed": 0, "requeued": 0}
    run = await seed.status(WfRun, crashed)
    assert run.status == "failed" and run.error["code"] == "shutdown" and run.ended_at
    for child_id in (queued_child, waiting_child):
        child = await seed.status(Run, child_id)
        assert child.status == "cancelled" and child.error["code"] == "orphaned"
    # 1단계 복구가 고아 자식을 다시 넣지 않는다.
    assert agent_result["requeued"] == 0
    assert agents.stats()["queued"] == 0
    assert (await seed.status(WfRun, finished)).status == "failed"
    await bus.flush()
    async with db.read() as session:
        node = await session.scalar(select(WfNodeRun).where(WfNodeRun.wf_run_id == crashed))
        wf_events = list(
            (
                await session.scalars(
                    select(WfRunEvent.type)
                    .where(WfRunEvent.wf_run_id == crashed)
                    .order_by(WfRunEvent.seq)
                )
            ).all()
        )
        child_events = list(
            (
                await session.scalars(select(RunEvent.type).where(RunEvent.run_id == queued_child))
            ).all()
        )
    assert node is not None and node.status == "cancelled"
    assert wf_events == ["workflow.error", "workflow.completed"]
    assert child_events == ["run.completed"]


async def test_grandchildren_of_an_orphaned_conductor_child_are_closed_too(
    setup: tuple[Database, EventBus, Seed],
) -> None:
    db, bus, seed = setup
    # agent 노드가 부른 에이전트가 지휘자라 하위 에이전트를 기다리던 중에 서버가 죽었다.
    conductor = await seed.child("waiting_input")
    grandchild = await seed.child("queued", parent=conductor)
    crashed = await seed.wf_run("running", child=conductor, node_status="running")
    agents = Supervisor(never)

    result = await recovery.recover_workflows(db, Supervisor(never, name="workflow"), bus)
    agent_result = await recovery.recover(db, agents, bus)

    assert result["failed"] == 1 and result["orphaned"] == 2
    assert (await seed.status(WfRun, crashed)).status == "failed"
    for run_id in (conductor, grandchild):
        run = await seed.status(Run, run_id)
        assert run.status == "cancelled" and run.error["code"] == "orphaned"
    # 1단계 복구가 기다릴 부모 없는 손자를 다시 넣지 않는다.
    assert agent_result["requeued"] == 0 and agents.stats()["queued"] == 0


async def test_queued_runs_are_requeued_and_waiting_runs_are_kept(
    setup: tuple[Database, EventBus, Seed],
) -> None:
    db, bus, seed = setup
    queued = await seed.wf_run("queued")
    child = await seed.child("waiting_input")
    waiting = await seed.wf_run(
        "waiting_input", child=child, node_status="waiting_input", pending=True
    )
    lane = Supervisor(never, name="workflow")

    result = await recovery.recover_workflows(db, lane, bus)

    assert result == {"failed": 0, "orphaned": 0, "resumed": 0, "requeued": 1}
    assert lane.position(f"{seed.workspace_id}:{queued}") == 1
    assert (await seed.status(WfRun, waiting)).status == "waiting_input"
    assert (await seed.status(Run, child)).status == "waiting_input"
    await bus.flush()
    async with db.read() as session:
        types = list(
            (
                await session.scalars(select(WfRunEvent.type).where(WfRunEvent.wf_run_id == queued))
            ).all()
        )
    assert types == ["workflow.queued"]


async def test_waiting_run_whose_child_already_moved_on_is_resumed(
    setup: tuple[Database, EventBus, Seed],
) -> None:
    db, bus, seed = setup
    # 자식은 응답을 받아 다시 큐에 들어갔는데 부모를 다시 넣기 전에 프로세스가 죽은 경우.
    child = await seed.child("queued")
    waiting = await seed.wf_run(
        "waiting_input", child=child, node_status="waiting_input", pending=True
    )
    lane = Supervisor(never, name="workflow")

    result = await recovery.recover_workflows(db, lane, bus)

    assert result["resumed"] == 1 and result["requeued"] == 1
    run = await seed.status(WfRun, waiting)
    assert run.status == "queued" and run.resume_payload == {"node_id": "agent_1"}
    assert lane.position(f"{seed.workspace_id}:{waiting}") == 1
    # 기다리는 부모가 있는 자식은 고아가 아니라 1단계 복구가 다시 넣는다.
    assert (await seed.status(Run, child)).status == "queued"
