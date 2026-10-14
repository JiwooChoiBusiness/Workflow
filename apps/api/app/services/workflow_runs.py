"""워크플로우 런 생성·중지·재개·조회(2단계 설계 3.6, 3.7, 6.1).

런은 HTTP 요청 수명과 분리된다. 여기서는 wf_run 행을 queued로 만들고 워크플로우 레인에 넣는
데까지 하고, 실행은 워크플로우 레인 Supervisor 워커가 WorkflowRunExecutor로 한다. 큐 항목은
1단계와 같은 "<workspace_id>:<run_id>"다.

agent 노드가 승인·질문을 기다리면 워크플로우 런도 waiting_input으로 끝난다. 응답은 (a) 자식 런에
provide_input → (b) 워크플로우 런을 queued(resume)로 다시 넣기 순서다. 자식이 다른 경로로 입력
대기에서 벗어나도(1단계 /runs/{rid}/input, 대기 만료, 중지) RunService가 WorkflowParents를 불러
(b)를 한다. (b)는 조건부 갱신이라 두 번 불려도 한 번만 넣는다.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import structlog
from pydantic import ValidationError

from app.agent.interactions import InteractionReply
from app.agent.task_service import AgentTaskService
from app.config import Settings
from app.db.engine import Database
from app.db.models import WfNodeRun, WfRun
from app.db.models.enums import RunStatus, WfRunTrigger, WorkflowKind
from app.db.repositories.workflows import (
    WfNodeRunRepository,
    WfRunEventRepository,
    WfRunRepository,
    WfVersionRepository,
    WorkflowRepository,
)
from app.db.types import new_id, utcnow
from app.runner.supervisor import Supervisor, SupervisorClosed
from app.schemas.events import TERMINAL_EVENT_TYPES, NodeError, UsageTotal
from app.schemas.run import RunEventList
from app.schemas.wf_run import (
    WfNodeRunDetail,
    WfNodeRunList,
    WfNodeRunOut,
    WfPendingOut,
    WfRunCreate,
    WfRunDetail,
    WfRunErrorOut,
    WfRunList,
    WfRunOut,
)
from app.schemas.workflow import WorkflowSettings
from app.services.errors import ServiceError, not_found
from app.services.runs import event_out, run_key
from app.services.workflow_files import WorkflowFileService
from app.services.workflows import (
    conversation_types,
    conversation_vars,
    issue_details,
    live_agent_ids,
    parse_graph,
    parse_settings,
    stored_env,
)
from app.storage.base import StorageBackend
from app.streaming.event_bus import EventBus, RunRef, Subscription
from app.workflow import graph as graph_check
from app.workflow.nodes.base import NodeRegistry
from app.workflow.nodes.start import InputError, StartConfig, coerce_inputs

log = structlog.get_logger("app.services.workflow_runs")

TERMINAL_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})
# 아직 끝나지 않은 자식 런(1단계 run).
OPEN_CHILD_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_INPUT)
MAX_EVENTS_PAGE = 1000


def wf_run_ref(run: WfRun) -> RunRef:
    return RunRef(
        run_id=run.id,
        session_id=None,
        workspace_id=run.workspace_id,
        conversation_id=run.conversation_id,
        kind="wf_run",
    )


def pending_of(run: WfRun) -> WfPendingOut | None:
    if not run.pending_interaction:
        return None
    try:
        return WfPendingOut.model_validate(run.pending_interaction)
    except ValidationError:
        log.warning("wf_run.pending_invalid", run_id=str(run.id))
        return None


def wf_run_out(run: WfRun) -> WfRunOut:
    return WfRunOut(
        id=run.id,
        workflow_id=run.workflow_id,
        wf_version_id=run.wf_version_id,
        kind=run.kind,  # pyright: ignore[reportArgumentType]
        trigger=run.trigger,  # pyright: ignore[reportArgumentType]
        status=run.status,  # pyright: ignore[reportArgumentType]
        inputs=run.inputs or {},
        outputs=run.outputs,
        error=WfRunErrorOut.model_validate(run.error) if run.error else None,
        usage=UsageTotal.model_validate(run.usage or {}),
        node_run_count=run.node_run_count,
        pending_interaction=pending_of(run),
        limits=run.limits or {},
        conversation_id=run.conversation_id,
        message_id=run.message_id,
        external_user=run.external_user,
        cancel_requested_at=run.cancel_requested_at,
        queued_at=run.queued_at,
        started_at=run.started_at,
        ended_at=run.ended_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _node_fields(row: WfNodeRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "wf_run_id": row.wf_run_id,
        "node_id": row.node_id,
        "node_type": row.node_type,
        "title": row.title,
        "index": row.index,
        "iteration_node_id": row.iteration_node_id,
        "iteration_index": row.iteration_index,
        "attempt": row.attempt,
        "status": row.status,
        "truncated": row.truncated,
        "error": NodeError.model_validate(row.error) if row.error else None,
        "usage": UsageTotal.model_validate(row.usage) if row.usage else None,
        "warnings": list(row.warnings or []),
        "child_run_id": row.child_run_id,
        "branch": row.branch,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "duration_ms": row.duration_ms,
    }


def node_run_out(row: WfNodeRun) -> WfNodeRunOut:
    return WfNodeRunOut.model_validate(_node_fields(row))


def node_run_detail(row: WfNodeRun) -> WfNodeRunDetail:
    return WfNodeRunDetail.model_validate(
        {
            **_node_fields(row),
            "inputs": row.inputs,
            "outputs": row.outputs,
            "process": row.process or {},
        }
    )


def elapsed_ms(run: WfRun) -> int:
    # 엔진이 잰 실행 시간(입력 대기 제외)은 체크포인트에만 남는다. 없으면 0이다.
    state = run.state or {}
    value = state.get("elapsed_ms")
    return value if isinstance(value, int) else 0


def completed_data(run: WfRun, *, status: str, duration_ms: int | None = None) -> dict[str, Any]:
    """서비스가 엔진 밖에서 런을 끝낼 때(중지·복구·실행 전 실패) 내는 workflow.completed."""
    return {
        "status": status,
        "outputs": None,
        "truncated": False,
        "usage_total": UsageTotal.model_validate(run.usage or {}).model_dump(mode="json"),
        "node_runs": run.node_run_count,
        "duration_ms": elapsed_ms(run) if duration_ms is None else duration_ms,
        "error": run.error,
    }


def run_limits(settings: Settings, workflow_settings: WorkflowSettings) -> dict[str, Any]:
    """런을 만들 때 적용하는 한도. 실행기가 그대로 EngineLimits로 바꾼다."""
    limits = workflow_settings.limits
    return {
        "max_run_seconds": limits.max_run_seconds,
        "max_total_tokens": limits.max_total_tokens,
        "max_cost_usd": limits.max_cost_usd,
        "max_parallel_nodes": settings.wf_max_parallel_nodes,
        "pool_max_bytes": settings.wf_variable_pool_max_mb * 2**20,
        "checkpoint_interval_sec": settings.wf_checkpoint_interval_sec,
    }


async def enqueue(
    bus: EventBus, supervisor: Supervisor, ref: RunRef, *, subscribe: bool = False
) -> Subscription | None:
    """workflow.queued를 먼저 발행해 채널을 연 뒤 구독하고 큐에 넣는다.

    구독이 큐 투입보다 먼저라 짧은 런이 구독 전에 끝나도 이벤트를 놓치지 않는다. 종료 중이라 넣지
    못하면 SupervisorClosed를 그대로 던진다(행은 queued로 남는다).
    """
    position = int(supervisor.stats()["queued"]) + 1
    [queued] = await bus.publish(ref, "workflow.queued", {"position": position})
    subscription = bus.subscribe(ref, after=queued.seq - 1) if subscribe else None
    try:
        supervisor.enqueue(run_key(ref.workspace_id, ref.run_id))
    except SupervisorClosed:
        if subscription is not None:
            subscription.close()
        raise
    return subscription


async def fail_unstarted(db: Database, bus: EventBus, ref: RunRef, now: datetime) -> None:
    """종료 중이라 큐에 넣지 못한 새 런을 failed(shutdown)로 끝낸다."""
    error: dict[str, Any] = {
        "code": "shutdown",
        "message": "서버가 종료 중이라 런을 시작하지 못했다",
        "retryable": True,
    }
    async with db.write() as session:
        run = await WfRunRepository(session, ref.workspace_id).get(ref.run_id)
        if run is None or run.status != RunStatus.QUEUED:
            return
        run.status = RunStatus.FAILED
        run.error = error
        run.ended_at = now
        data = completed_data(run, status=RunStatus.FAILED)
    await bus.publish(ref, "workflow.error", error)
    await bus.publish(ref, "workflow.completed", data)
    bus.end(ref.run_id)


@dataclass
class Requeued:
    ref: RunRef
    # subscribe=True였으면 workflow.queued부터 받는 구독. 종료 중이라 넣지 못했으면 None.
    subscription: Subscription | None


async def requeue_waiting(
    db: Database,
    bus: EventBus,
    supervisor: Supervisor,
    workspace_id: uuid.UUID,
    wf_run_id: uuid.UUID,
    *,
    child_run_id: uuid.UUID | None = None,
    subscribe: bool = False,
) -> Requeued | None:
    """waiting_input인 런을 queued(resume)로 바꿔 다시 넣는다. 넣지 않았으면 None.

    child_run_id를 주면 그 자식을 기다리는 런일 때만 넣는다. 이미 재개됐거나 끝난 런은 건너뛴다.
    종료 중이라 큐에 못 넣으면 queued로 두고 다음 기동의 복구가 넣는다.
    """
    async with db.write() as session:
        run = await WfRunRepository(session, workspace_id).get(wf_run_id)
        if run is None or run.status != RunStatus.WAITING_INPUT:
            return None
        pending = pending_of(run)
        if child_run_id is not None and (pending is None or pending.child_run_id != child_run_id):
            return None
        run.status = RunStatus.QUEUED
        run.resume_payload = {"node_id": pending.node_id if pending is not None else None}
        run.cancel_requested_at = None
        ref = wf_run_ref(run)
    try:
        subscription = await enqueue(bus, supervisor, ref, subscribe=subscribe)
    except SupervisorClosed:
        log.info("wf_run.resume_deferred", run_id=str(wf_run_id))
        return Requeued(ref=ref, subscription=None)
    log.info("wf_run.resumed", run_id=str(wf_run_id), child_run_id=str(child_run_id))
    return Requeued(ref=ref, subscription=subscription)


async def cancel_children(
    db: Database, tasks: AgentTaskService | None, workspace_id: uuid.UUID, wf_run_id: uuid.UUID
) -> int:
    """런의 agent 노드가 만든 자식 런 중 아직 끝나지 않은 것을 중지한다(3.4, 3.7의 4번).

    노드가 취소될 때 스스로 자식을 끝내지 못했어도 런이 실패·취소로 끝나면 여기서 정리한다.
    """
    if tasks is None:
        return 0
    async with db.read() as session:
        pairs = await WfNodeRunRepository(session, workspace_id).children_of(
            [wf_run_id], statuses=OPEN_CHILD_STATUSES
        )
    stopped = 0
    for child_run_id in dict.fromkeys(child for _, child in pairs):
        try:
            await tasks.cancel(child_run_id)
            stopped += 1
        except ServiceError as exc:
            log.warning(
                "wf_run.child_cancel_failed",
                run_id=str(wf_run_id),
                child_run_id=str(child_run_id),
                code=exc.code,
            )
    return stopped


class WorkflowParents:
    """RunService의 ChildRunListener.

    자식 런으로 그 자식을 기다리는 워크플로우 런을 찾아 다시 넣는다.

    역조회는 wf_node_run.child_run_id 인덱스로 한다. run.input.correlation(JSON)은 조건에 쓰지
    않는다(1단계 5장 규칙).
    """

    def __init__(self, db: Database, bus: EventBus, supervisor: Supervisor) -> None:
        self._db = db
        self._bus = bus
        self._supervisor = supervisor

    async def resume_parents(
        self, workspace_id: uuid.UUID, child_run_ids: Sequence[uuid.UUID]
    ) -> int:
        async with self._db.read() as session:
            pairs = await WfNodeRunRepository(session, workspace_id).parents_of(child_run_ids)
        resumed = 0
        for child_run_id, wf_run_id in dict.fromkeys(pairs):
            requeued = await requeue_waiting(
                self._db,
                self._bus,
                self._supervisor,
                workspace_id,
                wf_run_id,
                child_run_id=child_run_id,
            )
            if requeued is not None:
                resumed += 1
        return resumed


@dataclass
class StartedWfRun:
    run_id: uuid.UUID
    subscription: Subscription | None


class WorkflowRunService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        bus: EventBus,
        supervisor: Supervisor,
        storage: StorageBackend,
        tasks: AgentTaskService | None = None,
        registry: NodeRegistry | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._bus = bus
        # 워크플로우 레인.
        self._supervisor = supervisor
        self._storage = storage
        self._tasks = tasks
        self._registry = registry
        self._clock = clock

    def ensure_accepting(self) -> None:
        if self._supervisor.shutting_down or self._bus.closed:
            raise ServiceError(503, "shutting_down", "서버가 종료 중이라 새 런을 받지 않는다")

    async def start_draft_run(
        self,
        workspace_id: uuid.UUID,
        workflow_id: uuid.UUID,
        body: WfRunCreate,
        *,
        subscribe: bool,
    ) -> StartedWfRun:
        """초안 그래프로 미리보기 런을 만들어 워크플로우 레인에 넣는다."""
        self.ensure_accepting()
        async with self._db.read() as session:
            workflow = await WorkflowRepository(session, workspace_id).get_active(workflow_id)
            if workflow is None:
                raise not_found("워크플로우")
            if workflow.kind != WorkflowKind.WORKFLOW:
                raise ServiceError(409, "wrong_kind", "챗플로우는 이 경로가 아니라 대화로 실행한다")
            draft = await WfVersionRepository(session, workspace_id).get_draft(workflow.id)
            if draft is None:
                raise not_found("워크플로우 초안")
            agents = await live_agent_ids(session, workspace_id, draft.graph)
        graph = parse_graph(draft.graph)
        if graph is None:
            raise ServiceError(422, "draft_invalid", "저장된 그래프를 읽지 못했다")
        analysis = graph_check.analyze(
            graph,
            "workflow",
            registry=self._registry,
            env_names=[item.name for item in stored_env(draft.env_vars)],
            conversation_vars=conversation_types(
                workflow.kind, conversation_vars(draft.conversation_vars)
            ),
            agent_ids=agents,
        )
        if not analysis.ok:
            raise ServiceError(
                422,
                "draft_invalid",
                "초안에 검증 오류가 있다. 고쳐 저장한 뒤 다시 실행한다",
                details=issue_details(analysis.issues),
            )
        start_id = next(
            node.id for node in graph.nodes if node.type == "start" and node.parent_id is None
        )
        start = cast(StartConfig, analysis.configs[start_id])
        inputs = await WorkflowFileService(self._db, self._storage).resolve_inputs(
            workspace_id, workflow.id, start.fields, body.inputs
        )
        try:
            coerce_inputs(start.fields, inputs)
        except InputError as exc:
            raise ServiceError(
                422, "invalid_inputs", "런 입력이 올바르지 않다", details=exc.errors
            ) from None
        limits = run_limits(self._settings, parse_settings(draft.settings))
        now = self._clock()
        async with self._db.write() as session:
            runs = WfRunRepository(session, workspace_id)
            run = runs.add(
                WfRun(
                    id=new_id(),
                    workflow_id=workflow.id,
                    wf_version_id=draft.id,
                    kind=workflow.kind,
                    trigger=WfRunTrigger.PREVIEW,
                    status=RunStatus.QUEUED,
                    # 엔진이 재기동 뒤에도 같은 입력으로 시작해야 해서 가리지 않고 둔다. 이벤트와
                    # 노드 런 기록은 엔진이 비밀값을 가린다.
                    inputs=inputs,
                    usage=UsageTotal().model_dump(mode="json"),
                    node_run_count=0,
                    limits=limits,
                    graph_hash=draft.graph_hash,
                    queued_at=now,
                )
            )
            await runs.flush()
            ref = wf_run_ref(run)
        try:
            subscription = await enqueue(self._bus, self._supervisor, ref, subscribe=subscribe)
        except SupervisorClosed:
            await fail_unstarted(self._db, self._bus, ref, self._clock())
            raise ServiceError(
                503, "shutting_down", "서버가 종료 중이라 새 런을 받지 않는다"
            ) from None
        log.info("wf_run.created", run_id=str(ref.run_id), workflow_id=str(workflow_id))
        return StartedWfRun(run_id=ref.run_id, subscription=subscription)

    async def stop(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> WfRunOut:
        now = self._clock()
        finalized: dict[str, Any] | None = None
        async with self._db.write() as session:
            run = await WfRunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("워크플로우 런")
            if run.status in TERMINAL_STATUSES:
                raise ServiceError(
                    409, "run_not_active", "이미 끝난 런이다", details={"status": run.status}
                )
            ref = wf_run_ref(run)
            run.cancel_requested_at = now
            if run.status in (RunStatus.QUEUED, RunStatus.WAITING_INPUT):
                # 워커가 잡지 않은 런은 여기서 바로 끝낸다. 큐에 남은 항목은 워커가 꺼내도 상태가
                # queued가 아니라 건너뛴다.
                finalized = completed_data(run, status=RunStatus.CANCELLED)
                run.status = RunStatus.CANCELLED
                run.pending_interaction = None
                run.resume_payload = None
                run.state = None
                run.ended_at = now
            out = wf_run_out(run)
        signalled = self._supervisor.cancel(run_key(workspace_id, run_id), "user")
        if finalized is not None:
            # 기다리던 자식(승인 대기, 재개 뒤 큐에 있는 자식)도 끝낸다(3.7의 4번).
            await cancel_children(self._db, self._tasks, workspace_id, run_id)
            await self._bus.publish(ref, "workflow.completed", finalized)
            self._bus.end(run_id)
        elif not signalled:
            # running인데 이 프로세스에 실행 중인 태스크가 없다. 기동 복구 전에만 생기는 상태다.
            out = await self._cancel_orphan(ref)
        log.info("wf_run.stop_requested", run_id=str(run_id), status=out.status)
        return out

    async def _cancel_orphan(self, ref: RunRef) -> WfRunOut:
        data: dict[str, Any] | None = None
        async with self._db.write() as session:
            run = await WfRunRepository(session, ref.workspace_id).get(ref.run_id)
            if run is None:
                raise not_found("워크플로우 런")
            if run.status == RunStatus.RUNNING:
                data = completed_data(run, status=RunStatus.CANCELLED)
                run.status = RunStatus.CANCELLED
                run.state = None
                run.ended_at = self._clock()
                await WfNodeRunRepository(session, ref.workspace_id).close_running(
                    run.id, self._clock()
                )
            out = wf_run_out(run)
        if data is not None:
            await cancel_children(self._db, self._tasks, ref.workspace_id, ref.run_id)
            await self._bus.publish(ref, "workflow.completed", data)
            self._bus.end(ref.run_id)
        return out

    async def provide_input(
        self,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        reply: InteractionReply,
        *,
        subscribe: bool,
    ) -> StartedWfRun:
        """승인·질문 응답. 자식 런에 먼저 넘기고 워크플로우 런을 다시 넣는다(3.7의 3번).

        streaming이면 다시 넣으며 낸 workflow.queued부터 준다. 다른 경로가 먼저 다시 넣었으면 대기로
        끝난 앞 구간 뒤부터 준다.
        """
        self.ensure_accepting()
        # 읽기 트랜잭션은 시작한 때의 스냅샷을 보므로 남은 이벤트를 먼저 쓰고 연다.
        await self._bus.flush()
        async with self._db.read() as session:
            run = await WfRunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("워크플로우 런")
            pending = pending_of(run)
            if run.status != RunStatus.WAITING_INPUT or pending is None:
                raise ServiceError(
                    409,
                    "run_not_waiting",
                    "답이나 승인을 기다리는 런이 아니다",
                    details={"status": run.status},
                )
            ref = wf_run_ref(run)
            after = await WfRunEventRepository(session, workspace_id).max_seq(run_id)
        child_run_id = pending.child_run_id
        if child_run_id is None or self._tasks is None:
            raise ServiceError(409, "run_not_waiting", "응답을 넘길 자식 런이 없다")
        try:
            await self._tasks.provide_input(child_run_id, reply)
        except ServiceError as exc:
            if exc.code in ("run_not_waiting", "input_timeout"):
                # 자식이 이미 다른 경로로 답을 받았거나 만료됐다. 부모가 그 결과를 받게 다시 넣는다.
                await self._requeue(workspace_id, run_id, child_run_id, subscribe=False)
            raise
        requeued = await self._requeue(workspace_id, run_id, child_run_id, subscribe=subscribe)
        if requeued is not None:
            subscription = requeued.subscription
        else:
            subscription = self._bus.subscribe(ref, after=after) if subscribe else None
        log.info("wf_run.input_provided", run_id=str(run_id), child_run_id=str(child_run_id))
        return StartedWfRun(run_id=run_id, subscription=subscription)

    async def _requeue(
        self,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        child_run_id: uuid.UUID,
        *,
        subscribe: bool,
    ) -> Requeued | None:
        return await requeue_waiting(
            self._db,
            self._bus,
            self._supervisor,
            workspace_id,
            run_id,
            child_run_id=child_run_id,
            subscribe=subscribe,
        )

    async def get_run(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> WfRunDetail:
        async with self._db.read() as session:
            run = await WfRunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("워크플로우 런")
            rows = await WfNodeRunRepository(session, workspace_id).list_for_run(run.id)
        return WfRunDetail(
            **wf_run_out(run).model_dump(), node_runs=[node_run_out(row) for row in rows]
        )

    async def list_runs(
        self,
        workspace_id: uuid.UUID,
        workflow_id: uuid.UUID,
        *,
        status: str | None = None,
        trigger: str | None = None,
        conversation_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> WfRunList:
        async with self._db.read() as session:
            if await WorkflowRepository(session, workspace_id).get_active(workflow_id) is None:
                raise not_found("워크플로우")
            rows = await WfRunRepository(session, workspace_id).list_filtered(
                workflow_id=workflow_id,
                status=status,
                trigger=trigger,
                conversation_id=conversation_id,
                since=since,
                until=until,
                limit=limit + 1,
                offset=offset,
            )
        more = len(rows) > limit
        return WfRunList(
            items=[wf_run_out(row) for row in rows[:limit]],
            next_offset=offset + limit if more else None,
        )

    async def list_node_runs(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, *, iteration: str | None = None
    ) -> WfNodeRunList:
        async with self._db.read() as session:
            if await WfRunRepository(session, workspace_id).get(run_id) is None:
                raise not_found("워크플로우 런")
            rows = await WfNodeRunRepository(session, workspace_id).list_for_run(
                run_id, iteration_node_id=iteration
            )
        return WfNodeRunList(items=[node_run_out(row) for row in rows])

    async def get_node_run(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, node_run_id: uuid.UUID
    ) -> WfNodeRunDetail:
        async with self._db.read() as session:
            row = await WfNodeRunRepository(session, workspace_id).get_for_run(run_id, node_run_id)
            if row is None:
                raise not_found("노드 런")
        return node_run_detail(row)

    async def _ref(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> RunRef:
        async with self._db.read() as session:
            run = await WfRunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("워크플로우 런")
            return wf_run_ref(run)

    async def stream(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, *, after: int
    ) -> Subscription:
        """after 다음부터의 이벤트.

        진행 중이면 workflow.completed까지 따라가고 아니면 남은 기록만 준다.
        """
        return self._bus.subscribe(await self._ref(workspace_id, run_id), after=after)

    async def list_events(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, *, after: int, limit: int
    ) -> RunEventList:
        ref = await self._ref(workspace_id, run_id)
        limit = max(1, min(limit, MAX_EVENTS_PAGE))
        events = await self._bus.load_persisted(ref, after=after, limit=limit)
        return RunEventList(
            items=[event_out(event) for event in events],
            next_after=events[-1].seq if len(events) == limit else None,
        )

    async def wait_result(self, workspace_id: uuid.UUID, started: StartedWfRun) -> WfRunOut:
        """blocking 응답. workflow.completed(waiting_input 포함)까지 기다린다."""
        subscription = started.subscription
        if subscription is None:
            raise ValueError("wait_result에는 구독이 필요하다")
        finished = False
        async for event in subscription.events():
            if event.type in TERMINAL_EVENT_TYPES:
                finished = True
        if not finished:
            raise ServiceError(503, "shutting_down", "서버가 종료되어 런 결과를 기다리지 못했다")
        async with self._db.read() as session:
            run = await WfRunRepository(session, workspace_id).get(started.run_id)
            if run is None:
                raise not_found("워크플로우 런")
            return wf_run_out(run)
