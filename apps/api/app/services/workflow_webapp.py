"""웹앱의 워크플로우(2단계 설계 6.2, 7.4).

발행되어 webapp_enabled인 워크플로우만 다루고, 런은 지금의 발행 버전으로 trigger=webapp이다. 런의
조회·이벤트·중지·응답은 콘솔과 같은 WorkflowRunService가 하고 여기서는 그 런이 웹앱 범위인지만 본다.
범위 밖의 워크플로우·런·파일은 없는 것(404)으로 본다.

start_version_run은 발행 버전으로 런을 만드는 공통 부분이라 서비스 API(trigger=api)도 그대로 쓸 수
있다.
"""

import uuid
from collections.abc import AsyncIterable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import structlog
from pydantic import ValidationError

from app.config import Settings
from app.db.engine import Database
from app.db.models import WfRun, WfVersion, Workflow
from app.db.models.enums import RunStatus, WfRunTrigger, WorkflowKind
from app.db.repositories.workflows import (
    WfRunRepository,
    WfVersionRepository,
    WorkflowRepository,
)
from app.db.types import new_id, utcnow
from app.runner.supervisor import Supervisor, SupervisorClosed
from app.schemas.events import UsageTotal
from app.schemas.webapp import FlowAppOut, FlowField, FlowRunResult
from app.schemas.wf_run import WfRunList, WfRunOut
from app.schemas.workflow import Graph, WfFileOut
from app.services.errors import ServiceError, not_found
from app.services.workflow_files import FileDownload, WorkflowFileService, bind_run_uploads
from app.services.workflow_runs import (
    StartedWfRun,
    enqueue,
    fail_unstarted,
    run_limits,
    wf_run_out,
    wf_run_ref,
)
from app.services.workflows import (
    conversation_types,
    conversation_vars,
    issue_details,
    parse_graph,
    parse_settings,
    stored_env,
)
from app.storage.base import StorageBackend
from app.streaming.event_bus import EventBus
from app.workflow import graph as graph_check
from app.workflow.nodes.base import NodeRegistry
from app.workflow.nodes.start import InputError, StartConfig, StartField, coerce_inputs

log = structlog.get_logger("app.services.workflow_webapp")

MAX_RUNS_PAGE = 100


def app_not_found() -> ServiceError:
    return ServiceError(404, "app_not_found", "웹앱으로 공개한 워크플로우가 아니다")


@dataclass(frozen=True)
class PublishedFlow:
    workflow: Workflow
    version: WfVersion


def start_fields(graph: Graph | None) -> list[StartField]:
    """루트 start 노드의 입력 필드. 그래프나 설정을 읽지 못하면 빈 목록이다."""
    if graph is None:
        return []
    start = next(
        (node for node in graph.nodes if node.type == "start" and node.parent_id is None), None
    )
    if start is None:
        return []
    try:
        return StartConfig.model_validate(start.config).fields
    except ValidationError:
        return []


async def published_flow(
    db: Database, workspace_id: uuid.UUID, workflow_id: uuid.UUID
) -> PublishedFlow:
    """살아 있는 워크플로우와 지금의 발행 버전.

    없으면 404, 발행 전이면 409 workflow_not_published다.
    """
    version: WfVersion | None = None
    async with db.read() as session:
        workflow = await WorkflowRepository(session, workspace_id).get_active(workflow_id)
        if workflow is None:
            raise not_found("워크플로우")
        if workflow.published_version_id is not None:
            version = await WfVersionRepository(session, workspace_id).get(
                workflow.published_version_id
            )
    if version is None:
        raise ServiceError(
            409, "workflow_not_published", "발행한 버전이 없다. 워크플로우를 먼저 발행한다"
        )
    return PublishedFlow(workflow=workflow, version=version)


def flow_result(run: WfRunOut) -> FlowRunResult:
    return FlowRunResult(
        run_id=run.id,
        status=run.status,
        outputs=run.outputs,
        usage=run.usage,
        error=run.error,
        pending_interaction=run.pending_interaction,
    )


async def start_version_run(
    db: Database,
    settings: Settings,
    *,
    bus: EventBus,
    supervisor: Supervisor,
    storage: StorageBackend,
    workspace_id: uuid.UUID,
    workflow: Workflow,
    version: WfVersion,
    inputs: Mapping[str, Any],
    trigger: WfRunTrigger,
    subscribe: bool,
    registry: NodeRegistry | None = None,
    api_key_id: uuid.UUID | None = None,
    external_user: str | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> StartedWfRun:
    """버전 하나로 워크플로우 런을 만들어 워크플로우 레인에 넣는다.

    미리보기 런(WorkflowRunService.start_draft_run)과 같은 검사를 하되 버전과 trigger를 받는다. 발행
    버전은 발행할 때 검증을 통과했지만 노드 레지스트리가 바뀌었을 수 있어 다시 본다.
    """
    if supervisor.shutting_down or bus.closed:
        raise ServiceError(503, "shutting_down", "서버가 종료 중이라 새 런을 받지 않는다")
    if workflow.kind != WorkflowKind.WORKFLOW:
        raise ServiceError(409, "wrong_kind", "챗플로우는 이 경로가 아니라 대화로 실행한다")
    graph = parse_graph(version.graph)
    if graph is None:
        raise ServiceError(422, "version_invalid", "발행 버전의 그래프를 읽지 못했다")
    analysis = graph_check.analyze(
        graph,
        "workflow",
        registry=registry,
        env_names=[item.name for item in stored_env(version.env_vars)],
        conversation_vars=conversation_types(
            workflow.kind, conversation_vars(version.conversation_vars)
        ),
    )
    if not analysis.ok:
        raise ServiceError(
            422,
            "version_invalid",
            "발행 버전에 검증 오류가 있다. 콘솔에서 고쳐 다시 발행한다",
            details=issue_details(analysis.issues),
        )
    start_id = next(
        node.id for node in graph.nodes if node.type == "start" and node.parent_id is None
    )
    start = cast(StartConfig, analysis.configs[start_id])
    resolved = await WorkflowFileService(db, storage, clock=clock).resolve_inputs(
        workspace_id, workflow.id, start.fields, inputs
    )
    try:
        coerce_inputs(start.fields, resolved)
    except InputError as exc:
        raise ServiceError(
            422, "invalid_inputs", "런 입력이 올바르지 않다", details=exc.errors
        ) from None
    limits = run_limits(settings, parse_settings(version.settings))
    async with db.write() as session:
        runs = WfRunRepository(session, workspace_id)
        run = runs.add(
            WfRun(
                id=new_id(),
                workflow_id=workflow.id,
                wf_version_id=version.id,
                kind=workflow.kind,
                trigger=trigger,
                status=RunStatus.QUEUED,
                api_key_id=api_key_id,
                external_user=external_user,
                # 미리보기 런과 같이 재기동 뒤에도 같은 입력으로 시작하도록 가리지 않는다.
                inputs=resolved,
                usage=UsageTotal().model_dump(mode="json"),
                node_run_count=0,
                limits=limits,
                graph_hash=version.graph_hash,
                queued_at=clock(),
            )
        )
        await runs.flush()
        await bind_run_uploads(session, run, resolved)
        ref = wf_run_ref(run)
    try:
        subscription = await enqueue(bus, supervisor, ref, subscribe=subscribe)
    except SupervisorClosed:
        await fail_unstarted(db, bus, ref, clock())
        raise ServiceError(503, "shutting_down", "서버가 종료 중이라 새 런을 받지 않는다") from None
    log.info(
        "wf_run.created",
        run_id=str(ref.run_id),
        workflow_id=str(workflow.id),
        version_id=str(version.id),
        trigger=str(trigger),
    )
    return StartedWfRun(run_id=ref.run_id, subscription=subscription)


class WorkflowWebappService:
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

    async def published(self, workspace_id: uuid.UUID, slug: str) -> PublishedFlow:
        """웹앱으로 공개한 워크플로우와 지금의 발행 버전. 아니면 404 app_not_found."""
        version: WfVersion | None = None
        async with self._db.read() as session:
            workflow = await WorkflowRepository(session, workspace_id).get_active_by_slug(slug)
            if (
                workflow is not None
                and workflow.webapp_enabled
                and workflow.published_version_id is not None
            ):
                version = await WfVersionRepository(session, workspace_id).get(
                    workflow.published_version_id
                )
        if workflow is None or version is None:
            raise app_not_found()
        return PublishedFlow(workflow=workflow, version=version)

    async def get_app(self, workspace_id: uuid.UUID, slug: str) -> FlowAppOut:
        flow = await self.published(workspace_id, slug)
        workflow, version = flow.workflow, flow.version
        fields = start_fields(parse_graph(version.graph))
        return FlowAppOut(
            workflow_id=workflow.id,
            slug=workflow.slug,
            name=workflow.name,
            icon=workflow.icon,
            description=workflow.description,
            kind=workflow.kind,  # pyright: ignore[reportArgumentType]
            version_id=version.id,
            version_number=version.version_number or 0,
            published_at=version.published_at,
            fields=[FlowField.model_validate(field.model_dump()) for field in fields],
            webapp=parse_settings(version.settings).webapp,
        )

    async def start_run(
        self,
        workspace_id: uuid.UUID,
        slug: str,
        inputs: Mapping[str, Any],
        *,
        subscribe: bool,
    ) -> StartedWfRun:
        flow = await self.published(workspace_id, slug)
        return await start_version_run(
            self._db,
            self._settings,
            bus=self._bus,
            supervisor=self._supervisor,
            storage=self._storage,
            workspace_id=workspace_id,
            workflow=flow.workflow,
            version=flow.version,
            inputs=inputs,
            trigger=WfRunTrigger.WEBAPP,
            subscribe=subscribe,
            registry=self._registry,
            clock=self._clock,
        )

    async def list_runs(
        self, workspace_id: uuid.UUID, slug: str, *, limit: int = 20, offset: int = 0
    ) -> WfRunList:
        """이 웹앱에서 실행한 런(최근 것부터). 미리보기·API 런은 섞이지 않는다."""
        flow = await self.published(workspace_id, slug)
        limit = max(1, min(limit, MAX_RUNS_PAGE))
        async with self._db.read() as session:
            rows = await WfRunRepository(session, workspace_id).list_filtered(
                workflow_id=flow.workflow.id,
                trigger=WfRunTrigger.WEBAPP,
                limit=limit + 1,
                offset=offset,
            )
        more = len(rows) > limit
        return WfRunList(
            items=[wf_run_out(row) for row in rows[:limit]],
            next_offset=offset + limit if more else None,
        )

    async def check_run(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> WfRun:
        """웹앱에서 만든 런이고 그 워크플로우가 지금도 웹앱으로 공개돼 있으면 그 런. 아니면 404."""
        workflow: Workflow | None = None
        async with self._db.read() as session:
            run = await WfRunRepository(session, workspace_id).get(run_id)
            if run is not None:
                workflow = await WorkflowRepository(session, workspace_id).get_active(
                    run.workflow_id
                )
        if (
            run is None
            or workflow is None
            or run.trigger != WfRunTrigger.WEBAPP
            or not workflow.webapp_enabled
            or workflow.published_version_id is None
        ):
            raise not_found("워크플로우 런")
        return run

    async def upload(
        self,
        workspace_id: uuid.UUID,
        slug: str,
        *,
        filename: str | None,
        content_type: str | None,
        chunks: AsyncIterable[bytes],
    ) -> WfFileOut:
        flow = await self.published(workspace_id, slug)
        return await WorkflowFileService(self._db, self._storage, clock=self._clock).upload(
            workspace_id,
            flow.workflow.id,
            filename=filename,
            content_type=content_type,
            chunks=chunks,
        )

    async def run_file(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, file_id: uuid.UUID
    ) -> FileDownload:
        """웹앱 런을 거쳐 받는 파일. 규칙은 콘솔과 같다(WorkflowFileService.run_file)."""
        run = await self.check_run(workspace_id, run_id)
        files = WorkflowFileService(self._db, self._storage, clock=self._clock)
        return await files.run_file(workspace_id, run_id, file_id, run=run)
