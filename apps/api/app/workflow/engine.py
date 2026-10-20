# pyright: strict
"""워크플로우 엔진(설계 3.4, 3.5, 3.7~3.11).

스케줄러는 준비된 노드마다 asyncio.Task를 만들고 동시 실행 수를 세마포어(기본 4, code 노드는
타입별 1)로 막는다. 노드가 끝나면 출력을 풀에 넣고 엣지를 해결해 다음 준비 노드를 띄운다. 준비된
노드도 실행 중인 노드도 없으면 런이 끝난다(end·answer에 닿아도 다른 가지는 끝까지 돈다).
노드 실패(strategy=fail), 취소, 런 시간 초과, 토큰 예산 초과는 실행 중인 노드를 취소하고 런을
끝낸다.

iteration 노드는 run_frames로 하위 그래프를 반복마다 한 프레임씩 돌린다. 프레임은 풀을 복제하지
않고 "<노드>@<index>" 키로 출력을 남기며, 반복 안의 노드도 같은 세마포어를 나눠 쓴다. 반복 안에서
입력 대기가 생기면 그 반복을 멈춘 채로 두고(순차면 새 반복을 시작하지 않는다) iteration 노드가
waiting_input으로 끝난다. 재개하면 체크포인트의 @index 상태로 멈춘 반복부터 잇는다.

loop 노드는 run_loop로 같은 프레임 기계를 하나씩 차례로 돌린다. 반복 변수는 풀의 "<loop id>"
자리에 두고 하위 그래프의 variable_assigner가 바꾸며(NodeResult.loop_updates), 반복이 끝날 때마다
종료 조건을 본다. loop가 끝나면 그 자리는 loop 노드의 최종 출력으로 바뀐다.

입력 대기는 agent 노드의 승인·질문과 human_input 노드의 폼 두 가지다. 폼 응답은 재개할 때
RunContext.reply로 들어와 그 노드 state의 "reply"로 넘어간다.

단일 노드 실행(RunContext.single_node)은 그 노드 하나(iteration이면 하위 그래프까지)만 돈다. 노드가
참조하는 변수는 seed로 받은 값을 풀에 먼저 넣는다.

챗플로우의 answer는 상류 llm 델타를 노드가 ready가 되기 전에 answer.delta로 미리 흘릴 수 있다(3.8).
그 판단과 흘리기는 answer_stream.AnswerStreams가 루트 프레임 노드의 시작·델타·완료를 받아서 한다.

이벤트는 Engine.run()이 돌려주는 비동기 반복자로 나간다. 엔진은 이벤트 하나를 내고 소비자가 다음
이벤트를 달라고 할 때까지 기다린다. 그래서 소비자(M12의 실행기)가 노드 런 행을 쓰는 동안 엔진은
다음 노드로 넘어가지 않는다.
"""

import asyncio
import contextlib
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
import uuid6
from pydantic import BaseModel, Field, ValidationError

from app.schemas.events import UsageTotal, WfPendingKind, WorkflowFormData
from app.schemas.run import PendingInteractionOut
from app.schemas.workflow import (
    FAIL_HANDLE,
    OUT_HANDLE,
    Graph,
    Issue,
    Node,
    NodeError,
    NodeRunStatus,
    VarType,
    WorkflowEndStatus,
    WorkflowKind,
)
from app.workflow.answer_stream import AnswerStreams
from app.workflow.frames import (
    Checkpoint,
    FrameFailure,
    FrameGraph,
    FrameState,
    IterationProgress,
    LoopProgress,
    WaitingRun,
    inner_nodes,
    single_frame,
    state_key,
)
from app.workflow.graph import Analysis, analyze
from app.workflow.memory import Turn
from app.workflow.nodes import default_registry
from app.workflow.nodes.base import (
    NODE_EVENT_TYPES,
    REPLY_STATE_KEY,
    AnyRunner,
    FramePlan,
    FrameResult,
    FramesOutcome,
    LoopOutcome,
    LoopPlan,
    NodeFailure,
    NodeNotImplemented,
    NodeRegistry,
    NodeResult,
)
from app.workflow.ports import CancelToken, EnginePorts
from app.workflow.render import TemplateError, render
from app.workflow.variables import (
    CONV,
    ENV,
    POOL_MAX_BYTES,
    SYS,
    Masker,
    PoolLimitError,
    PoolView,
    Selector,
    SelectorError,
    StreamMasker,
    VariableError,
    VariablePool,
    coerce,
    measure,
    parse_selector,
    preview,
    sys_variables,
)

log = structlog.get_logger("app.workflow")

# 글을 조각으로 흘리는 이벤트. 비밀값 마스킹을 스트림 단위로 한다.
STREAMED_EVENTS = frozenset({"node.text_delta", "answer.delta"})


@dataclass(frozen=True)
class EngineLimits:
    max_run_seconds: float = 1800
    # 자식 런(agent 노드)을 포함한 입력+출력 토큰.
    max_total_tokens: int = 2_000_000
    max_cost_usd: float | None = None
    max_parallel_nodes: int = 4
    pool_max_bytes: int = POOL_MAX_BYTES
    # 0이면 끈다. 켜면 노드가 끝날 때 마지막 저장 뒤 이만큼 지났으면 체크포인트를 쓴다(3.7의 (3)).
    checkpoint_interval_sec: float = 0


@dataclass(frozen=True)
class NodeReply:
    """입력 대기로 멈춘 노드에 사람이 낸 응답(human_input 폼).

    재개하면 그 노드 state["reply"]가 된다.
    """

    node_id: str
    iteration_index: int | None
    data: Mapping[str, Any]


@dataclass
class RunContext:
    run_id: uuid.UUID
    graph: Graph
    kind: WorkflowKind
    ports: EnginePorts
    cancel: CancelToken
    inputs: Mapping[str, Any] = field(default_factory=dict[str, Any])
    workflow_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    # sys.*에 더할 값(챗플로우의 query, files, conversation_id 등). run_id·kind·now는 엔진이 채운다.
    system: Mapping[str, Any] = field(default_factory=dict[str, Any])
    env: Mapping[str, Any] = field(default_factory=dict[str, Any])
    # env 가운데 비밀값의 이름. 그 값은 이벤트·노드 런·체크포인트에서 ***가 된다(9.4).
    secret_env: Collection[str] = ()
    conversation: Mapping[str, Any] = field(default_factory=dict[str, Any])
    # 대화 변수 선언 타입. None이면 검증이 conv 이름을 따지지 않는다.
    conversation_types: Mapping[str, VarType] | None = None
    limits: EngineLimits = field(default_factory=EngineLimits)
    # waiting_input으로 멈춘 런을 이어 갈 때 그때 저장한 체크포인트.
    resume: Checkpoint | None = None
    # settings.mask_secrets_in_outputs. end 출력·answer 텍스트에도 비밀값 마스킹을 건다.
    mask_outputs: bool = True
    # 단일 노드 실행(6.1)이면 그 노드 id. 런 출력은 그 노드의 출력이다.
    single_node: str | None = None
    # 단일 노드 실행에서 노드가 참조하는 변수의 값. 키는 "<scope>.<name>"이다.
    seed: Mapping[str, Any] = field(default_factory=dict[str, Any])
    # 챗플로우 대화의 앞 턴(오래된 것부터). 실행기가 메모리 창만큼 읽어 둔다.
    history: Sequence[Turn] = ()
    # 재개할 때 기다리던 노드에 넘길 응답(human_input 폼). agent 노드는 자식 런이 답을 받는다.
    reply: NodeReply | None = None


class NodeRunRecord(BaseModel):
    """wf_node_run 행 하나의 내용. 마스킹은 했고 64KB·8KB 자르기는 저장하는 쪽이 한다."""

    node_run_id: uuid.UUID
    node_id: str
    node_type: str
    title: str
    index: int
    attempt: int
    status: NodeRunStatus
    iteration_node_id: str | None = None
    iteration_index: int | None = None
    inputs: dict[str, Any] = Field(default_factory=dict[str, Any])
    outputs: dict[str, Any] = Field(default_factory=dict[str, Any])
    process: dict[str, Any] = Field(default_factory=dict[str, Any])
    error: NodeError | None = None
    usage: UsageTotal | None = None
    warnings: list[str] = Field(default_factory=list[str])
    child_run_id: uuid.UUID | None = None
    branch: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class WfEvent:
    type: str
    # 버스에 싣는 값(마스킹·4KB 미리보기 적용). UUID는 그대로 두고 버스의 jsonable이 바꾼다.
    data: dict[str, Any]
    # node.started·node.completed에만 있다. wf_node_run에 쓸 전체 값.
    node_run: NodeRunRecord | None = None


class RunError(BaseModel):
    code: str
    message: str
    node_id: str | None = None
    retryable: bool = False


class PendingWait(BaseModel):
    """wf_run.pending_interaction(3.7). 병렬 가지가 같이 멈춰도 하나(노드 id·index 순)만 담는다.

    approval·question은 agent 노드의 자식 런 승인·질문(interaction), form은 human_input 노드의
    폼이다.
    """

    node_id: str
    iteration_index: int | None = None
    child_run_id: uuid.UUID | None = None
    interaction: PendingInteractionOut | None = None
    kind: WfPendingKind
    form: WorkflowFormData | None = None


class RunOutcome(BaseModel):
    status: WorkflowEndStatus
    # end 출력의 합. mask_outputs면 마스킹했다. 단일 노드 실행이면 그 노드의 출력이다.
    outputs: dict[str, Any] = Field(default_factory=dict[str, Any])
    # answer 노드들이 낸 글을 완료 순서로 이은 것. 런이 멈추거나 실패했으면 그때까지 흘린 글이다.
    answer: str = ""
    # 대화 변수 최종값. 서비스가 성공한 런에서만 wf_conversation에 쓴다.
    conversation: dict[str, Any] = Field(default_factory=dict[str, Any])
    usage: UsageTotal = Field(default_factory=UsageTotal)
    error: RunError | None = None
    pending: PendingWait | None = None
    checkpoint: Checkpoint | None = None
    node_runs: int = 0
    duration_ms: int = 0


def new_id() -> uuid.UUID:
    return uuid6.uuid7()


def _failed(code: str, message: str, *, retryable: bool = False) -> NodeResult:
    return NodeResult(
        status="failed", error=NodeError(code=code, message=message, retryable=retryable)
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class _Done:
    record: NodeRunRecord
    result: NodeResult
    # 오류 정책·재시도와 관계없이 런을 끝내는 실패(node_not_implemented).
    fatal: bool
    started: float


class _NodeCtx:
    """NodeContext 구현. 노드가 읽은 변수와 경고를 모아 노드 런 기록에 남긴다."""

    def __init__(
        self, execution: "_Execution", frame: FrameState, node: Node, record: NodeRunRecord
    ) -> None:
        self._execution = execution
        self._node = node
        context = execution.ctx
        self.run_id = context.run_id
        self.node_id = node.id
        self.node_run_id = record.node_run_id
        self.attempt = record.attempt
        self.kind: WorkflowKind = context.kind
        self.iteration_index = frame.index
        self._stream = frame.key(node.id)
        self.pool = execution.view(frame)
        self.ports = context.ports
        self.cancel = context.cancel
        self.run_inputs = context.inputs
        self.history = context.history
        # answer 노드면 엔진이 시작 전에 채운다.
        self.streamed = ""
        self.warnings: list[str] = []
        self.seen: dict[str, Any] = {}

    async def emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        if event_type not in NODE_EVENT_TYPES:
            raise ValueError(f"노드가 낼 수 없는 이벤트다: {event_type}")
        payload = {**data, "node_id": self.node_id, "node_run_id": self.node_run_id}
        await self._execution.emit_node_event(event_type, payload, stream=self._stream)

    def render(self, template: str, variables: Mapping[str, Any] | None = None) -> str:
        context = dict(variables) if variables is not None else self.pool.template_context()
        rendered = render(template, context)
        for name in rendered.missing:
            self.warn(f"missing_variable: {name}")
        return rendered.text

    def resolve(self, selector: str, expect: VarType | None = None) -> Any:
        try:
            parsed = parse_selector(selector)
        except SelectorError as exc:
            raise NodeFailure("invalid_selector", str(exc)) from None
        found = self.pool.lookup(parsed)
        if not found.found:
            self.warn(f"missing_variable: {parsed}")
        value = found.value
        if expect is not None:
            try:
                value = coerce(value, expect)
            except VariableError as exc:
                raise NodeFailure(exc.code, f"{parsed}: {exc.message}") from None
        self.seen[str(parsed)] = value
        return value

    def is_skipped(self, selector: str) -> bool:
        try:
            parsed = parse_selector(selector)
        except SelectorError:
            return False
        return self.pool.is_skipped(parsed.scope)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    async def run_frames(self, plan: FramePlan, state: Mapping[str, Any] | None) -> FramesOutcome:
        return await self._execution.run_frames(self._node, plan, state)

    async def run_loop(self, plan: LoopPlan, state: Mapping[str, Any] | None) -> LoopOutcome:
        return await self._execution.run_loop(self._node, plan, state)


_Item = tuple[WfEvent, asyncio.Future[None]]


def _key_order(key: str) -> tuple[str, int]:
    node_id, _, index = key.partition("@")
    return node_id, int(index) if index else -1


class _Execution:
    def __init__(self, registry: NodeRegistry, ctx: RunContext) -> None:
        self.registry = registry
        self.ctx = ctx
        self.outcome: RunOutcome | None = None
        self.masker = Masker(
            ctx.env[name] for name in ctx.secret_env if isinstance(ctx.env.get(name), str)
        )
        # (이벤트 타입, 프레임 키) → 그 스트림의 마스커와 끝부분을 낼 때 붙일 id들.
        self._deltas: dict[tuple[str, str], tuple[StreamMasker, dict[str, Any]]] = {}
        self.pool = VariablePool(ctx.limits.pool_max_bytes)
        self.analysis: Analysis | None = None
        self.outputs: dict[str, Any] = {}
        self.answer = ""
        self.conversation: dict[str, Any] = dict(ctx.conversation)
        self.usage = UsageTotal()
        self.node_runs = 0
        self._queue: asyncio.Queue[_Item | None] = asyncio.Queue()
        self._closed = False
        self._budget_tokens = 0
        self._next_index = 0
        self._elapsed_before = 0
        self._started = 0.0
        self._last_checkpoint = 0.0
        self._failure: RunError | None = None
        self._cancelled = False
        # 런이 멈추기로 하면(_fail, 취소) 켠다. 어느 프레임이 멈추게 했든 모든 프레임이 바로 깬다.
        self._stop_event = asyncio.Event()
        self._stop_waiter: asyncio.Future[Any] | None = None
        # 종료(shutdown) 실패는 체크포인트 없이 끝낸다(3.6).
        self._skip_checkpoint = False
        self._slots = asyncio.Semaphore(max(1, ctx.limits.max_parallel_nodes))
        self._type_slots: dict[str, asyncio.Semaphore] = {}
        self._inner: dict[str, frozenset[str]] = {}
        self._inflight: dict[str, NodeRunRecord] = {}
        self._node_state: dict[str, dict[str, Any]] = {}
        self._waiting: dict[str, WaitingRun] = {}
        # 승인·질문·폼을 실제로 기다리는 노드(agent, human_input). 반복을 멈춘 iteration·loop는
        # _waiting에만 있다.
        self._pending: dict[str, PendingInteractionOut | WorkflowFormData] = {}
        reply = ctx.reply
        self._reply = reply
        self._reply_key = (
            state_key(reply.node_id, reply.iteration_index) if reply is not None else None
        )
        self._resume_waiting: dict[str, WaitingRun] = {}
        self._resume_state: dict[str, dict[str, Any]] = {}
        self._resume_nodes: dict[str, str] = {}
        self._resume_edges: dict[str, str] = {}
        self._cancel_waiter: asyncio.Future[None] | None = None
        self._root: FrameState | None = None
        # 챗플로우에서 미리 흘릴 answer가 있을 때만 있다.
        self.streams: AnswerStreams | None = None
        # 돌고 있거나 입력 대기로 멈춘 반복 프레임. 키는 "<iteration id>@<index>"다.
        self._subframes: dict[str, FrameState] = {}

    async def stream(self) -> AsyncGenerator[WfEvent]:
        task = asyncio.create_task(self._main())
        ack: asyncio.Future[None] | None = None
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                event, ack = item
                yield event
                if not ack.done():
                    ack.set_result(None)
                ack = None
            await task
        finally:
            # 소비자가 중간에 그만두면 엔진을 취소한다. 닫힌 뒤의 emit은 기다리지 않고 돌아온다.
            self._closed = True
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if ack is not None and not ack.done():
                ack.cancel()
            while not self._queue.empty():
                rest = self._queue.get_nowait()
                if rest is not None and not rest[1].done():
                    rest[1].cancel()

    async def _emit(
        self, event_type: str, data: dict[str, Any], node_run: NodeRunRecord | None = None
    ) -> None:
        if self._closed:
            return
        ack: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((WfEvent(event_type, data, node_run), ack))
        await ack

    async def emit_node_event(
        self, event_type: str, data: dict[str, Any], *, stream: str | None = None
    ) -> None:
        text = data.get("text")
        if event_type == "answer.delta" and not self.ctx.mask_outputs:
            await self._emit(event_type, data)
        elif event_type in STREAMED_EVENTS and stream is not None and isinstance(text, str):
            await self._emit_streamed(event_type, stream, data)
        else:
            await self._emit(event_type, self.masker.mask(data))
        if event_type == "node.text_delta" and self.streams is not None and isinstance(text, str):
            await self.streams.delta(str(data["node_id"]), text)

    async def emit_answer(self, node_id: str, text: str) -> None:
        """answer 노드가 돌기 전에 흘리는 글(3.8). 노드 런이 아직 없어 node_run_id가 없다.

        루트 프레임의 answer만 미리 흘리므로 스트림 키가 노드 id이고, 노드가 시작해 나머지를 낼 때도
        같은 키를 써서 미리 흘린 끝과 노드가 낸 앞에 걸친 비밀값도 가린다.
        """
        data: dict[str, Any] = {"text": text, "node_id": node_id}
        if not self.ctx.mask_outputs:
            await self._emit("answer.delta", data)
            return
        await self._emit_streamed("answer.delta", node_id, data)

    async def _emit_streamed(self, event_type: str, stream: str, data: dict[str, Any]) -> None:
        key = (event_type, stream)
        ids = {name: value for name, value in data.items() if name != "text"}
        found = self._deltas.get(key)
        masker = found[0] if found is not None else StreamMasker(self.masker)
        self._deltas[key] = (masker, ids)
        text = masker.feed(str(data["text"]))
        if text:
            await self._emit(event_type, {**ids, "text": text})

    async def _flush_streamed(self, stream: str | None = None) -> None:
        """붙잡아 둔 델타 끝부분을 낸다. stream이 없으면 남은 것 전부(런이 멈추거나 끝날 때)."""
        for key in [key for key in self._deltas if stream is None or key[1] == stream]:
            masker, ids = self._deltas.pop(key)
            rest = masker.flush()
            if rest:
                await self._emit(key[0], {**ids, "text": rest})

    async def _main(self) -> None:
        try:
            self.outcome = await self._run()
        finally:
            self._queue.put_nowait(None)

    @property
    def _stopping(self) -> bool:
        return self._failure is not None or self._cancelled

    def _fail(self, error: RunError) -> None:
        if not self._stopping:
            self._failure = error
            self._stop_event.set()

    def _elapsed_ms(self) -> int:
        return self._elapsed_before + int((self.ctx.ports.monotonic() - self._started) * 1000)

    def _remaining(self) -> float:
        return self.ctx.limits.max_run_seconds - self._elapsed_ms() / 1000

    def view(self, frame: FrameState) -> PoolView:
        iteration_id = frame.graph.iteration_id
        inner = self._inner.get(iteration_id, frozenset()) if iteration_id else frozenset[str]()
        return PoolView(self.pool, inner=inner, index=frame.index)

    async def _run(self) -> RunOutcome:
        ctx = self.ctx
        self._started = ctx.ports.monotonic()
        resume = ctx.resume
        analysis = analyze(
            ctx.graph,
            ctx.kind,
            registry=self.registry,
            env_names=list(ctx.env),
            conversation_vars=ctx.conversation_types,
        )
        self.analysis = analysis
        shown = ctx.seed if ctx.single_node is not None else ctx.inputs
        inputs, truncated = preview(self.masker.mask(dict(shown)))
        await self._emit(
            "workflow.started",
            {
                "workflow_id": ctx.workflow_id,
                "version_id": ctx.version_id,
                "kind": ctx.kind,
                "inputs_preview": inputs,
                "truncated": truncated,
                "resumed": resume is not None,
                "node_count": len(ctx.graph.nodes),
            },
        )
        if self.masker.short:
            # 8자 미만 비밀값은 일반 글자와 겹치기 쉬워 가리지 않는다(9.4). 값은 남기지 않는다.
            log.warning(
                "workflow.secret_too_short", run_id=str(ctx.run_id), count=len(self.masker.short)
            )
        errors = self._blocking_issues(analysis)
        if errors:
            message = f"그래프 검증 오류 {len(errors)}건. 첫 오류: {errors[0].message}"
            self._skip_checkpoint = True
            return await self._finish(
                None, RunError(code="graph_invalid", message=message, node_id=errors[0].node_id)
            )
        self._inner = inner_nodes(analysis.frames.values())
        if ctx.single_node is not None:
            root = FrameState(single_frame(ctx.single_node))
        else:
            root = FrameState(analysis.frames[None])
        self._root = root
        try:
            self._prepare_pool(resume)
            if resume is None and ctx.single_node is not None:
                self._seed_pool(ctx.single_node)
        except PoolLimitError as exc:
            self._skip_checkpoint = True
            return await self._finish(None, RunError(code="variable_pool_limit", message=str(exc)))
        except VariableError as exc:
            self._skip_checkpoint = True
            return await self._finish(None, RunError(code=exc.code, message=exc.message))
        if ctx.kind == "chatflow" and ctx.single_node is None:
            streams = AnswerStreams(
                analysis,
                root,
                context=lambda: self.view(root).template_context(),
                emit=self.emit_answer,
            )
            self.streams = streams if streams.active else None
        ready = self._restore(root, resume) if resume is not None else root.begin()
        if resume is not None and resume.waiting:
            # 반복을 멈춘 iteration보다 실제로 답을 기다리던 노드를 알린다.
            keys = [key for key in resume.waiting if not self._is_container(key)]
            first = min(keys or resume.waiting, key=_key_order)
            await self._emit("workflow.resumed", {"node_id": first.partition("@")[0]})
        self._last_checkpoint = ctx.ports.monotonic()
        self._cancel_waiter = asyncio.ensure_future(ctx.cancel.wait())
        self._stop_waiter = asyncio.ensure_future(self._stop_event.wait())
        try:
            await self._run_frame(root, ready)
        finally:
            self._cancel_waiter.cancel()
            self._stop_waiter.cancel()
        return await self._finish(root, self._failure)

    def _blocking_issues(self, analysis: Analysis) -> list[Issue]:
        """실행을 막는 검증 오류. 단일 노드 실행은 그 노드와 하위 그래프의 오류만 본다."""
        target = self.ctx.single_node
        if target is None:
            return analysis.errors
        if target not in analysis.nodes:
            message = f"노드 {target}이(가) 그래프에 없다"
            return [Issue(level="error", code="unknown_node", message=message, node_id=target)]
        inner = analysis.frames.get(target)
        scope = {target, *(inner.nodes if inner is not None else ())}
        return [issue for issue in analysis.errors if issue.node_id in scope]

    def _is_container(self, key: str) -> bool:
        runner = self._analysis().runners.get(key.partition("@")[0])
        return runner is not None and runner.container

    def _prepare_pool(self, resume: Checkpoint | None) -> None:
        ctx = self.ctx
        types: dict[str, VarType] = dict(ctx.conversation_types or {})
        if resume is not None:
            self.pool.restore(
                resume.pool,
                skipped=resume.skipped,
                empty=resume.empty,
                types={SYS: sys_variables(ctx.kind), CONV: types},
            )
        else:
            system: dict[str, Any] = {
                "workflow_id": str(ctx.workflow_id) if ctx.workflow_id else None,
                "run_id": str(ctx.run_id),
                "kind": ctx.kind,
                "now": _iso(ctx.ports.now()),
                **ctx.system,
            }
            declared = sys_variables(ctx.kind)
            for name, value in system.items():
                self.pool.put(SYS, name, value, declared.get(name))
            for name in types:
                self.conversation.setdefault(name, None)
            for name, value in self.conversation.items():
                self.pool.put(CONV, name, value, types.get(name))
        for name, value in ctx.env.items():
            env_type: VarType | None = "string" if isinstance(value, str) else None
            self.pool.put(ENV, name, value, env_type)

    def _seed_pool(self, target: str) -> None:
        """단일 노드 실행에서 사용자가 준 변수 값을 풀에 넣는다(6.1)."""
        ctx = self.ctx
        inner = self._inner.get(target, frozenset[str]())
        declared = sys_variables(ctx.kind)
        types: Mapping[str, VarType] = ctx.conversation_types or {}
        for key, value in ctx.seed.items():
            try:
                selector = parse_selector(key)
            except SelectorError as exc:
                raise VariableError("invalid_inputs", str(exc)) from None
            scope, name = selector.scope, selector.name
            if selector.path or scope == ENV or scope == target or scope in inner:
                raise VariableError("invalid_inputs", f"{key}은(는) 미리 줄 수 있는 변수가 아니다")
            if scope == SYS:
                self.pool.put(SYS, name, value, declared.get(name))
            elif scope == CONV:
                self.pool.put(CONV, name, value, types.get(name))
                self.conversation[name] = value
            else:
                self.pool.put(scope, name, value)

    def _restore(self, root: FrameState, resume: Checkpoint) -> list[str]:
        self.outputs = dict(resume.outputs)
        self.answer = resume.answer_text
        self.conversation = dict(resume.conv_vars)
        self.usage = resume.usage.model_copy()
        self._budget_tokens = resume.budget_tokens
        self._next_index = resume.next_index
        self.node_runs = resume.node_runs
        self._elapsed_before = resume.elapsed_ms
        self._resume_waiting = dict(resume.waiting)
        self._resume_state = dict(resume.node_state)
        self._resume_nodes = dict(resume.node_states)
        self._resume_edges = dict(resume.edge_states)
        ready = root.restore(resume.node_states, resume.edge_states)
        if self.streams is not None:
            self.streams.restore(resume.answer_cursors)
        return ready

    def _on_cancel(self) -> None:
        if self._stopping:
            return
        if self.ctx.cancel.reason == "shutdown":
            self._skip_checkpoint = True
            # 1단계 종료 실패처럼 다시 보내면 되는 실패다.
            self._failure = RunError(
                code="shutdown", message="서버가 종료되어 런을 멈췄다", retryable=True
            )
        else:
            self._cancelled = True
        self._stop_event.set()

    async def _run_frame(self, frame: FrameState, initial: list[str]) -> None:
        queue: deque[str] = deque(initial)
        running: dict[asyncio.Task[_Done], str] = {}
        order: dict[asyncio.Task[_Done], int] = {}
        try:
            while True:
                if self.ctx.cancel.is_set():
                    self._on_cancel()
                if self._remaining() <= 0:
                    self._fail(self._timeout())
                # 반복 프레임은 자기 실패에서 멈추고 런은 iteration의 error_mode가 정한다.
                halted = self._stopping or frame.failure is not None
                while queue and not halted:
                    node_id = queue.popleft()
                    frame.mark(node_id, "running")
                    task = asyncio.create_task(self._execute(frame, node_id))
                    running[task] = node_id
                    order[task] = len(order)
                if not running or halted:
                    break
                waiters: set[asyncio.Future[Any]] = {*running}
                for waiter in (self._cancel_waiter, self._stop_waiter):
                    if waiter is not None:
                        waiters.add(waiter)
                done, _ = await asyncio.wait(
                    waiters, timeout=max(self._remaining(), 0), return_when=asyncio.FIRST_COMPLETED
                )
                finished = [t for t in sorted(running, key=order.__getitem__) if t in done]
                for task in finished:
                    node_id = running.pop(task)
                    await self._settle(frame, node_id, task.result(), queue)
                await self._maybe_checkpoint()
        finally:
            await self._stop_running(frame, running, order)

    async def _stop_running(
        self,
        frame: FrameState,
        running: dict[asyncio.Task[_Done], str],
        order: dict[asyncio.Task[_Done], int],
    ) -> None:
        if not running:
            return
        runners = self._analysis().runners
        for task, node_id in running.items():
            # 런이 멈추면 iteration의 반복 프레임은 스스로 깨어 노드를 정리하고 끝난다. 그 정리를
            # 취소로 끊지 않도록 container 노드는 기다린다. 소비자가 떠났으면 모두 취소한다.
            if self._closed or not self._stopping or not runners[node_id].container:
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        for task in sorted(running, key=order.__getitem__):
            node_id = running[task]
            if not task.cancelled() and task.exception() is None:
                # 멈추기로 한 사이에 끝난 노드는 실제 결과로 남긴다.
                await self._settle(frame, node_id, task.result(), deque())
                continue
            if not task.cancelled():
                log.error(
                    "workflow.node_task_failed",
                    run_id=str(self.ctx.run_id),
                    node_id=node_id,
                    exc_info=task.exception(),
                )
            record = self._inflight.pop(frame.key(node_id), None)
            if record is None:
                # 자리를 기다리다 취소된 노드는 시작한 적이 없다.
                frame.mark(node_id, "pending")
                continue
            frame.mark(node_id, "cancelled")
            elapsed = self.ctx.ports.now() - record.started_at
            await self._complete(
                record,
                status="cancelled",
                outputs={},
                inputs={},
                duration_ms=max(0, int(elapsed.total_seconds() * 1000)),
            )

    def _timeout(self) -> RunError:
        limit = self.ctx.limits.max_run_seconds
        return RunError(code="timeout", message=f"런 시간 상한({limit:g}초)을 넘었다")

    @contextlib.asynccontextmanager
    async def _slot(self, runner: AnyRunner) -> AsyncGenerator[None]:
        # 타입별 자리를 먼저 잡아야 code 노드가 차례를 기다리는 동안 전역 자리를 쥐지 않는다.
        async with contextlib.AsyncExitStack() as stack:
            if runner.concurrency is not None:
                slots = self._type_slots.setdefault(
                    runner.type, asyncio.Semaphore(runner.concurrency)
                )
                await stack.enter_async_context(slots)
            if not runner.container:
                await stack.enter_async_context(self._slots)
            yield

    def _new_record(
        self, frame: FrameState, node: Node, attempt: int, resumed: WaitingRun | None
    ) -> NodeRunRecord:
        if resumed is not None:
            node_run_id, index, attempt = resumed.node_run_id, resumed.index, resumed.attempt
        else:
            node_run_id, index = new_id(), self._next_index
            self._next_index += 1
            self.node_runs += 1
        return NodeRunRecord(
            node_run_id=node_run_id,
            node_id=node.id,
            node_type=node.type,
            title=node.title,
            index=index,
            attempt=attempt,
            status="running",
            iteration_node_id=frame.graph.iteration_id,
            iteration_index=frame.index,
            started_at=self.ctx.ports.now(),
        )

    async def _execute(self, frame: FrameState, node_id: str) -> _Done:
        analysis = self._analysis()
        node = analysis.nodes[node_id]
        runner = analysis.runners[node_id]
        config = analysis.configs[node_id]
        key = frame.key(node_id)
        resumed = self._resume_waiting.pop(key, None)
        state = self._resume_state.pop(key, {}) if resumed is not None else None
        if resumed is not None and self._reply is not None and key == self._reply_key:
            state = {**(state or {}), REPLY_STATE_KEY: dict(self._reply.data)}
            self._reply = None
        async with self._slot(runner):
            attempt = 1
            while True:
                record = self._new_record(frame, node, attempt, resumed)
                child = resumed.child_run_id if resumed is not None else None
                session = resumed.child_session_id if resumed is not None else None
                resumed = None
                self._inflight[key] = record
                ctx = _NodeCtx(self, frame, node, record)
                streams = self.streams if frame is self._root else None
                if streams is not None:
                    ctx.streamed = streams.take(node.id)
                await self._emit("node.started", self._started_data(record, child, session), record)
                if streams is not None:
                    await streams.started(node.id)
                started = self.ctx.ports.monotonic()
                result, fatal = await self._invoke(runner, ctx, config, state, record)
                result = self._normalize(runner, config, result, ctx, node)
                error = result.error
                if (
                    result.status != "failed"
                    or error is None
                    or fatal
                    or not error.retryable
                    or record.attempt > node.retry.max_attempts
                ):
                    return _Done(record, result, fatal, started)
                self._account(runner, result.usage)
                await self._complete_result(record, result, started, outputs={}, branch=None)
                self._inflight.pop(key, None)
                attempt = record.attempt + 1
                await self._emit(
                    "node.retrying",
                    {
                        "node_id": node.id,
                        "node_run_id": record.node_run_id,
                        "attempt": attempt,
                        "reason": error.code,
                    },
                )
                await asyncio.sleep(node.retry.interval_ms / 1000)
                state = None

    async def _invoke(
        self,
        runner: AnyRunner,
        ctx: _NodeCtx,
        config: BaseModel,
        state: dict[str, Any] | None,
        record: NodeRunRecord,
    ) -> tuple[NodeResult, bool]:
        timeout = runner.timeout_sec(config)
        try:
            async with asyncio.timeout(timeout):
                return await runner.run(ctx, config, state), False
        except NodeNotImplemented as exc:
            return _failed(exc.code, exc.message), True
        except NodeFailure as exc:
            return _failed(exc.code, exc.message, retryable=exc.retryable), False
        except (TemplateError, VariableError) as exc:
            return _failed(exc.code, exc.message), False
        except TimeoutError:
            message = f"노드 시간 상한({timeout:g}초)을 넘었다" if timeout else "시간이 다 됐다"
            return _failed("timeout", message, retryable=True), False
        except Exception as exc:
            log.exception(
                "workflow.node_crashed",
                run_id=str(self.ctx.run_id),
                node_id=record.node_id,
                node_type=record.node_type,
            )
            return _failed("node_error", f"{type(exc).__name__}: {exc}"), False

    def _normalize(
        self, runner: AnyRunner, config: BaseModel, result: NodeResult, ctx: _NodeCtx, node: Node
    ) -> NodeResult:
        update: dict[str, Any] = {
            "warnings": [*ctx.warnings, *(w for w in result.warnings if w not in ctx.warnings)],
            "inputs": result.inputs if result.inputs is not None else dict(ctx.seen),
        }
        try:
            if result.status == "succeeded":
                update["outputs"] = self._typed_outputs(runner, config, result.outputs)
                update["branch"] = self._branch(runner, config, result.branch)
                for value in result.conv_updates.values():
                    measure(value)
                if result.loop_updates:
                    self._check_loop_updates(node, result.loop_updates)
            elif result.status == "failed" and result.error is None:
                update["error"] = NodeError(code="node_error", message="노드가 이유 없이 실패했다")
            elif (
                result.status == "waiting_input" and result.pending is None and not runner.container
            ):
                # 반복을 멈춘 iteration은 승인·질문이 하위 노드에 있다.
                raise NodeFailure("invalid_result", "waiting_input 결과에 승인·질문 내용이 없다")
        except (NodeFailure, VariableError) as exc:
            return _failed(exc.code, exc.message).model_copy(
                update={"warnings": update["warnings"], "inputs": update["inputs"]}
            )
        return result.model_copy(update=update)

    def _check_loop_updates(self, node: Node, updates: Mapping[str, Any]) -> None:
        # 노드를 품은 loop의 반복 변수만 바꿀 수 있다. 단일 노드 실행도 부모 loop 자리를 쓴다.
        variables = self._analysis().inner_outputs.get(node.parent_id or "")
        unknown = sorted(set(updates) - set(variables or {}))
        if variables is None or unknown:
            names = ", ".join(unknown) or ", ".join(sorted(updates))
            raise NodeFailure(
                "invalid_result", f"이 노드를 품은 loop의 반복 변수가 아니다: {names}"
            )
        for value in updates.values():
            measure(value)

    def _typed_outputs(
        self, runner: AnyRunner, config: BaseModel, outputs: Mapping[str, Any]
    ) -> dict[str, Any]:
        declared: dict[str, VarType] = {spec.name: spec.type for spec in runner.outputs(config)}
        unknown = sorted(set(outputs) - set(declared))
        if unknown:
            raise NodeFailure("invalid_output", f"선언하지 않은 출력이다: {', '.join(unknown)}")
        typed: dict[str, Any] = {}
        for name, declared_type in declared.items():
            try:
                value = coerce(outputs.get(name), declared_type)
                measure(value)
            except VariableError as exc:
                raise NodeFailure(exc.code, f"출력 {name}: {exc.message}") from None
            typed[name] = value
        return typed

    def _branch(self, runner: AnyRunner, config: BaseModel, branch: str | None) -> str | None:
        handles = runner.handles(config)
        if branch is None:
            if runner.branching:
                raise NodeFailure("invalid_branch", "분기 노드가 핸들을 고르지 않았다")
            return None
        if branch not in handles:
            raise NodeFailure("invalid_branch", f"없는 핸들을 골랐다: {branch}")
        return branch if runner.branching else None

    def _account(self, runner: AnyRunner, usage: UsageTotal | None) -> None:
        if usage is None:
            return
        self._budget_tokens += usage.input_tokens + usage.output_tokens
        if not runner.counts_usage:
            return
        total = self.usage
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens
        total.reasoning_tokens += usage.reasoning_tokens
        total.cached_input_tokens += usage.cached_input_tokens
        total.estimated = total.estimated or usage.estimated
        if usage.cost_usd is not None:
            total.cost_usd = (total.cost_usd or 0.0) + usage.cost_usd

    def _check_budget(self) -> None:
        limits = self.ctx.limits
        if self._budget_tokens > limits.max_total_tokens:
            message = f"런 토큰 예산({limits.max_total_tokens})을 넘었다({self._budget_tokens})"
            self._fail(RunError(code="token_budget", message=message))
        cost = self.usage.cost_usd
        if limits.max_cost_usd is not None and cost is not None and cost > limits.max_cost_usd:
            message = f"런 비용 상한(${limits.max_cost_usd})을 넘었다(${cost:.4f})"
            self._fail(RunError(code="token_budget", message=message))

    async def _settle(
        self, frame: FrameState, node_id: str, done: _Done, queue: deque[str]
    ) -> None:
        analysis = self._analysis()
        node = analysis.nodes[node_id]
        runner = analysis.runners[node_id]
        config = analysis.configs[node_id]
        key = frame.key(node_id)
        result = done.result
        self._inflight.pop(key, None)
        self._account(runner, result.usage)
        status = result.status
        handles: list[str] | None = None
        outputs = result.outputs
        branch: str | None = result.branch
        strategy = node.error.strategy
        handled = status == "failed" and not done.fatal and strategy != "fail"
        try:
            if status == "succeeded":
                handles = [result.branch or OUT_HANDLE]
                self.pool.put_outputs(key, outputs, self._output_types(runner, config))
            elif status == "skipped":
                handles = []
                self.pool.mark_skipped(key)
            elif handled and strategy == "default":
                handles = [OUT_HANDLE]
                outputs = self._default_outputs(runner, config, node)
                self.pool.put_outputs(key, outputs, self._output_types(runner, config))
            elif handled:
                handles = [FAIL_HANDLE]
                branch = FAIL_HANDLE
                self.pool.mark_empty(key)
            if status == "succeeded":
                if result.run_outputs:
                    self.outputs.update(result.run_outputs)
                if result.answer:
                    self.answer += result.answer
                for name, value in result.conv_updates.items():
                    self.pool.assign(name, value)
                    self.conversation[name] = value
                if result.loop_updates and node.parent_id is not None:
                    for name, value in result.loop_updates.items():
                        self.pool.assign(name, value, scope=node.parent_id)
        except PoolLimitError as exc:
            self._fail(RunError(code="variable_pool_limit", message=str(exc), node_id=node_id))
            handles = None
        record_outputs = outputs if outputs else (result.run_outputs or {})
        await self._complete_result(
            done.record, result, done.started, outputs=record_outputs, branch=branch
        )
        if status == "waiting_input":
            frame.mark(node_id, "waiting_input")
            self._node_state[key] = dict(result.state)
            self._waiting[key] = WaitingRun(
                node_run_id=done.record.node_run_id,
                index=done.record.index,
                attempt=done.record.attempt,
                child_run_id=result.child_run_id,
                child_session_id=result.child_session_id,
            )
            if result.pending is not None:
                self._pending[key] = result.pending
        elif handles is None:
            frame.mark(node_id, status)
            if status == "failed" and result.error is not None:
                error = result.error
                message = self.masker.text(error.message)
                if frame.graph.iteration_id is not None and not done.fatal:
                    if frame.failure is None:
                        masked = error.model_copy(update={"message": message})
                        frame.failure = FrameFailure(node_id, masked)
                else:
                    self._fail(
                        RunError(
                            code=error.code,
                            message=message,
                            node_id=node_id,
                            retryable=error.retryable,
                        )
                    )
        else:
            transition = frame.complete(node_id, status, handles)
            for skipped in transition.skipped:
                await self._record_skipped(frame, skipped)
            queue.extend(transition.ready)
        self._check_budget()
        if self.streams is not None and frame is self._root:
            await self.streams.settled()

    def _output_types(self, runner: AnyRunner, config: BaseModel) -> dict[str, VarType | None]:
        return {spec.name: spec.type for spec in runner.outputs(config)}

    def _default_outputs(self, runner: AnyRunner, config: BaseModel, node: Node) -> dict[str, Any]:
        declared = self._output_types(runner, config)
        values: dict[str, Any] = {}
        for name, declared_type in declared.items():
            value = node.error.default_outputs.get(name)
            try:
                values[name] = coerce(value, declared_type)
            except VariableError:
                # 검증이 type_mismatch 경고를 이미 냈다. 맞지 않는 기본값은 null로 둔다.
                values[name] = None
        return values

    async def _record_skipped(self, frame: FrameState, node_id: str) -> None:
        node = self._analysis().nodes[node_id]
        self.pool.mark_skipped(frame.key(node_id))
        record = self._new_record(frame, node, 1, None)
        await self._complete(record, status="skipped", outputs={}, inputs={}, duration_ms=0)

    def _started_data(
        self,
        record: NodeRunRecord,
        child_run_id: uuid.UUID | None,
        child_session_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        return {
            "node_run_id": record.node_run_id,
            "node_id": record.node_id,
            "node_type": record.node_type,
            "title": record.title,
            "index": record.index,
            "iteration_id": record.iteration_node_id,
            "iteration_index": record.iteration_index,
            "attempt": record.attempt,
            "child_run_id": child_run_id,
            "child_session_id": child_session_id,
        }

    async def _complete_result(
        self,
        record: NodeRunRecord,
        result: NodeResult,
        started: float,
        *,
        outputs: Mapping[str, Any],
        branch: str | None,
    ) -> None:
        await self._complete(
            record,
            status=result.status,
            outputs=outputs,
            inputs=result.inputs or {},
            duration_ms=int((self.ctx.ports.monotonic() - started) * 1000),
            error=result.error,
            usage=result.usage,
            warnings=result.warnings,
            process=result.process,
            branch=branch,
            child_run_id=result.child_run_id,
        )

    async def _complete(
        self,
        record: NodeRunRecord,
        *,
        status: NodeRunStatus,
        outputs: Mapping[str, Any],
        inputs: Mapping[str, Any],
        duration_ms: int,
        error: NodeError | None = None,
        usage: UsageTotal | None = None,
        warnings: list[str] | None = None,
        process: Mapping[str, Any] | None = None,
        branch: str | None = None,
        child_run_id: uuid.UUID | None = None,
    ) -> None:
        mask = self.masker
        masked_error = (
            error.model_copy(update={"message": mask.text(error.message)}) if error else None
        )
        masked_outputs: dict[str, Any] = mask.mask(dict(outputs))
        full = record.model_copy(
            update={
                "status": status,
                "inputs": mask.mask(dict(inputs)),
                "outputs": masked_outputs,
                "process": mask.mask(dict(process or {})),
                "error": masked_error,
                "usage": usage,
                "warnings": list(warnings or []),
                "child_run_id": child_run_id or record.child_run_id,
                "branch": branch,
                "ended_at": self.ctx.ports.now(),
                "duration_ms": duration_ms,
            }
        )
        outputs_preview, truncated = preview(masked_outputs)
        await self._flush_streamed(state_key(record.node_id, record.iteration_index))
        await self._emit(
            "node.completed",
            {
                "node_run_id": record.node_run_id,
                "node_id": record.node_id,
                "status": status,
                "outputs_preview": outputs_preview,
                "truncated": truncated,
                "error": masked_error.model_dump() if masked_error else None,
                "usage": usage.model_dump() if usage else None,
                "duration_ms": duration_ms,
                "branch": branch,
                "warnings": list(warnings or []),
            },
            full,
        )

    async def run_frames(
        self, owner: Node, plan: FramePlan, state: Mapping[str, Any] | None
    ) -> FramesOutcome:
        """owner의 하위 그래프를 plan.items마다 돈다(3.5).

        끝난 반복은 index 순으로 모은다. 반복 안에서 입력 대기가 생기면 그 반복을 멈춘 채 두고 새
        반복을 시작하지 않으며, 돌던 반복이 다 멈추거나 끝나면 waiting_input으로 돌아간다. 재개하면
        iteration.started가 다시 나가고 멈췄던 반복부터 iteration.next가 이어진다.
        """
        graph = self._analysis().frames.get(owner.id)
        if graph is None or graph.entry is None:
            raise NodeFailure("invalid_iteration", "하위 그래프에 iteration_start가 없다")
        try:
            progress = IterationProgress.model_validate(dict(state or {}))
            output = parse_selector(plan.output)
        except (ValidationError, SelectorError) as exc:
            raise NodeFailure("invalid_iteration", f"반복을 이어 갈 수 없다: {exc}") from None
        total = len(plan.items)
        results: dict[int, FrameResult] = {}
        for key, done_status in progress.done.items():
            index = int(key) if key.isdigit() else -1
            if 0 <= index < total:
                value = (
                    self._frame_value(graph, output, index) if done_status == "succeeded" else None
                )
                results[index] = FrameResult(index=index, status=done_status, value=value)
        resumed = set(progress.stalled)
        todo = deque(index for index in range(total) if index not in results)
        started = self.ctx.ports.monotonic()
        await self._emit("iteration.started", {"node_id": owner.id, "total": total})
        limit = max(1, plan.parallel)
        running: dict[asyncio.Task[None], FrameState] = {}
        stalled: list[FrameState] = []
        failure: FrameResult | None = None
        try:
            while True:
                while (
                    todo
                    and len(running) < limit
                    and failure is None
                    and not stalled
                    and not self._stopping
                ):
                    index = todo.popleft()
                    frame = FrameState(graph, index)
                    self._subframes[frame.frame_key] = frame
                    await self._emit("iteration.next", {"node_id": owner.id, "index": index})
                    ready = await self._open_frame(
                        owner,
                        frame,
                        {"item": plan.items[index], "index": index},
                        {"item": None, "index": "number"},
                        index in resumed,
                    )
                    if ready is None:
                        self._subframes.pop(frame.frame_key, None)
                        break
                    running[asyncio.create_task(self._run_frame(frame, ready))] = frame
                if not running:
                    break
                finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)

                def frame_index(task: asyncio.Task[None]) -> int:
                    return running[task].index or 0

                for task in sorted(finished, key=frame_index):
                    frame = running.pop(task)
                    task.result()
                    index = frame.index or 0
                    if frame.failure is not None:
                        failed = FrameResult(
                            index=index,
                            status="failed",
                            error=frame.failure.error,
                            node_id=frame.failure.node_id,
                        )
                        results[index] = failed
                        await self._close_frame(frame)
                        if plan.stop_on_failure and failure is None:
                            failure = failed
                    elif self._stopping:
                        # 실패 체크포인트에 남도록 프레임 표에서 빼지 않는다.
                        continue
                    elif frame.waiting:
                        stalled.append(frame)
                    else:
                        value = self._frame_value(graph, output, index)
                        results[index] = FrameResult(index=index, status="succeeded", value=value)
                        self._subframes.pop(frame.frame_key, None)
                if failure is not None and running:
                    # terminate: 돌던 다른 반복을 멈춘다. 그 안에서 만든 자식 런도 노드가 끝낸다.
                    cancelled = list(running.values())
                    await self._cancel_frames(running)
                    for frame in cancelled:
                        await self._close_frame(frame)
        finally:
            await self._cancel_frames(running)
        ordered = [results[index] for index in sorted(results)]
        elapsed = progress.elapsed_ms + int((self.ctx.ports.monotonic() - started) * 1000)
        if self._stopping:
            return FramesOutcome(status="cancelled", results=ordered)
        if failure is None and stalled:
            progress = IterationProgress(
                done={str(item.index): item.status for item in ordered},
                stalled=[frame.index or 0 for frame in stalled],
                elapsed_ms=elapsed,
            )
            return FramesOutcome(
                status="waiting_input", results=ordered, state=progress.model_dump(mode="json")
            )
        for frame in stalled:
            # terminate로 끝난 iteration의 멈춘 반복은 이어 갈 일이 없다.
            await self._close_frame(frame)
        await self._emit(
            "iteration.completed",
            {
                "node_id": owner.id,
                "count": total,
                "failed_count": sum(1 for item in ordered if item.status == "failed"),
                "duration_ms": elapsed,
            },
        )
        if failure is not None:
            return FramesOutcome(status="failed", results=ordered, failure=failure)
        return FramesOutcome(status="succeeded", results=ordered)

    async def run_loop(
        self, owner: Node, plan: LoopPlan, state: Mapping[str, Any] | None
    ) -> LoopOutcome:
        """owner(loop)의 하위 그래프를 반복 하나씩 차례로 돈다(2.4, 3.5).

        반복 변수는 처음 시작할 때 풀의 "<loop id>" 자리에 넣고, 반복이 끝날 때마다 plan.until로
        종료 조건을 본다. 반복 안에서 입력 대기가 생기면 그 반복을 멈춘 채 waiting_input으로
        돌아가고, 재개하면 iteration.started를 다시 내고 멈췄던 반복을 체크포인트의 @index 상태로
        잇는다.
        """
        graph = self._analysis().frames.get(owner.id)
        if graph is None or graph.entry is None:
            raise NodeFailure("invalid_loop", "하위 그래프에 loop_start가 없다")
        try:
            progress = LoopProgress.model_validate(dict(state or {}))
        except ValidationError as exc:
            raise NodeFailure("invalid_loop", f"반복을 이어 갈 수 없다: {exc}") from None
        scope = owner.id
        current = self.pool.scope(scope)
        try:
            if current is None:
                for name, value in plan.variables.items():
                    self.pool.put(scope, name, value, plan.types.get(name))
            else:
                # 체크포인트에서 되살린 값에는 선언 타입이 없다. 이어 쓰는 값에 타입만 다시 붙인다.
                for name, variable in dict(current).items():
                    self.pool.put(scope, name, variable.value, plan.types.get(name))
        except PoolLimitError as exc:
            self._fail(RunError(code="variable_pool_limit", message=str(exc), node_id=owner.id))
            return LoopOutcome(status="cancelled")
        except VariableError as exc:
            raise NodeFailure(exc.code, exc.message) from None
        inner = self._inner.get(owner.id, frozenset[str]())
        started = self.ctx.ports.monotonic()
        total = plan.max_rounds
        await self._emit("iteration.started", {"node_id": owner.id, "total": total})
        index = progress.round
        resumed = state is not None
        stopped: Literal["condition", "limit"] = "limit"

        def elapsed() -> int:
            return progress.elapsed_ms + int((self.ctx.ports.monotonic() - started) * 1000)

        async def completed(count: int, failed: int) -> None:
            await self._emit(
                "iteration.completed",
                {
                    "node_id": owner.id,
                    "count": count,
                    "failed_count": failed,
                    "duration_ms": elapsed(),
                },
            )

        while index < total:
            if self._stopping:
                return LoopOutcome(status="cancelled", rounds=index)
            frame = FrameState(graph, index)
            self._subframes[frame.frame_key] = frame
            await self._emit("iteration.next", {"node_id": owner.id, "index": index})
            ready = await self._open_frame(
                owner, frame, {"index": index}, {"index": "number"}, resumed
            )
            resumed = False
            if ready is None:
                self._subframes.pop(frame.frame_key, None)
                return LoopOutcome(status="cancelled", rounds=index)
            await self._run_frame(frame, ready)
            if frame.failure is not None:
                await self._close_frame(frame)
                failure = FrameResult(
                    index=index,
                    status="failed",
                    error=frame.failure.error,
                    node_id=frame.failure.node_id,
                )
                await completed(index + 1, 1)
                return LoopOutcome(status="failed", rounds=index + 1, failure=failure)
            if self._stopping:
                # 실패 체크포인트에 남도록 프레임 표에서 빼지 않는다.
                return LoopOutcome(status="cancelled", rounds=index)
            if frame.waiting:
                waiting = LoopProgress(round=index, elapsed_ms=elapsed())
                return LoopOutcome(
                    status="waiting_input",
                    rounds=index,
                    state=waiting.model_dump(mode="json"),
                )
            self._subframes.pop(frame.frame_key, None)
            index += 1
            if plan.until is None:
                continue
            try:
                done = plan.until(PoolView(self.pool, inner=inner, index=index - 1))
            except (NodeFailure, VariableError) as exc:
                error = NodeError(code=exc.code, message=self.masker.text(exc.message))
                await completed(index, 1)
                failure = FrameResult(index=index - 1, status="failed", error=error)
                return LoopOutcome(status="failed", rounds=index, failure=failure)
            if done:
                stopped = "condition"
                break
        variables = {name: var.value for name, var in (self.pool.scope(scope) or {}).items()}
        # 반복 변수 자리를 비워야 loop 노드의 최종 출력을 같은 이름으로 넣을 수 있다.
        self.pool.drop(scope)
        await completed(index, 0)
        return LoopOutcome(status="succeeded", rounds=index, variables=variables, stopped=stopped)

    async def _open_frame(
        self,
        owner: Node,
        frame: FrameState,
        values: Mapping[str, Any],
        types: Mapping[str, VarType | None],
        resumed: bool,
    ) -> list[str] | None:
        """반복 하나를 시작할 준비를 한다. values는 진입 자리의 출력(item·index)이다.

        돌릴 노드를 돌려주고, 런이 실패하면 None.
        """
        graph = frame.graph
        entry = graph.entry
        if entry is None:
            return []
        if resumed:
            ready = frame.restore(self._resume_nodes, self._resume_edges)
            if frame.nodes.get(entry) != "pending":
                return ready
        key = frame.key(entry)
        if self.pool.scope(key) is None:
            try:
                self.pool.put_outputs(key, values, types)
            except PoolLimitError as exc:
                self._fail(RunError(code="variable_pool_limit", message=str(exc), node_id=owner.id))
                return None
            except VariableError as exc:
                frame.failure = FrameFailure(entry, NodeError(code=exc.code, message=exc.message))
                return []
        transition = frame.enter()
        for skipped in transition.skipped:
            await self._record_skipped(frame, skipped)
        return transition.ready

    def _frame_value(self, graph: FrameGraph, output: Selector, index: int) -> Any:
        inner = self._inner.get(graph.iteration_id or "", frozenset[str]())
        return PoolView(self.pool, inner=inner, index=index).lookup(output).value

    async def _cancel_frames(self, running: dict[asyncio.Task[None], FrameState]) -> None:
        if not running:
            return
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        running.clear()

    async def _close_frame(self, frame: FrameState) -> None:
        """더 이어 가지 않을 반복을 정리한다. 그 안에서 입력을 기다리던 노드는 취소로 남긴다."""
        self._subframes.pop(frame.frame_key, None)
        nodes = self._analysis().nodes
        for node_id in frame.graph.nodes:
            key = frame.key(node_id)
            self._pending.pop(key, None)
            self._node_state.pop(key, None)
            waiting = self._waiting.pop(key, None)
            if waiting is None:
                continue
            frame.mark(node_id, "cancelled")
            record = self._new_record(frame, nodes[node_id], waiting.attempt, waiting)
            await self._complete(
                record,
                status="cancelled",
                outputs={},
                inputs={},
                duration_ms=0,
                child_run_id=waiting.child_run_id,
            )
            await self._cancel_child(waiting.child_run_id)

    async def _cancel_child(self, child_run_id: uuid.UUID | None) -> None:
        tasks = self.ctx.ports.tasks
        if child_run_id is None or tasks is None:
            return
        try:
            await tasks.cancel(child_run_id)
        except Exception:
            # 자식 정리는 덤이다. 남은 자식은 런이 끝날 때 실행기·복구가 다시 본다.
            log.warning(
                "workflow.child_cancel_failed",
                run_id=str(self.ctx.run_id),
                child_run_id=str(child_run_id),
                exc_info=True,
            )

    async def _maybe_checkpoint(self) -> None:
        interval = self.ctx.limits.checkpoint_interval_sec
        if interval <= 0 or self._stopping:
            return
        if self.ctx.ports.monotonic() - self._last_checkpoint >= interval:
            await self._checkpoint()

    async def _checkpoint(self) -> Checkpoint:
        nodes: dict[str, str] = {}
        edges: dict[str, str] = {}
        if self._root is not None:
            nodes, edges = self._root.snapshot()
        frames: list[dict[str, Any]] = []
        for frame in self._subframes.values():
            frame_nodes, frame_edges = frame.snapshot()
            nodes.update(frame_nodes)
            edges.update(frame_edges)
            frames.append({"iteration_id": frame.graph.iteration_id, "index": frame.index})
        pool = self.pool.snapshot()
        mask = self.masker
        node_state = {key: dict(value) for key, value in self._node_state.items()}
        waiting = dict(self._waiting)
        outputs, answer = dict(self.outputs), self.answer
        conversation, usage = dict(self.conversation), self.usage.model_copy()
        budget, next_index, node_runs = self._budget_tokens, self._next_index, self.node_runs
        elapsed = self._elapsed_ms()
        cursors = self.streams.cursors() if self.streams is not None else {}

        # 풀이 수 MB면 마스킹·검증이 루프를 붙잡으므로 스레드에서 만든다(3.7).
        def build() -> Checkpoint:
            return Checkpoint(
                pool=mask.mask(pool.dump(exclude=(ENV,))),
                skipped=sorted(pool.skipped),
                empty=sorted(pool.empty),
                node_states=nodes,
                edge_states=edges,
                node_state=mask.mask(node_state),
                waiting=waiting,
                frames=frames,
                outputs=mask.mask(outputs),
                answer_text=mask.text(answer),
                answer_cursors=cursors,
                conv_vars=mask.mask(conversation),
                usage=usage,
                budget_tokens=budget,
                next_index=next_index,
                node_runs=node_runs,
                elapsed_ms=elapsed,
            )

        checkpoint = await asyncio.to_thread(build)
        await self.ctx.ports.save_checkpoint(checkpoint)
        self._last_checkpoint = self.ctx.ports.monotonic()
        return checkpoint

    def _completed_data(
        self, status: WorkflowEndStatus, outputs: dict[str, Any] | None, error: RunError | None
    ) -> dict[str, Any]:
        return {
            "status": status,
            "outputs": outputs,
            "usage_total": self.usage.model_dump(),
            "node_runs": self.node_runs,
            "duration_ms": self._elapsed_ms(),
            "error": error.model_dump() if error else None,
        }

    def _single_outputs(self) -> None:
        target = self.ctx.single_node
        if target is None:
            return
        variables = self.pool.scope(target)
        if variables:
            self.outputs = {**self.outputs, **{name: var.value for name, var in variables.items()}}

    async def _finish(self, root: FrameState | None, failure: RunError | None) -> RunOutcome:
        if self._reply is not None:
            # 응답을 받을 노드가 체크포인트에서 기다리고 있지 않았다. 다시 물으면 사람이 다시
            # 답한다.
            log.warning("workflow.reply_unused", run_id=str(self.ctx.run_id), key=self._reply_key)
        if failure is None and not self._cancelled and self._waiting and not self._pending:
            failure = RunError(
                code="invalid_wait", message="입력 대기로 멈췄는데 기다리는 승인·질문이 없다"
            )
        self._single_outputs()
        await self._flush_streamed()
        mask = self.masker if self.ctx.mask_outputs else Masker()
        outputs: dict[str, Any] = mask.mask(self.outputs)
        prestreamed = self.streams.pending() if self.streams is not None else ""
        answer = mask.text(self.answer + prestreamed)
        checkpoint: Checkpoint | None = None
        pending: PendingWait | None = None
        if failure is not None:
            status: WorkflowEndStatus = "failed"
            if root is not None and not self._skip_checkpoint:
                checkpoint = await self._checkpoint()
            await self._emit("workflow.error", failure.model_dump())
            await self._emit("workflow.completed", self._completed_data(status, None, failure))
        elif self._cancelled:
            status = "cancelled"
            await self._emit("workflow.completed", self._completed_data(status, None, None))
        elif self._pending and root is not None:
            status = "waiting_input"
            key = min(self._pending, key=_key_order)
            node_id, _, index = key.partition("@")
            pending = self._pending_wait(key, node_id, int(index) if index else None)
            checkpoint = await self._checkpoint()
            interaction = pending.interaction
            form = pending.form
            await self._emit(
                "workflow.waiting_input",
                {
                    "node_id": node_id,
                    "iteration_index": pending.iteration_index,
                    "child_run_id": pending.child_run_id,
                    "interaction": (
                        self.masker.mask(
                            interaction.model_dump(mode="json", exclude={"created_at"})
                        )
                        if interaction is not None
                        else None
                    ),
                    "kind": pending.kind,
                    "form": form.model_dump(mode="json") if form is not None else None,
                },
            )
            await self._emit("workflow.completed", self._completed_data(status, None, None))
        else:
            status = "succeeded"
            await self._emit("workflow.completed", self._completed_data(status, outputs, None))
        return RunOutcome(
            status=status,
            outputs=outputs if status == "succeeded" else {},
            answer=answer,
            conversation=dict(self.conversation),
            usage=self.usage.model_copy(),
            error=failure,
            pending=pending,
            checkpoint=checkpoint,
            node_runs=self.node_runs,
            duration_ms=self._elapsed_ms(),
        )

    def _pending_wait(self, key: str, node_id: str, iteration_index: int | None) -> PendingWait:
        waiting_on = self._pending[key]
        if isinstance(waiting_on, WorkflowFormData):
            # 안내문은 풀 값으로 렌더한 글이라 이벤트와 대기 기록 모두 비밀값을 가린다.
            form = WorkflowFormData.model_validate(self.masker.mask(waiting_on.model_dump()))
            return PendingWait(
                node_id=node_id, iteration_index=iteration_index, kind="form", form=form
            )
        waiting = self._waiting.get(key)
        return PendingWait(
            node_id=node_id,
            iteration_index=iteration_index,
            child_run_id=waiting.child_run_id if waiting is not None else None,
            interaction=waiting_on,
            kind=waiting_on.kind,
        )

    def _analysis(self) -> Analysis:
        if self.analysis is None:
            raise RuntimeError("그래프 분석 전이다")
        return self.analysis


class EngineRun:
    """Engine.run()의 결과. async for로 이벤트를 받고 다 받은 뒤 outcome을 읽는다.

    중간에 그만두려면 aclose()를 부른다. 실행 중인 노드를 취소하고 돌아온다.
    """

    def __init__(self, execution: _Execution) -> None:
        self._execution = execution
        self._events = execution.stream()

    def __aiter__(self) -> "EngineRun":
        return self

    async def __aenter__(self) -> "EngineRun":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def __anext__(self) -> WfEvent:
        return await anext(self._events)

    async def aclose(self) -> None:
        await self._events.aclose()

    @property
    def outcome(self) -> RunOutcome:
        if self._execution.outcome is None:
            raise RuntimeError("런이 아직 끝나지 않았다")
        return self._execution.outcome


class Engine:
    def __init__(self, registry: NodeRegistry | None = None) -> None:
        self.registry = registry or default_registry()

    def run(self, ctx: RunContext) -> EngineRun:
        return EngineRun(_Execution(self.registry, ctx))
