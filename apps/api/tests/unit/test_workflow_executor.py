"""워크플로우 런 실행기(services/workflow_executor.py, 2단계 설계 3.6, 3.7, 3.10).

실제 SQLite와 이벤트 버스로 런 행을 돌리고, 노드 런 기록·체크포인트·종료 처리·자식 런 정리를 본다.
대기는 시험용 노드(WaitNode)로 만든다.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.agent.interactions import InteractionReply
from app.agent.task_service import AgentTaskResult, AgentTaskSpec
from app.config import Settings
from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import (
    Agent,
    AgentVersion,
    ChatSession,
    Message,
    Run,
    WfFile,
    WfNodeRun,
    WfRun,
    WfRunEvent,
    WfVersion,
    Workflow,
)
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id
from app.runner.supervisor import CancelSignal
from app.schemas.events import RunEventOut
from app.schemas.run import PendingInteractionOut
from app.schemas.workflow import Graph, WorkflowSettings
from app.services.workflow_executor import NODE_RUN_MAX_BYTES, WorkflowRunExecutor
from app.services.workflow_runs import run_limits
from app.services.workflows import graph_hash
from app.storage.local_fs import LocalFsStorage
from app.streaming.event_bus import EventBus
from app.streaming.stores import sqlite_stores
from app.workflow.nodes import default_registry
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    NodeContext,
    NodeRegistry,
    NodeResult,
    NodeRunner,
    OutputSpec,
)
from app.workflow.ports import FilePortError


class ProbeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = None
    gate: bool = False


class WaitNode(NodeRunner[ProbeConfig]):
    """처음에는 승인을 기다리며 멈추고, 재개하면 저장한 state로 끝난다."""

    type = "test_wait"
    Config = ProbeConfig
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (OutputSpec("value", "any"),)

    async def run(
        self, ctx: NodeContext, config: ProbeConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        if state is None:
            pending = PendingInteractionOut(
                interaction_id="int_1",
                kind="approval",
                tool_call_id="call_1",
                tool_name="shell",
                input={"command": "echo hi"},
                created_at=datetime.now(UTC),
            )
            return NodeResult(status="waiting_input", pending=pending, state={"ticket": "T"})
        return NodeResult(status="succeeded", outputs={"value": state["ticket"]})


class HoldNode(NodeRunner[ProbeConfig]):
    """취소될 때까지 붙잡고 있는 노드. gate=True면 시작했음을 알린다."""

    type = "test_hold"
    Config = ProbeConfig
    fixed_outputs = (OutputSpec("value", "any"),)

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(
        self, ctx: NodeContext, config: ProbeConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        self.started.set()
        await asyncio.Event().wait()
        return NodeResult(status="succeeded")


class ChildNode(NodeRunner[ProbeConfig]):
    """포트로 자식 과업을 만들고 실패한다. 실행기가 자식 id를 기록하고 정리하는지 본다."""

    type = "test_child"
    Config = ProbeConfig
    fixed_outputs = (OutputSpec("value", "any"),)

    async def run(
        self, ctx: NodeContext, config: ProbeConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        assert ctx.ports.tasks is not None
        await ctx.ports.tasks.start(
            AgentTaskSpec(
                workspace_id=uuid.UUID(int=0),
                agent_id=uuid.uuid4(),
                instruction="x",
                correlation={"node_run_id": str(ctx.node_run_id)},
            )
        )
        raise RuntimeError("자식을 만든 뒤 실패")


class FakeTasks:
    """AgentTaskService. start는 미리 만든 run 행 id를 돌려준다."""

    def __init__(self, child_run_id: uuid.UUID) -> None:
        self.child_run_id = child_run_id
        self.specs: list[AgentTaskSpec] = []
        self.cancelled: list[uuid.UUID] = []

    async def start(self, spec: AgentTaskSpec) -> uuid.UUID:
        self.specs.append(spec)
        return self.child_run_id

    async def events(
        self, run_id: uuid.UUID, after: int | None = None
    ) -> AsyncIterator[RunEventOut]:
        if False:
            yield  # pragma: no cover

    async def wait(self, run_id: uuid.UUID, timeout: float | None = None) -> AgentTaskResult:
        raise NotImplementedError

    async def provide_input(self, run_id: uuid.UUID, payload: InteractionReply) -> None:
        raise NotImplementedError

    async def cancel(self, run_id: uuid.UUID) -> None:
        self.cancelled.append(run_id)


class Env:
    def __init__(self, db: Database, bus: EventBus, settings: Settings) -> None:
        self.db = db
        self.bus = bus
        self.settings = settings
        self.workspace_id = new_id()
        self.workflow_id = new_id()
        self.version_id = new_id()
        self.hold = HoldNode()
        self.registry = default_registry().copy()
        for runner in (WaitNode(), self.hold, ChildNode()):
            self.registry.register(runner)

    async def setup(self) -> None:
        async with self.db.write() as session:
            workspace = WorkspaceRepository(session).add(name="w", slug="w", settings={})
            workspace.id = self.workspace_id
            await session.flush()
            session.add(
                Workflow(
                    id=self.workflow_id,
                    workspace_id=self.workspace_id,
                    slug="wf",
                    name="wf",
                    kind="workflow",
                )
            )

    async def version(self, graph: Graph) -> None:
        async with self.db.write() as session:
            existing = await session.get(WfVersion, self.version_id)
            if existing is not None:
                existing.graph = graph.model_dump(mode="json")
                existing.graph_hash = graph_hash(graph)
                return
            session.add(
                WfVersion(
                    id=self.version_id,
                    workspace_id=self.workspace_id,
                    workflow_id=self.workflow_id,
                    status="draft",
                    graph=graph.model_dump(mode="json"),
                    settings=WorkflowSettings().model_dump(mode="json"),
                    graph_hash=graph_hash(graph),
                )
            )

    async def queued_run(self, inputs: dict[str, Any] | None = None) -> uuid.UUID:
        run_id = new_id()
        async with self.db.write() as session:
            version = await session.get(WfVersion, self.version_id)
            assert version is not None
            session.add(
                WfRun(
                    id=run_id,
                    workspace_id=self.workspace_id,
                    workflow_id=self.workflow_id,
                    wf_version_id=self.version_id,
                    kind="workflow",
                    trigger="preview",
                    status="queued",
                    inputs=inputs or {},
                    usage={},
                    limits=run_limits(self.settings, WorkflowSettings()),
                    graph_hash=version.graph_hash,
                )
            )
        return run_id

    async def child_run(self) -> uuid.UUID:
        """자식 과업 런 행(외래 키 때문에 실제 run 행이 있어야 한다)."""
        async with self.db.write() as session:
            agent = Agent(id=new_id(), workspace_id=self.workspace_id, slug="a", name="a")
            session.add(agent)
            await session.flush()
            version = AgentVersion(
                id=new_id(),
                workspace_id=self.workspace_id,
                agent_id=agent.id,
                status="draft",
                approval_mode="all_shell",
            )
            session.add(version)
            await session.flush()
            chat = ChatSession(
                id=new_id(),
                workspace_id=self.workspace_id,
                agent_id=agent.id,
                agent_version_id=version.id,
                source="task",
            )
            session.add(chat)
            await session.flush()
            message = Message(
                id=new_id(), workspace_id=self.workspace_id, session_id=chat.id, seq=1, role="user"
            )
            session.add(message)
            await session.flush()
            run = Run(
                id=new_id(),
                workspace_id=self.workspace_id,
                session_id=chat.id,
                agent_version_id=version.id,
                trigger="task",
                status="queued",
                user_message_id=message.id,
                approval_mode="all_shell",
            )
            session.add(run)
            return run.id

    def executor(self, tasks: FakeTasks | None = None, registry: NodeRegistry | None = None) -> Any:
        return WorkflowRunExecutor(
            db=self.db,
            settings=self.settings,
            bus=self.bus,
            tasks=tasks,
            registry=registry or self.registry,
        )

    def key(self, run_id: uuid.UUID) -> str:
        return f"{self.workspace_id}:{run_id}"

    async def run_row(self, run_id: uuid.UUID) -> WfRun:
        async with self.db.read() as session:
            row = await session.get(WfRun, run_id)
            assert row is not None
            return row

    async def node_rows(self, run_id: uuid.UUID) -> list[WfNodeRun]:
        async with self.db.read() as session:
            result = await session.scalars(
                select(WfNodeRun).where(WfNodeRun.wf_run_id == run_id).order_by(WfNodeRun.index)
            )
            return list(result.all())

    async def event_types(self, run_id: uuid.UUID) -> list[str]:
        await self.bus.flush()
        async with self.db.read() as session:
            result = await session.scalars(
                select(WfRunEvent.type)
                .where(WfRunEvent.wf_run_id == run_id)
                .order_by(WfRunEvent.seq)
            )
            return list(result.all())


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    db = Database(tmp_path / "wf.db")
    await upgrade_head(db)
    bus = EventBus(sqlite_stores(db), batch_window_sec=0.01, drop_delay_sec=0)
    bus.start()
    settings = Settings.model_validate({"data_dir": tmp_path / "data"})
    instance = Env(db, bus, settings)
    await instance.setup()
    try:
        yield instance
    finally:
        await bus.close()
        await db.close()


def build(nodes: Sequence[dict[str, Any]], *pairs: str) -> Graph:
    edges = []
    for index, pair in enumerate(pairs, start=1):
        source, target = pair.split(">")
        edges.append({"id": f"e{index}", "source": source, "target": target})
    return Graph.model_validate({"nodes": list(nodes), "edges": edges})


def node(node_id: str, node_type: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "title": node_id, "config": config or {}}


GREETING = build(
    [
        node("start", "start", {"fields": [{"name": "name", "type": "string"}]}),
        node(
            "template_1",
            "template",
            {"template": "안녕, {{ name }}", "vars": [{"name": "name", "var": "start.name"}]},
        ),
        node("end_1", "end", {"outputs": [{"name": "greeting", "var": "template_1.output"}]}),
    ],
    "start>template_1",
    "template_1>end_1",
)


async def test_runs_a_queued_run_and_records_node_runs_and_events(env: Env) -> None:
    await env.version(GREETING)
    run_id = await env.queued_run({"name": "지우"})

    await env.executor()(env.key(run_id), CancelSignal())

    run = await env.run_row(run_id)
    assert run.status == "succeeded"
    assert run.outputs == {"greeting": "안녕, 지우"}
    assert run.node_run_count == 3 and run.ended_at is not None and run.started_at is not None
    assert run.state is None and run.pending_interaction is None
    rows = await env.node_rows(run_id)
    assert [(row.node_id, row.status, row.index) for row in rows] == [
        ("start", "succeeded", 0),
        ("template_1", "succeeded", 1),
        ("end_1", "succeeded", 2),
    ]
    assert rows[1].outputs == {"output": "안녕, 지우"} and rows[1].truncated is False
    assert rows[1].duration_ms is not None and rows[1].ended_at is not None
    assert await env.event_types(run_id) == [
        "workflow.started",
        "node.started",
        "node.completed",
        "node.started",
        "node.completed",
        "node.started",
        "node.completed",
        "workflow.completed",
    ]


async def test_large_node_outputs_are_cut_to_64kb_in_the_record(env: Env) -> None:
    big = build(
        [
            node("start", "start"),
            node("template_1", "template", {"template": "{{ '가' * 40000 }}"}),
            node("end_1", "end", {"outputs": [{"name": "text", "var": "template_1.output"}]}),
        ],
        "start>template_1",
        "template_1>end_1",
    )
    await env.version(big)
    run_id = await env.queued_run()

    await env.executor()(env.key(run_id), CancelSignal())

    run = await env.run_row(run_id)
    # 런 출력 전문은 wf_run.outputs에 남고 노드 런 기록만 자른다.
    assert run.status == "succeeded" and len(run.outputs["text"]) == 40000  # type: ignore[index]
    template = (await env.node_rows(run_id))[1]
    assert template.truncated is True
    stored = template.outputs or {}
    assert len(str(stored["output"]).encode("utf-8")) <= NODE_RUN_MAX_BYTES
    await env.bus.flush()
    async with env.db.read() as session:
        completed = await session.scalar(
            select(WfRunEvent.payload).where(
                WfRunEvent.wf_run_id == run_id, WfRunEvent.type == "workflow.completed"
            )
        )
    assert completed is not None and completed["truncated"] is True
    assert len(str(completed["outputs"]["text"]).encode("utf-8")) <= 4096


async def test_waiting_run_keeps_a_checkpoint_and_resumes(env: Env) -> None:
    graph = build(
        [
            node("start", "start"),
            node("wait_1", "test_wait"),
            node("end_1", "end", {"outputs": [{"name": "ticket", "var": "wait_1.value"}]}),
        ],
        "start>wait_1",
        "wait_1>end_1",
    )
    await env.version(graph)
    run_id = await env.queued_run()
    executor = env.executor()

    await executor(env.key(run_id), CancelSignal())

    run = await env.run_row(run_id)
    assert run.status == "waiting_input" and run.ended_at is None
    assert run.state is not None and run.state["waiting"]
    assert run.pending_interaction is not None
    assert run.pending_interaction["node_id"] == "wait_1"
    assert run.pending_interaction["interaction"]["kind"] == "approval"
    waiting_row = next(row for row in await env.node_rows(run_id) if row.node_id == "wait_1")
    assert waiting_row.status == "waiting_input"
    assert (await env.event_types(run_id))[-2:] == ["workflow.waiting_input", "workflow.completed"]

    async with env.db.write() as session:
        row = await session.get(WfRun, run_id)
        assert row is not None
        row.status = "queued"
        row.resume_payload = {"node_id": "wait_1"}
    await executor(env.key(run_id), CancelSignal())

    run = await env.run_row(run_id)
    assert run.status == "succeeded" and run.outputs == {"ticket": "T"}
    assert run.state is None and run.pending_interaction is None
    rows = await env.node_rows(run_id)
    # 재개한 노드는 같은 노드 런 행을 이어 쓴다.
    assert [row.node_id for row in rows].count("wait_1") == 1
    assert next(row for row in rows if row.node_id == "wait_1").status == "succeeded"
    types = await env.event_types(run_id)
    assert types.count("workflow.started") == 2 and "workflow.resumed" in types


async def test_resume_after_the_draft_changed_fails_the_run(env: Env) -> None:
    graph = build(
        [node("start", "start"), node("wait_1", "test_wait"), node("end_1", "end")],
        "start>wait_1",
        "wait_1>end_1",
    )
    await env.version(graph)
    run_id = await env.queued_run()
    executor = env.executor()
    await executor(env.key(run_id), CancelSignal())
    assert (await env.run_row(run_id)).status == "waiting_input"
    await env.version(build([node("start", "start"), node("end_1", "end")], "start>end_1"))
    async with env.db.write() as session:
        row = await session.get(WfRun, run_id)
        assert row is not None
        row.status = "queued"
        row.resume_payload = {"node_id": "wait_1"}

    await executor(env.key(run_id), CancelSignal())

    run = await env.run_row(run_id)
    assert run.status == "failed" and run.error is not None
    assert run.error["code"] == "graph_changed"
    assert (await env.event_types(run_id))[-2:] == ["workflow.error", "workflow.completed"]
    waited = next(row for row in await env.node_rows(run_id) if row.node_id == "wait_1")
    assert waited.status == "failed" and waited.error is not None
    assert waited.error["code"] == "graph_changed"


@pytest.mark.parametrize(("reason", "status"), [("shutdown", "failed"), ("user", "cancelled")])
async def test_cancel_signal_ends_the_run(env: Env, reason: str, status: str) -> None:
    graph = build(
        [node("start", "start"), node("hold_1", "test_hold"), node("end_1", "end")],
        "start>hold_1",
        "hold_1>end_1",
    )
    await env.version(graph)
    run_id = await env.queued_run()
    cancel = CancelSignal()
    task = asyncio.create_task(env.executor()(env.key(run_id), cancel))
    await asyncio.wait_for(env.hold.started.wait(), 5)

    cancel.set(reason)
    await asyncio.wait_for(task, 5)

    run = await env.run_row(run_id)
    assert run.status == status
    # 종료로 실패한 런도 체크포인트를 쓰지 않는다(3.6).
    assert run.state is None
    if status == "failed":
        assert run.error is not None and run.error["code"] == "shutdown"
    held = next(row for row in await env.node_rows(run_id) if row.node_id == "hold_1")
    assert held.status == "cancelled"
    assert (await env.event_types(run_id))[-1] == "workflow.completed"


@pytest.mark.parametrize(("reason", "status"), [("user", "cancelled"), ("shutdown", "failed")])
async def test_a_parallel_wait_is_closed_when_the_run_stops(
    env: Env, reason: str, status: str
) -> None:
    graph = build(
        [
            node("start", "start"),
            node("wait_1", "test_wait"),
            node("hold_1", "test_hold"),
            node("end_1", "end"),
        ],
        "start>wait_1",
        "start>hold_1",
        "wait_1>end_1",
        "hold_1>end_1",
    )
    await env.version(graph)
    run_id = await env.queued_run()
    cancel = CancelSignal()
    task = asyncio.create_task(env.executor()(env.key(run_id), cancel))
    await asyncio.wait_for(env.hold.started.wait(), 5)

    cancel.set(reason)
    await asyncio.wait_for(task, 5)

    assert (await env.run_row(run_id)).status == status
    rows = {row.node_id: row for row in await env.node_rows(run_id)}
    assert rows["hold_1"].status == "cancelled"
    # 엔진은 이미 멈춘 대기 노드를 다시 돌리지 않으므로 실행기가 런과 같은 상태로 닫는다.
    waited = rows["wait_1"]
    assert waited.status == status and waited.ended_at is not None
    if status == "failed":
        assert waited.error is not None and waited.error["code"] == "shutdown"


async def test_child_run_is_recorded_at_start_and_cancelled_when_the_run_fails(env: Env) -> None:
    child_run_id = await env.child_run()
    tasks = FakeTasks(child_run_id)
    graph = build(
        [node("start", "start"), node("child_1", "test_child"), node("end_1", "end")],
        "start>child_1",
        "child_1>end_1",
    )
    await env.version(graph)
    run_id = await env.queued_run()

    await env.executor(tasks)(env.key(run_id), CancelSignal())

    [spec] = tasks.specs
    # 포트가 워크스페이스와 연결 정보를 채운다.
    assert spec.workspace_id == env.workspace_id
    assert spec.correlation["workflow_run_id"] == str(run_id)
    child_row = next(row for row in await env.node_rows(run_id) if row.node_id == "child_1")
    assert child_row.child_run_id == child_run_id and child_row.status == "failed"
    run = await env.run_row(run_id)
    assert run.status == "failed" and run.error is not None
    assert run.error["code"] == "node_error" and run.error["node_id"] == "child_1"
    # 실패한 런의 체크포인트는 남긴다(4.4).
    assert run.state is not None
    assert tasks.cancelled == [child_run_id]


async def test_stopped_or_missing_runs_are_skipped(env: Env) -> None:
    await env.version(GREETING)
    run_id = await env.queued_run({"name": "x"})
    async with env.db.write() as session:
        row = await session.get(WfRun, run_id)
        assert row is not None
        row.status = "cancelled"
    executor = env.executor()

    await executor(env.key(run_id), CancelSignal())
    await executor(env.key(new_id()), CancelSignal())
    await executor("not-a-key", CancelSignal())

    assert (await env.run_row(run_id)).status == "cancelled"
    assert await env.node_rows(run_id) == []


class FileNode(NodeRunner[ProbeConfig]):
    """파일 포트로 파일을 남기고 다시 읽는다. 노드 설정도 출력으로 내놓는다."""

    type = "test_file"
    Config = ProbeConfig
    fixed_outputs = (
        OutputSpec("file", "file"),
        OutputSpec("text", "string"),
        OutputSpec("seen", "object"),
    )

    async def run(
        self, ctx: NodeContext, config: ProbeConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        files = ctx.ports.files
        assert files is not None
        ref = await files.save_output(
            Path(str(config.value)), filename="보고서:1.txt", mime="text/plain"
        )
        data = await files.read_bytes(ref, max_bytes=1024)
        missing = ""
        try:
            await files.read_bytes({**ref, "file_id": str(uuid.uuid4())}, max_bytes=1024)
        except FilePortError as exc:
            missing = exc.code
        settings = ctx.ports.node_settings
        seen = {
            "code_timeout_sec": settings.code_timeout_sec,
            "prices": dict(settings.model_prices),
            "missing": missing,
        }
        return NodeResult(
            status="succeeded",
            outputs={"file": ref, "text": data.decode("utf-8"), "seen": seen},
        )


async def test_file_port_and_node_settings(env: Env, tmp_path: Path) -> None:
    source = tmp_path / "made-by-node.txt"
    source.write_text("노드가 만든 파일", encoding="utf-8")
    graph = build(
        [
            node("start", "start"),
            node("file_1", "test_file", {"value": str(source)}),
            node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "file", "var": "file_1.file"},
                        {"name": "text", "var": "file_1.text"},
                        {"name": "seen", "var": "file_1.seen"},
                    ]
                },
            ),
        ],
        "start>file_1",
        "file_1>end_1",
    )
    await env.version(graph)
    prices = {"m-1": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}
    async with env.db.write() as session:
        workspace = await WorkspaceRepository(session).get(env.workspace_id)
        assert workspace is not None
        workspace.settings = {"model_prices": prices}
    run_id = await env.queued_run()
    registry = env.registry.copy()
    registry.register(FileNode())
    storage = LocalFsStorage(tmp_path / "storage")
    executor = WorkflowRunExecutor(
        db=env.db,
        settings=env.settings.model_copy(update={"wf_code_timeout_sec": 12}),
        bus=env.bus,
        storage=storage,
        registry=registry,
    )

    await executor(env.key(run_id), CancelSignal())

    run = await env.run_row(run_id)
    assert run.status == "succeeded", run.error
    outputs = run.outputs or {}
    assert outputs["text"] == "노드가 만든 파일"
    assert outputs["seen"] == {
        "code_timeout_sec": 12,
        "prices": {"m-1": {**prices["m-1"], "cached_input_per_mtok": None}},
        "missing": "file_not_found",
    }
    ref = outputs["file"]
    async with env.db.read() as session:
        row = await session.get(WfFile, uuid.UUID(ref["file_id"]))
    assert row is not None
    assert (row.kind, row.wf_run_id, row.workflow_id) == ("output", run_id, env.workflow_id)
    assert (row.filename, row.mime, ref["source"]) == ("보고서_1.txt", "text/plain", "wf_file")
    assert storage.local_path(row.storage_key).read_text("utf-8") == "노드가 만든 파일"
