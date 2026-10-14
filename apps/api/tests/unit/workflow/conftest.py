"""워크플로우 엔진 테스트의 가짜 포트, 시험용 노드, 픽스처 그래프, 실행 도우미.

--import-mode=importlib에서는 테스트 모듈이 conftest를 import할 수 없어 kit 픽스처로 넘긴다.
"""

import asyncio
import json
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.agent.task_service import AgentTaskResult, AgentTaskSpec
from app.llm.base import ModelAdapter
from app.runner.supervisor import CancelSignal
from app.sandbox.base import SandboxBackend
from app.schemas.events import UsageTotal
from app.schemas.run import PendingInteractionOut, RunErrorOut
from app.schemas.workflow import WF_EVENT_DATA_MODELS, Graph, NodeError, VarType, WorkflowKind
from app.streaming.event_bus import jsonable
from app.workflow.engine import Engine, EngineLimits, RunContext, RunOutcome, WfEvent
from app.workflow.frames import Checkpoint
from app.workflow.nodes import default_registry
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    NodeContext,
    NodeRegistry,
    NodeResult,
    NodeRunner,
    OutputSpec,
)
from app.workflow.ports import ChildRunView, FilePort, HttpPort, NodeSettings, TaskPort

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "graphs"

# 자식 런이 만들어질 때 그 결과를 정한다. None이면 시험이 settle()할 때까지 돌고 있다.
ChildPlan = Callable[["FakeTasks", uuid.UUID, AgentTaskSpec], AgentTaskResult | None]


class FakeTasks:
    """TaskPort. 자식 런을 메모리에 두고 결과는 계획(plan)이나 settle()로 정한다.

    계획이 없으면 자식은 곧바로 succeeded로 끝나고 글은 "done: <지시>"다. 같은 객체를 엔진 런
    두 번(대기 → 재개)에 넘기면 재개한 노드가 그 자식을 다시 기다린다.
    """

    def __init__(self) -> None:
        self.specs: list[AgentTaskSpec] = []
        self.started: list[uuid.UUID] = []
        self.sessions: dict[uuid.UUID, uuid.UUID] = {}
        self.results: dict[uuid.UUID, AgentTaskResult | None] = {}
        self.files: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.cancelled: list[uuid.UUID] = []
        self.waits: list[uuid.UUID] = []
        self.plan: deque[ChildPlan] = deque()
        self.positions: dict[uuid.UUID, int] = {}
        self._changed: dict[uuid.UUID, asyncio.Event] = {}

    def result(self, child: uuid.UUID, status: str = "succeeded", **fields: Any) -> AgentTaskResult:
        return AgentTaskResult.model_validate(
            {"run_id": child, "session_id": self.sessions[child], "status": status, **fields}
        )

    def waiting(self, child: uuid.UUID, interaction_id: str = "int_1") -> AgentTaskResult:
        pending = PendingInteractionOut(
            interaction_id=interaction_id,
            kind="approval",
            tool_call_id="call_1",
            tool_name="shell",
            input={"command": "echo hi"},
            created_at=datetime.now(UTC),
        )
        return self.result(child, "waiting_input", pending_interaction=pending)

    def failed(self, child: uuid.UUID, code: str, status: str = "failed") -> AgentTaskResult:
        error = RunErrorOut(code=code, message=f"{code} 때문에 끝났다")
        return self.result(child, status, error=error)

    def settle(self, child: uuid.UUID, result: AgentTaskResult | None) -> None:
        self.results[child] = result
        self._changed[child].set()
        self._changed[child] = asyncio.Event()

    async def start(self, spec: AgentTaskSpec) -> uuid.UUID:
        child = uuid.uuid4()
        self.specs.append(spec)
        self.started.append(child)
        self.sessions[child] = uuid.uuid4()
        self._changed[child] = asyncio.Event()
        plan = self.plan.popleft() if self.plan else None
        if plan is None:
            self.results[child] = self.result(child, text=f"done: {spec.instruction}")
        else:
            self.results[child] = plan(self, child, spec)
        return child

    async def wait(self, run_id: uuid.UUID, timeout: float | None = None) -> AgentTaskResult:
        self.waits.append(run_id)
        async with asyncio.timeout(timeout):
            while True:
                result = self.results[run_id]
                if result is not None:
                    return result
                await self._changed[run_id].wait()

    async def cancel(self, run_id: uuid.UUID) -> None:
        self.cancelled.append(run_id)
        current = self.results.get(run_id)
        if current is None or current.status == "waiting_input":
            self.settle(run_id, self.failed(run_id, "cancelled", status="cancelled"))

    async def child(self, run_id: uuid.UUID) -> ChildRunView:
        result = self.results[run_id]
        status = "running" if result is None else result.status
        if run_id in self.positions:
            status = "queued"
        return ChildRunView(
            run_id=run_id,
            session_id=self.sessions[run_id],
            status=status,
            steps=2,
            usage=result.usage if result is not None else UsageTotal(),
            position=self.positions.get(run_id),
            files=self.files.get(run_id, []),
        )


class FakePorts:
    """EnginePorts. 제어 노드는 모델·샌드박스·HTTP·파일·과업 서비스를 쓰지 않는다.

    모델·HTTP·코드·agent 노드 시험은 쓰는 것만 넣어 만든다.
    """

    def __init__(
        self,
        *,
        model: ModelAdapter | None = None,
        sandbox: SandboxBackend | None = None,
        http: HttpPort | None = None,
        files: FilePort | None = None,
        tasks: TaskPort | None = None,
        node_settings: NodeSettings | None = None,
    ) -> None:
        self.model = model
        self.sandbox = sandbox
        self.http = http
        self.files = files
        self.tasks = tasks
        self.node_settings = node_settings or NodeSettings()
        self.checkpoints: list[Checkpoint] = []

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        # Windows의 time.monotonic은 15ms 단위라 짧은 간격 시험에 perf_counter를 쓴다.
        return time.perf_counter()

    async def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self.checkpoints.append(checkpoint)


class ProbeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any = None
    # test_gate: 기다릴 문 이름(없으면 노드 id).
    gate: str | None = None
    # test_flaky: 이만큼 실패한 뒤 성공한다.
    failures: int = 0
    retryable: bool = True
    code: str = "flaky"
    tokens: int = 0
    # 노드 시간 상한(초). 엔진이 run() 바깥에서 건다.
    timeout: float | None = None


class GateRunner(NodeRunner[ProbeConfig]):
    """open()으로 문을 열 때까지 멈춰 있는 노드. 동시에 몇 개가 돌았는지 센다."""

    type = "test_gate"
    Config = ProbeConfig
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (OutputSpec("value", "any"),)

    def __init__(self) -> None:
        self.gates: dict[str, asyncio.Event] = {}
        self.entered: list[str] = []
        self.cancelled: list[str] = []
        self.running = 0
        self.peak = 0

    def gate(self, name: str) -> asyncio.Event:
        return self.gates.setdefault(name, asyncio.Event())

    def open(self, *names: str) -> None:
        for name in names:
            self.gate(name).set()

    def timeout_sec(self, config: ProbeConfig) -> float | None:
        return config.timeout

    async def run(
        self, ctx: NodeContext, config: ProbeConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        self.entered.append(ctx.node_id)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await self.gate(config.gate or ctx.node_id).wait()
        except asyncio.CancelledError:
            self.cancelled.append(ctx.node_id)
            raise
        finally:
            self.running -= 1
        return NodeResult(status="succeeded", outputs={"value": config.value})


class FlakyRunner(NodeRunner[ProbeConfig]):
    """failures번 실패한 뒤 value를 낸다. tokens가 있으면 시도마다 그만큼 usage를 낸다."""

    type = "test_flaky"
    Config = ProbeConfig
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (OutputSpec("value", "any"),)

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    async def run(
        self, ctx: NodeContext, config: ProbeConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        count = self.calls.get(ctx.node_id, 0) + 1
        self.calls[ctx.node_id] = count
        usage = UsageTotal(input_tokens=config.tokens, output_tokens=1) if config.tokens else None
        if count <= config.failures:
            error = NodeError(
                code=config.code, message=f"{count}번째 실패", retryable=config.retryable
            )
            return NodeResult(status="failed", error=error, usage=usage)
        return NodeResult(status="succeeded", outputs={"value": config.value}, usage=usage)


class WaitRunner(NodeRunner[ProbeConfig]):
    """agent 노드 흉내. 처음에는 승인을 기다리며 멈추고, 재개하면 저장한 state로 끝난다."""

    type = "test_wait"
    Config = ProbeConfig
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (OutputSpec("value", "any"),)

    def __init__(self) -> None:
        self.states: list[dict[str, Any] | None] = []

    async def run(
        self, ctx: NodeContext, config: ProbeConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        self.states.append(state)
        if state is None:
            pending = PendingInteractionOut(
                interaction_id=f"int_{ctx.node_id}",
                kind="approval",
                tool_call_id="call_1",
                tool_name="shell",
                input={"command": "echo hi"},
                created_at=datetime.now(UTC),
            )
            return NodeResult(
                status="waiting_input",
                pending=pending,
                state={"ticket": config.value},
                child_run_id=uuid.uuid4(),
            )
        return NodeResult(status="succeeded", outputs={"value": state["ticket"]})


def check_event(event: WfEvent) -> None:
    # 이벤트 data가 스키마 모델에 맞고 모르는 필드가 없는지 본다(1단계 RecordingPorts와 같다).
    model = WF_EVENT_DATA_MODELS[event.type]
    payload = jsonable(event.data)
    model.model_validate(payload)
    unknown = set(payload) - set(model.model_fields)
    assert not unknown, f"{event.type}에 스키마에 없는 필드: {unknown}"
    if event.type in ("node.started", "node.completed"):
        assert event.node_run is not None
        assert event.node_run.node_run_id == event.data["node_run_id"]


@dataclass
class RunResult:
    events: list[WfEvent]
    outcome: RunOutcome
    ports: FakePorts

    def types(self) -> list[str]:
        return [event.type for event in self.events]

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [event.data for event in self.events if event.type == event_type]

    def started(self) -> list[str]:
        return [data["node_id"] for data in self.of("node.started")]

    def statuses(self) -> dict[str, str]:
        """노드 id → 마지막 node.completed 상태."""
        return {data["node_id"]: data["status"] for data in self.of("node.completed")}

    def completed_order(self) -> list[tuple[str, str]]:
        return [(data["node_id"], data["status"]) for data in self.of("node.completed")]


OnEvent = Callable[[WfEvent], Awaitable[None] | None]


class Kit:
    def __init__(self) -> None:
        self.gate = GateRunner()
        self.flaky = FlakyRunner()
        self.wait = WaitRunner()
        self.registry: NodeRegistry = default_registry().copy()
        for runner in (self.gate, self.flaky, self.wait):
            self.registry.register(runner)
        self.engine = Engine(self.registry)

    def load(self, name: str) -> Graph:
        return Graph.model_validate(json.loads((FIXTURES / f"{name}.json").read_text("utf-8")))

    def ports(self, **parts: Any) -> FakePorts:
        """모델·샌드박스·HTTP·파일·과업·노드 설정 중 시험에 쓰는 것만 넣은 포트."""
        return FakePorts(**parts)

    def tasks(self) -> FakeTasks:
        return FakeTasks()

    def raw(self, name: str) -> dict[str, Any]:
        return json.loads((FIXTURES / f"{name}.json").read_text("utf-8"))

    def graph(self, nodes: list[dict[str, Any]], edges: list[str]) -> Graph:
        """edges는 "source>target" 또는 "source:handle>target"."""
        parsed: list[dict[str, Any]] = []
        for index, spec in enumerate(edges, start=1):
            source, target = spec.split(">")
            source, _, handle = source.partition(":")
            parsed.append(
                {
                    "id": f"e{index}",
                    "source": source,
                    "target": target,
                    "source_handle": handle or "out",
                }
            )
        return Graph.model_validate({"nodes": nodes, "edges": parsed})

    def context(
        self,
        graph: Graph,
        *,
        kind: WorkflowKind = "workflow",
        inputs: Mapping[str, Any] | None = None,
        limits: EngineLimits | None = None,
        cancel: CancelSignal | None = None,
        resume: Checkpoint | None = None,
        env: Mapping[str, Any] | None = None,
        secret_env: Collection[str] = (),
        conversation: Mapping[str, Any] | None = None,
        conversation_types: Mapping[str, VarType] | None = None,
        system: Mapping[str, Any] | None = None,
        mask_outputs: bool = True,
        ports: FakePorts | None = None,
        run_id: uuid.UUID | None = None,
        single_node: str | None = None,
        seed: Mapping[str, Any] | None = None,
    ) -> RunContext:
        return RunContext(
            run_id=run_id or uuid.uuid4(),
            graph=graph,
            kind=kind,
            ports=ports or FakePorts(),
            cancel=cancel or CancelSignal(),
            inputs=dict(inputs or {}),
            workflow_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            system=dict(system or {}),
            env=dict(env or {}),
            secret_env=secret_env,
            conversation=dict(conversation or {}),
            conversation_types=conversation_types,
            limits=limits or EngineLimits(),
            resume=resume,
            mask_outputs=mask_outputs,
            single_node=single_node,
            seed=dict(seed or {}),
        )

    async def run(
        self, graph: Graph, *, on_event: OnEvent | None = None, **options: Any
    ) -> RunResult:
        context = self.context(graph, **options)
        ports = context.ports
        assert isinstance(ports, FakePorts)
        events: list[WfEvent] = []
        run = self.engine.run(context)
        async with run:
            async for event in run:
                check_event(event)
                events.append(event)
                if on_event is not None:
                    result = on_event(event)
                    if result is not None:
                        await result
        return RunResult(events, run.outcome, ports)


def node(
    node_id: str, node_type: str, config: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "title": node_id, "config": config or {}, **extra}


@pytest.fixture
def kit() -> Kit:
    return Kit()


@pytest.fixture
def make_node() -> Callable[..., dict[str, Any]]:
    return node
