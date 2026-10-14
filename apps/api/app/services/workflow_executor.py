"""워크플로우 런 실행기. 워크플로우 레인 Supervisor 워커가 부르는 실행 함수다(2단계 설계 3.6, 3.7).

wf_run과 버전을 읽어 엔진(app/workflow/engine.py)의 RunContext를 만들고, 엔진이 내는 이벤트를 받아
노드 런 행(wf_node_run)과 이벤트 버스에 남긴다. 엔진은 이벤트 하나를 내고 소비가 끝날 때까지
기다리므로 node.started·node.completed는 행을 먼저 쓰고 버스에 낸다. 런의 끝을 알리는
workflow.error·workflow.waiting_input·workflow.completed는 모아 두었다가 wf_run 행을 고친 뒤에
낸다. 완료 이벤트를 받은 화면이 런을 다시 읽어도 최종 상태가 보이게 하려는 1단계 규칙이다.

서버 종료로 멈추면 엔진이 체크포인트 없이 failed(shutdown)로 끝낸다. 유예가 지나 워커가 취소되면
여기서 상태만 빨리 정리한다. 실패·취소로 끝난 런의 자식 런(agent 노드)은 남기지 않고 중지한다.

노드가 쓰는 포트도 여기서 만든다. 파일 포트는 런 작업 폴더의 파일을 storage의 wf-files로 옮겨
wf_file(kind=output) 행을 남기고, 노드 설정에는 WF_CODE_TIMEOUT_SEC와 워크스페이스 가격표가
들어간다. 과업 포트는 agent 노드의 자식 런을 ChildRunService.start_node_child로 만들고(버전 규칙·
승인 끄기, 오케스트레이션 9장) 자식 id를 그 노드 런에 바로 적는다.

단일 노드 실행 런(trigger=single_node)은 inputs가 {node_id, values}이고 엔진을 그 노드 하나로
돌린다.
"""

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.task_service import AgentTaskResult, AgentTaskService, AgentTaskSpec
from app.config import Settings
from app.db.engine import Database
from app.db.models import WfFile, WfNodeRun
from app.db.models.enums import RunStatus, SessionFileKind, WfFileKind, WfRunTrigger, WorkflowKind
from app.db.repositories.runs import RunRepository
from app.db.repositories.session_files import SessionFileRepository
from app.db.repositories.workflows import (
    WfFileRepository,
    WfNodeRunRepository,
    WfRunRepository,
    WfVersionRepository,
    WorkflowRepository,
)
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.llm.base import ModelAdapter
from app.runner.supervisor import CancelSignal
from app.sandbox.base import SandboxBackend, SandboxError
from app.schemas.events import UsageTotal
from app.schemas.wf_run import WfSingleNodeInputs
from app.schemas.workflow import Graph, VarType
from app.schemas.workflow import WorkflowKind as WorkflowKindName
from app.schemas.workspace import WorkspaceSettings
from app.security.crypto import CryptoError, SecretCipher, cipher_from_settings
from app.services.child_runs import ChildRunService, NodeParent
from app.services.errors import ServiceError
from app.services.files import guess_mime, sanitize_filename
from app.services.runs import parse_run_key, waits_for_child, workspace_handle
from app.services.workflow_files import MAX_WF_FILE_BYTES, file_ref
from app.services.workflow_runs import cancel_children, completed_data, wf_run_ref
from app.services.workflows import (
    conversation_types,
    conversation_vars,
    decrypt_env,
    parse_graph,
    parse_settings,
    stored_env,
)
from app.storage.base import StorageBackend, StorageError, StorageLimitExceeded, wf_file_key
from app.streaming.event_bus import EventBus, RunRef, jsonable
from app.workflow.engine import Engine, EngineLimits, NodeRunRecord, RunContext, RunOutcome, WfEvent
from app.workflow.frames import Checkpoint
from app.workflow.nodes.base import NodeRegistry
from app.workflow.ports import (
    ChildRunView,
    FilePort,
    FilePortError,
    HttpPort,
    NodeSettings,
    TaskPort,
    TaskPortError,
)
from app.workflow.variables import POOL_MAX_BYTES, FileRef, preview

log = structlog.get_logger("app.runner")

# wf_run 행을 고친 뒤에 내는 런의 끝 이벤트(엔진이 마지막에 차례로 낸다).
TAIL_EVENTS = frozenset({"workflow.error", "workflow.waiting_input", "workflow.completed"})
NODE_RUN_MAX_BYTES = 64 * 1024
# iteration 하위 노드 런은 반복마다 생겨 3,000행이 될 수 있어 작게 자른다(3.10).
ITERATION_NODE_RUN_MAX_BYTES = 8 * 1024
WF_WORKSPACES = "wf-workspaces"


def engine_limits(raw: Mapping[str, Any]) -> EngineLimits:
    """wf_run.limits(런을 만들 때 적용한 값)를 엔진 한도로. 빠진 값은 엔진 기본값이다."""
    defaults = EngineLimits()
    cost = raw.get("max_cost_usd")
    return EngineLimits(
        max_run_seconds=float(raw.get("max_run_seconds", defaults.max_run_seconds)),
        max_total_tokens=int(raw.get("max_total_tokens", defaults.max_total_tokens)),
        max_cost_usd=float(cost) if isinstance(cost, int | float) else None,
        max_parallel_nodes=int(raw.get("max_parallel_nodes", defaults.max_parallel_nodes)),
        pool_max_bytes=int(raw.get("pool_max_bytes", POOL_MAX_BYTES)),
        checkpoint_interval_sec=float(
            raw.get("checkpoint_interval_sec", defaults.checkpoint_interval_sec)
        ),
    )


@dataclass
class _Job:
    ref: RunRef
    workflow_id: uuid.UUID
    version_id: uuid.UUID
    kind: WorkflowKindName
    graph: Graph
    env_raw: list[dict[str, Any]]
    conversation_types: dict[str, VarType] | None
    inputs: dict[str, Any]
    limits: EngineLimits
    mask_outputs: bool
    resume: Checkpoint | None
    # workspace.settings.model_prices. 모델 노드가 node.usage의 cost_usd를 계산한다.
    model_prices: dict[str, dict[str, Any]]
    workflow_name: str = ""
    # 초안 버전으로 도는 런. agent 노드가 발행 버전 없는 에이전트의 초안을 쓸 수 있다.
    draft: bool = True
    # 버전 settings.approval_mode=off(9.1).
    approval_off: bool = False
    single_node: str | None = None
    seed: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def run_id(self) -> uuid.UUID:
        return self.ref.run_id

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.ref.workspace_id


class _Failure(Exception):
    """실행 전에 런을 실패로 끝내야 하는 문제(버전·체크포인트·비밀값)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _TasksPort:
    """엔진의 TaskPort. 과업 서비스에 런 정보를 채워 넘기고 서비스 오류를 TaskPortError로 바꾼다.

    agent 노드는 워크스페이스를 모르므로 spec.workspace_id는 이 런의 것으로 바꾼다.
    correlation에 workflow_run_id를 넣고, node_run_id가 있으면 자식을 만들자마자 그 노드 런의
    child_run_id에 적는다. 노드가 자식을 기다리는 도중 서버가 죽어도 기동 복구가 이 열로 고아
    자식을 찾는다(3.7의 5번).
    """

    def __init__(self, executor: "WorkflowRunExecutor", job: _Job, tasks: AgentTaskService) -> None:
        self._executor = executor
        self._job = job
        self._tasks = tasks

    async def start(self, spec: AgentTaskSpec) -> uuid.UUID:
        # 노드가 그사이 취소되어도 자식 만들기와 노드 런 기록은 끝낸다. 기록이 있어야 런이 끝날 때
        # 실행기·기동 복구가 그 자식을 찾아 정리한다.
        return await asyncio.shield(self._start(spec))

    async def _start(self, spec: AgentTaskSpec) -> uuid.UUID:
        job = self._job
        correlation = {**spec.correlation, "workflow_run_id": str(job.run_id)}
        spec = spec.model_copy(
            update={"workspace_id": job.workspace_id, "correlation": correlation}
        )
        children = self._executor.children
        try:
            if children is not None:
                parent = NodeParent(
                    workspace_id=job.workspace_id,
                    wf_run_id=job.run_id,
                    workflow_id=job.workflow_id,
                    label=job.workflow_name,
                    draft=job.draft,
                    approval_off=job.approval_off,
                )
                child_run_id = (await children.start_node_child(parent, spec)).run_id
            else:
                child_run_id = await self._tasks.start(spec)
        except ServiceError as exc:
            raise TaskPortError(exc.code, exc.message) from None
        node_run_id = correlation.get("node_run_id")
        if node_run_id is not None:
            await self._executor.record_child(job, uuid.UUID(str(node_run_id)), child_run_id)
        return child_run_id

    async def wait(self, run_id: uuid.UUID, timeout: float | None = None) -> AgentTaskResult:
        try:
            return await self._tasks.wait(run_id, timeout)
        except ServiceError as exc:
            raise TaskPortError(exc.code, exc.message) from None

    async def cancel(self, run_id: uuid.UUID) -> None:
        try:
            await self._tasks.cancel(run_id)
        except ServiceError as exc:
            raise TaskPortError(exc.code, exc.message) from None

    async def child(self, run_id: uuid.UUID) -> ChildRunView:
        workspace_id = self._job.workspace_id
        async with self._executor.db.read() as session:
            run = await RunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise TaskPortError("not_found", "자식 런을 찾을 수 없다")
            rows = await SessionFileRepository(session, workspace_id).list_for_session(
                run.session_id, kind=SessionFileKind.OUTPUT, run_id=run.id
            )
        children = self._executor.children
        files = [
            FileRef(
                file_id=str(row.id),
                filename=row.filename,
                mime=row.mime,
                size_bytes=row.size_bytes,
                storage_key=row.storage_key,
                source="session_file",
            ).model_dump(mode="json")
            for row in rows
            if row.storage_key is not None
        ]
        return ChildRunView(
            run_id=run.id,
            session_id=run.session_id,
            status=RunStatus.RUNNING if waits_for_child(run) else run.status,
            steps=run.steps,
            usage=UsageTotal.model_validate(run.usage or {}),
            position=(
                children.queue_position(workspace_id, run.id) if children is not None else None
            ),
            files=files,
        )


class _FilesPort:
    """FilePort 구현. 노드가 만든 파일을 storage의 wf-files/<workspace>/<file_id>로 옮긴다."""

    def __init__(self, executor: "WorkflowRunExecutor", job: _Job, storage: StorageBackend) -> None:
        self._executor = executor
        self._job = job
        self._storage = storage

    async def save_output(self, source: Path, *, filename: str, mime: str | None) -> dict[str, Any]:
        job = self._job
        name = sanitize_filename(filename)
        file_id = new_id()
        key = wf_file_key(job.workspace_id, file_id)
        # 큰 파일을 옮기는 동안 DB 쓰기 잠금을 잡지 않도록 저장소에 먼저 쓴다.
        try:
            stored = await self._storage.write_file(key, source, max_bytes=MAX_WF_FILE_BYTES)
        except StorageLimitExceeded:
            raise FilePortError(
                "file_too_large", f"파일은 {MAX_WF_FILE_BYTES // 2**20}MB 이하여야 한다"
            ) from None
        except (OSError, StorageError) as exc:
            raise FilePortError("file_save_failed", f"파일을 저장하지 못했다: {exc}") from None
        try:
            async with self._executor.db.write() as session:
                files = WfFileRepository(session, job.workspace_id)
                row = files.add(
                    WfFile(
                        id=file_id,
                        kind=WfFileKind.OUTPUT,
                        workflow_id=job.workflow_id,
                        wf_run_id=job.run_id,
                        filename=name,
                        mime=guess_mime(name, mime),
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        storage_key=key,
                        created_at=self._executor.now(),
                    )
                )
                await files.flush()
                return file_ref(row)
        except BaseException:
            await self._storage.delete(key)
            raise

    async def read_bytes(self, ref: Mapping[str, Any], *, max_bytes: int) -> bytes:
        try:
            file = FileRef.model_validate(dict(ref))
            file_id = uuid.UUID(file.file_id)
        except (ValidationError, ValueError):
            raise FilePortError("file_invalid", "파일 참조가 아니다") from None
        # 참조의 저장소 키는 노드 출력에서 온 값이라 믿지 않고 이 워크스페이스의 행에서 다시 읽는다.
        key: str | None = None
        size = 0
        async with self._executor.db.read() as session:
            if file.source == "wf_file":
                row = await WfFileRepository(session, self._job.workspace_id).get(file_id)
                if row is not None:
                    key, size = row.storage_key, row.size_bytes
            else:
                found = await SessionFileRepository(session, self._job.workspace_id).get(file_id)
                if found is not None:
                    key, size = found.storage_key, found.size_bytes
        if key is None:
            raise FilePortError("file_not_found", f"파일을 찾지 못했다: {file.filename}")
        too_large = f"{file.filename}이(가) {max_bytes // 2**20}MB를 넘는다"
        if size > max_bytes:
            raise FilePortError("file_too_large", too_large)
        data = bytearray()
        try:
            async for chunk in self._storage.read_stream(key):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise FilePortError("file_too_large", too_large)
        except (OSError, StorageError) as exc:
            raise FilePortError("file_read_failed", f"파일을 읽지 못했다: {exc}") from None
        return bytes(data)


class _Ports:
    """EnginePorts 구현."""

    def __init__(self, executor: "WorkflowRunExecutor", job: _Job) -> None:
        self._executor = executor
        self._job = job
        tasks = executor.tasks
        self._tasks = _TasksPort(executor, job, tasks) if tasks is not None else None
        storage = executor.storage
        self._files = _FilesPort(executor, job, storage) if storage is not None else None
        self._settings = NodeSettings(
            code_timeout_sec=executor.settings.wf_code_timeout_sec,
            model_prices=job.model_prices,
        )

    @property
    def model(self) -> ModelAdapter | None:
        return self._executor.llm

    @property
    def sandbox(self) -> SandboxBackend | None:
        return self._executor.sandbox

    @property
    def http(self) -> HttpPort | None:
        return self._executor.http

    @property
    def files(self) -> FilePort | None:
        return self._files

    @property
    def tasks(self) -> TaskPort | None:
        return self._tasks

    @property
    def node_settings(self) -> NodeSettings:
        return self._settings

    def now(self) -> datetime:
        return self._executor.now()

    def monotonic(self) -> float:
        # Windows의 time.monotonic은 15ms 단위라 짧은 노드의 소요 시간이 0이 된다.
        return time.perf_counter()

    async def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        await self._executor.save_checkpoint(self._job, checkpoint)


class WorkflowRunExecutor:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        bus: EventBus,
        llm: ModelAdapter | None = None,
        sandbox: SandboxBackend | None = None,
        http: HttpPort | None = None,
        tasks: AgentTaskService | None = None,
        children: ChildRunService | None = None,
        storage: StorageBackend | None = None,
        registry: NodeRegistry | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.settings = settings
        self.bus = bus
        self.llm = llm
        # DATA_DIR/wf-workspaces의 두 번째 로컬 백엔드(9.1).
        self.sandbox = sandbox
        self.http = http
        self.tasks = tasks
        # agent 노드의 자식을 만든다(버전 규칙·승인 끄기·자식 세션 제목). 없으면 tasks.start로
        # 바로 만든다(시험).
        self.children = children
        # None이면 노드가 파일을 남기지 못한다(http_request의 큰 응답은 경고만 남긴다).
        self.storage = storage
        # None이면 런마다 기본 레지스트리를 쓴다.
        self.registry = registry
        self.now = clock
        self._cipher_cache: SecretCipher | None = None

    def _cipher(self) -> SecretCipher:
        if self._cipher_cache is None:
            self._cipher_cache = cipher_from_settings(self.settings)
        return self._cipher_cache

    async def __call__(self, key: str, cancel: CancelSignal) -> None:
        try:
            workspace_id, run_id = parse_run_key(key)
        except ValueError:
            log.error("runner.bad_run_key", lane="workflow", key=key)
            return
        try:
            job = await self._claim(workspace_id, run_id)
        except _Failure as exc:
            await self._fail_before_start(workspace_id, run_id, exc.code, exc.message)
            return
        if job is None:
            return
        try:
            await self._execute(job, cancel)
        except asyncio.CancelledError:
            # 종료 유예가 지나 워커가 강제로 취소됐다. 런 상태만 빨리 정리한다.
            await self._abort(job, "shutdown", "서버가 종료되어 런을 멈췄다", retryable=True)
            raise
        except Exception as exc:
            log.exception("workflow.run_crashed", run_id=str(run_id))
            message = f"런 실행 중 오류가 났다: {type(exc).__name__}: {exc}"[:500]
            await self._abort(job, "internal_error", message)

    async def _claim(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> _Job | None:
        now = self.now()
        async with self.db.write() as session:
            run = await WfRunRepository(session, workspace_id).get(run_id)
            if run is None or run.status != RunStatus.QUEUED:
                # 큐에 있는 동안 중지됐거나 이미 다른 워커가 잡았다.
                return None
            version = await WfVersionRepository(session, workspace_id).get(run.wf_version_id)
            if version is None:
                raise _Failure("version_missing", "런의 워크플로우 버전이 없다")
            graph = parse_graph(version.graph)
            if graph is None:
                raise _Failure("graph_invalid", "저장된 그래프를 읽지 못했다")
            resume: Checkpoint | None = None
            if run.resume_payload is not None:
                if version.graph_hash != run.graph_hash:
                    raise _Failure(
                        "graph_changed", "대기하는 동안 초안 그래프가 바뀌어 이어서 실행할 수 없다"
                    )
                if run.state is None:
                    raise _Failure("checkpoint_missing", "이어서 실행할 체크포인트가 없다")
                try:
                    resume = Checkpoint.model_validate(run.state)
                except ValidationError:
                    raise _Failure(
                        "checkpoint_invalid", "체크포인트를 읽지 못해 이어서 실행할 수 없다"
                    ) from None
            settings = parse_settings(version.settings)
            prices = await self._model_prices(session, workspace_id)
            workflow = await WorkflowRepository(session, workspace_id).get(run.workflow_id)
            kind: WorkflowKindName = "chatflow" if run.kind == WorkflowKind.CHATFLOW else "workflow"
            inputs = dict(run.inputs or {})
            single: WfSingleNodeInputs | None = None
            if run.trigger == WfRunTrigger.SINGLE_NODE:
                try:
                    single = WfSingleNodeInputs.model_validate(inputs)
                except ValidationError:
                    raise _Failure("invalid_inputs", "단일 노드 실행 입력을 읽지 못했다") from None
            job = _Job(
                ref=wf_run_ref(run),
                workflow_id=run.workflow_id,
                version_id=version.id,
                kind=kind,
                graph=graph,
                env_raw=list(version.env_vars or []),
                conversation_types=conversation_types(
                    run.kind, conversation_vars(version.conversation_vars)
                ),
                inputs={} if single is not None else inputs,
                limits=engine_limits(run.limits or {}),
                mask_outputs=settings.mask_secrets_in_outputs,
                resume=resume,
                model_prices=prices,
                workflow_name=workflow.name if workflow is not None else "",
                draft=version.version_number is None,
                approval_off=settings.approval_mode == "off",
                single_node=single.node_id if single is not None else None,
                seed=dict(single.values) if single is not None else {},
            )
            run.status = RunStatus.RUNNING
            run.started_at = run.started_at or now
            run.pending_interaction = None
            run.resume_payload = None
            return job

    async def _model_prices(
        self, session: AsyncSession, workspace_id: uuid.UUID
    ) -> dict[str, dict[str, Any]]:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            return {}
        try:
            settings = WorkspaceSettings.model_validate(workspace.settings or {})
        except ValidationError:
            log.warning("workspace.settings_invalid", workspace_id=str(workspace_id))
            return {}
        return {model: price.model_dump() for model, price in settings.model_prices.items()}

    async def _execute(self, job: _Job, cancel: CancelSignal) -> None:
        try:
            env, secrets = decrypt_env(stored_env(job.env_raw), self._cipher)
        except CryptoError as exc:
            await self._abort(job, "env_unreadable", f"비밀 환경변수를 풀지 못했다: {exc}")
            return
        context = RunContext(
            run_id=job.run_id,
            graph=job.graph,
            kind=job.kind,
            ports=_Ports(self, job),
            cancel=cancel,
            inputs=job.inputs,
            workflow_id=job.workflow_id,
            version_id=job.version_id,
            env=env,
            secret_env=secrets,
            conversation_types=job.conversation_types,
            limits=job.limits,
            resume=job.resume,
            mask_outputs=job.mask_outputs,
            single_node=job.single_node,
            seed=job.seed,
        )
        tail: list[WfEvent] = []
        run = Engine(self.registry).run(context)
        async with run:
            async for event in run:
                if event.type in TAIL_EVENTS:
                    tail.append(event)
                    continue
                if event.node_run is not None:
                    await self._save_node_run(job, event.node_run)
                await self.bus.publish(job.ref, event.type, event.data)
        await self._finish(job, run.outcome, tail)

    async def _save_node_run(self, job: _Job, record: NodeRunRecord) -> None:
        limit = ITERATION_NODE_RUN_MAX_BYTES if record.iteration_node_id else NODE_RUN_MAX_BYTES
        inputs, inputs_cut = preview(jsonable(record.inputs), limit)
        outputs, outputs_cut = preview(jsonable(record.outputs), limit)
        values: dict[str, Any] = {
            "node_id": record.node_id,
            "node_type": record.node_type,
            "title": record.title,
            "index": record.index,
            "iteration_node_id": record.iteration_node_id,
            "iteration_index": record.iteration_index,
            "attempt": record.attempt,
            "status": record.status,
            "inputs": inputs,
            "outputs": outputs,
            "truncated": inputs_cut or outputs_cut,
            "process": jsonable(record.process),
            "error": record.error.model_dump(mode="json") if record.error else None,
            "usage": record.usage.model_dump(mode="json") if record.usage else None,
            "warnings": list(record.warnings),
            "branch": record.branch,
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "duration_ms": record.duration_ms,
        }
        async with self.db.write() as session:
            rows = WfNodeRunRepository(session, job.workspace_id)
            row = await rows.get(record.node_run_id)
            if row is None:
                rows.add(
                    WfNodeRun(
                        id=record.node_run_id,
                        wf_run_id=job.run_id,
                        child_run_id=record.child_run_id,
                        **values,
                    )
                )
                return
            for name, value in values.items():
                setattr(row, name, value)
            # 자식은 노드가 만들자마자 포트가 적어 둔다. 엔진 기록에 아직 없으면 그 값을 둔다.
            if record.child_run_id is not None:
                row.child_run_id = record.child_run_id

    async def record_child(
        self, job: _Job, node_run_id: uuid.UUID, child_run_id: uuid.UUID
    ) -> None:
        async with self.db.write() as session:
            row = await WfNodeRunRepository(session, job.workspace_id).get_for_run(
                job.run_id, node_run_id
            )
            if row is not None:
                row.child_run_id = child_run_id

    async def save_checkpoint(self, job: _Job, checkpoint: Checkpoint) -> None:
        state = checkpoint.model_dump(mode="json")
        async with self.db.write() as session:
            run = await WfRunRepository(session, job.workspace_id).get(job.run_id)
            if run is not None:
                run.state = state

    async def _finish(self, job: _Job, outcome: RunOutcome, tail: list[WfEvent]) -> None:
        status = outcome.status
        now = self.now()
        async with self.db.write() as session:
            run = await WfRunRepository(session, job.workspace_id).get(job.run_id)
            if run is not None:
                run.status = status
                run.usage = outcome.usage.model_dump(mode="json")
                run.node_run_count = outcome.node_runs
                run.outputs = jsonable(outcome.outputs) if status == "succeeded" else None
                run.error = outcome.error.model_dump(mode="json") if outcome.error else None
                run.resume_payload = None
                if status == "waiting_input" and outcome.pending is not None:
                    run.pending_interaction = outcome.pending.model_dump(mode="json")
                    run.ended_at = None
                else:
                    run.pending_interaction = None
                    run.ended_at = now
                # 성공·취소 런의 체크포인트는 쓸 일이 없다. 실패·대기 런은 남긴다(4.4).
                if status in ("succeeded", "cancelled"):
                    run.state = None
        if status in ("failed", "cancelled"):
            await cancel_children(self.db, self.tasks, job.workspace_id, job.run_id)
        if status in ("succeeded", "cancelled"):
            await self._release_workspace(job)
        for event in tail:
            data = event.data
            if event.type == "workflow.completed":
                outputs = data.get("outputs")
                if isinstance(outputs, dict):
                    shown, truncated = preview(jsonable(outputs))
                    data = {**data, "outputs": shown, "truncated": truncated}
            await self.bus.publish(job.ref, event.type, data)
        self.bus.end(job.run_id)
        log.info(
            "workflow.run_finished",
            run_id=str(job.run_id),
            status=status,
            node_runs=outcome.node_runs,
            code=outcome.error.code if outcome.error is not None else None,
            duration_ms=outcome.duration_ms,
        )

    async def _release_workspace(self, job: _Job) -> None:
        """런 작업 폴더(DATA_DIR/wf-workspaces/<run_id>)를 지운다.

        실패 런의 폴더는 로그를 볼 수 있게 남기고 하우스키핑이 1시간 뒤에 지운다.
        """
        if self.sandbox is None:
            return
        try:
            handle = workspace_handle(self.sandbox, job.run_id, None)
        except Exception:
            return
        if not await asyncio.to_thread(Path(handle.root).exists):
            return
        try:
            await self.sandbox.release(handle, delete=True)
        except SandboxError as exc:
            log.warning(
                "workflow.workspace_release_failed", run_id=str(job.run_id), error=exc.message
            )

    async def _fail_before_start(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, code: str, message: str
    ) -> None:
        error = {"code": code, "message": message, "retryable": False}
        async with self.db.write() as session:
            run = await WfRunRepository(session, workspace_id).get(run_id)
            if run is None or run.status != RunStatus.QUEUED:
                return
            run.status = RunStatus.FAILED
            run.error = error
            run.pending_interaction = None
            run.resume_payload = None
            run.ended_at = self.now()
            ref = wf_run_ref(run)
            data = completed_data(run, status=RunStatus.FAILED)
        await cancel_children(self.db, self.tasks, workspace_id, run_id)
        await self.bus.publish(ref, "workflow.error", error)
        await self.bus.publish(ref, "workflow.completed", data)
        self.bus.end(run_id)
        log.warning("workflow.run_failed_before_start", run_id=str(run_id), code=code)

    async def _abort(self, job: _Job, code: str, message: str, *, retryable: bool = False) -> None:
        """엔진 밖에서 끝났을 때(예외, 강제 취소, 비밀값 오류). 상태만 남긴다."""
        error = {"code": code, "message": message, "retryable": retryable}
        try:
            async with self.db.write() as session:
                run = await WfRunRepository(session, job.workspace_id).get(job.run_id)
                if run is None or run.status != RunStatus.RUNNING:
                    return
                run.status = RunStatus.FAILED
                run.error = error
                run.pending_interaction = None
                run.ended_at = self.now()
                await WfNodeRunRepository(session, job.workspace_id).close_running(
                    job.run_id, self.now()
                )
                data = completed_data(run, status=RunStatus.FAILED)
            # 종료 유예가 끝난 뒤에는 시간이 없다. 자식 런은 에이전트 레인의 종료가 정리한다.
            if code != "shutdown":
                await cancel_children(self.db, self.tasks, job.workspace_id, job.run_id)
            await self.bus.publish(job.ref, "workflow.error", error)
            await self.bus.publish(job.ref, "workflow.completed", data)
            self.bus.end(job.run_id)
        except Exception:
            log.exception("workflow.abort_failed", run_id=str(job.run_id))
