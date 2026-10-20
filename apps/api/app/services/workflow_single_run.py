"""단일 노드 실행(2단계 설계 6.1, 3.7의 6번).

캔버스에서 노드 하나를 손으로 준 변수 값으로 돌려 본다. 초안 그래프 전체가 아니라 그 노드(iteration
이면 하위 그래프까지)의 검증 오류만 실행을 막는다. 노드가 참조하는 상류 출력·sys·conv 값은
요청 본문 inputs에서 "<scope>.<name>" 키로 받고, 선언 타입이 파일이면 올린 파일 id를 FileRef로
바꾼다. env는 초안의 환경 변수를 그대로 쓴다.

런은 trigger=single_node인 wf_run이고 wf_run.inputs는 {node_id, values}다. 실행은 미리보기 런과
같은 워크플로우 레인·실행기이며, agent 노드가 승인을 기다리면 같은 /wf-runs/{id}/input으로
재개한다. 로그 목록에는 trigger로 고를 때만 나오고 7일 뒤 지운다(4.4).
"""

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime

import structlog

from app.config import Settings
from app.db.engine import Database
from app.db.models import WfRun
from app.db.models.enums import RunStatus, WfRunTrigger, WorkflowKind
from app.db.repositories.workflows import WfRunRepository, WfVersionRepository, WorkflowRepository
from app.db.types import new_id, utcnow
from app.runner.supervisor import Supervisor, SupervisorClosed
from app.schemas.events import UsageTotal
from app.schemas.wf_run import WfNodeRunCreate, WfSingleNodeInputs
from app.schemas.workflow import VarType
from app.schemas.workflow import WorkflowKind as WorkflowKindName
from app.services.errors import ServiceError, not_found
from app.services.workflow_files import WorkflowFileService, bind_run_uploads
from app.services.workflow_runs import (
    StartedWfRun,
    enqueue,
    fail_unstarted,
    run_limits,
    wf_run_ref,
)
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
from app.streaming.event_bus import EventBus
from app.workflow import graph as graph_check
from app.workflow.graph import Analysis
from app.workflow.nodes.base import NodeRegistry
from app.workflow.variables import (
    CONV,
    ENV,
    SYS,
    Selector,
    SelectorError,
    parse_selector,
    sys_variables,
)

log = structlog.get_logger("app.services.workflow_single_run")

# 런 입력을 받는 start와 엔진이 값을 넣는 iteration_start·loop_start는 혼자 돌릴 뜻이 없다.
NOT_RUNNABLE = frozenset({"start", "iteration_start", "loop_start"})
FILE_TYPES: dict[VarType, bool] = {"file": False, "array[file]": True}


def _declared(
    analysis: Analysis,
    kind: WorkflowKindName,
    conversation: Mapping[str, VarType] | None,
    selector: Selector,
) -> tuple[bool, VarType | None]:
    """(있는 변수인지, 선언 타입). 타입을 모르면 None이다."""
    if selector.scope == SYS:
        names = sys_variables(kind)
        return selector.name in names, names.get(selector.name)
    if selector.scope == CONV:
        if kind != "chatflow":
            return False, None
        if conversation is None:
            return True, None
        return selector.name in conversation, conversation.get(selector.name)
    if selector.scope not in analysis.nodes:
        return False, None
    outputs = analysis.outputs.get(selector.scope)
    if outputs is None:
        return True, None
    return selector.name in outputs, outputs.get(selector.name)


class WorkflowSingleRunService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        bus: EventBus,
        supervisor: Supervisor,
        storage: StorageBackend,
        registry: NodeRegistry | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._bus = bus
        # 워크플로우 레인.
        self._supervisor = supervisor
        self._storage = storage
        self._registry = registry
        self._clock = clock

    async def start(
        self,
        workspace_id: uuid.UUID,
        workflow_id: uuid.UUID,
        node_id: str,
        body: WfNodeRunCreate,
        *,
        subscribe: bool,
    ) -> StartedWfRun:
        if self._supervisor.shutting_down or self._bus.closed:
            raise ServiceError(503, "shutting_down", "서버가 종료 중이라 새 런을 받지 않는다")
        async with self._db.read() as session:
            workflow = await WorkflowRepository(session, workspace_id).get_active(workflow_id)
            if workflow is None:
                raise not_found("워크플로우")
            draft = await WfVersionRepository(session, workspace_id).get_draft(workflow.id)
            if draft is None:
                raise not_found("워크플로우 초안")
            agents = await live_agent_ids(session, workspace_id, draft.graph)
        graph = parse_graph(draft.graph)
        if graph is None:
            raise ServiceError(422, "draft_invalid", "저장된 그래프를 읽지 못했다")
        node = next((item for item in graph.nodes if item.id == node_id), None)
        if node is None:
            raise not_found("노드")
        if node.type in NOT_RUNNABLE:
            raise ServiceError(422, "node_not_runnable", f"{node.type} 노드는 혼자 실행할 수 없다")
        kind: WorkflowKindName = (
            "chatflow" if workflow.kind == WorkflowKind.CHATFLOW else "workflow"
        )
        conversation = conversation_types(workflow.kind, conversation_vars(draft.conversation_vars))
        analysis = graph_check.analyze(
            graph,
            kind,
            registry=self._registry,
            env_names=[item.name for item in stored_env(draft.env_vars)],
            conversation_vars=conversation,
            agent_ids=agents,
        )
        inner = analysis.frames.get(node.id)
        scope = {node.id, *(inner.nodes if inner is not None else ())}
        errors = [
            issue for issue in analysis.issues if issue.level == "error" and issue.node_id in scope
        ]
        if errors:
            raise ServiceError(
                422,
                "draft_invalid",
                "이 노드에 검증 오류가 있다. 고쳐 저장한 뒤 다시 실행한다",
                details=issue_details(errors),
            )
        many: dict[str, bool] = {}
        problems: dict[str, str] = {}
        for key in body.inputs:
            try:
                selector = parse_selector(key)
            except SelectorError:
                problems[key] = "<노드 id>.<출력 이름> 꼴이어야 한다"
                continue
            if selector.path:
                problems[key] = "하위 경로 없이 변수 하나를 준다"
            elif selector.scope == ENV:
                problems[key] = "환경 변수는 초안의 값을 쓴다"
            elif selector.scope in scope:
                problems[key] = "실행할 노드 자신의 출력은 줄 수 없다"
            else:
                exists, declared = _declared(analysis, kind, conversation, selector)
                if not exists:
                    problems[key] = "그래프에 없는 변수다"
                elif declared in FILE_TYPES:
                    many[key] = FILE_TYPES[declared]
        if problems:
            raise ServiceError(422, "invalid_inputs", "런 입력이 올바르지 않다", details=problems)
        values = await WorkflowFileService(self._db, self._storage).resolve_files(
            workspace_id, workflow.id, many, body.inputs
        )
        inputs = WfSingleNodeInputs(node_id=node.id, values=values)
        limits = run_limits(self._settings, parse_settings(draft.settings))
        async with self._db.write() as session:
            runs = WfRunRepository(session, workspace_id)
            run = runs.add(
                WfRun(
                    id=new_id(),
                    workflow_id=workflow.id,
                    wf_version_id=draft.id,
                    kind=workflow.kind,
                    trigger=WfRunTrigger.SINGLE_NODE,
                    status=RunStatus.QUEUED,
                    inputs=inputs.model_dump(mode="json"),
                    usage=UsageTotal().model_dump(mode="json"),
                    node_run_count=0,
                    limits=limits,
                    graph_hash=draft.graph_hash,
                    queued_at=self._clock(),
                )
            )
            await runs.flush()
            await bind_run_uploads(session, run, values)
            ref = wf_run_ref(run)
        try:
            subscription = await enqueue(self._bus, self._supervisor, ref, subscribe=subscribe)
        except SupervisorClosed:
            await fail_unstarted(self._db, self._bus, ref, self._clock())
            raise ServiceError(
                503, "shutting_down", "서버가 종료 중이라 새 런을 받지 않는다"
            ) from None
        log.info(
            "wf_run.single_node_created",
            run_id=str(ref.run_id),
            workflow_id=str(workflow_id),
            node_id=node.id,
            values=len(values),
        )
        return StartedWfRun(run_id=ref.run_id, subscription=subscription)
