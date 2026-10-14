"""런 실행기. Supervisor 워커가 부르는 실행 함수다(설계 2.1 러너, 4.1).

DB에서 런·세션·버전을 읽고, 작업공간을 확보하고(WorkspaceProvisioner), 첨부를 복사하고, 히스토리를
만들어 AgentLoop를 DB 포트와 함께 돌린 뒤 결과를 런 행과 이벤트로 남긴다. 루프는 DB를 모른다.

종료 규칙: 런은 항상 run.completed로 끝난다. 실패면 그 바로 앞에 run.error가 온다. 서버 종료로
멈추면 failed(code=shutdown)이고 그때는 파일 수집과 디스크 측정을 건너뛴다(종료 유예 안에 끝내야
한다).

작업공간 행이 없는 세션의 첫 런은 버전의 발행 스냅샷에서 시작한다(3.12). 과업 런(4.10)은 입력을
task/에 두고 submit_result를 붙여 돌리며, 끝나면 run.output을 채우고 ephemeral이면 작업공간을
지운다(결과 파일은 이미 storage에 있다).

하위 에이전트(오케스트레이션): 버전에 sub_agents가 있고 깊이·호출 수 한도 안이면 call_agent를
붙인다. call_agent로 멈춘 런은 run.completed·bus.end 없이 waiting_input(agent_call)으로 저장하고, 그
뒤에야 자식을 큐에 넣는다. 저장하는 트랜잭션에서 중지 요청을 보고 중지면 자식도 함께 닫는다(3.3,
3.6). 자식 런이 끝날 때는 런을 끝내는 같은 트랜잭션에서 기다리던 부모를 queued로 돌리고 트랜잭션
밖에서 넣는다. 재개된 부모는 루프를 돌리기 전에 자식 결과 파일을 작업공간 agents/로 복사한다(4.2).
"""

import asyncio
import base64
import hashlib
import json
import shutil
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.child_runs import (
    ChildCall,
    ChildCorrelation,
    ChildStarted,
    ChildStartError,
    SubAgentChoice,
    can_call_children,
    child_correlation,
)
from app.agent.context import (
    StepMessage,
    StoredMessage,
    build_history,
    decode_step,
    encode_blocks,
    encode_step,
)
from app.agent.file_collector import (
    MAX_FILE_BYTES,
    MAX_SCAN_ENTRIES,
    AlreadyCollected,
    CopiedFile,
    CopyFailed,
    changes_since,
    collect_files,
)
from app.agent.interactions import (
    PendingInteraction,
    ResumePayload,
    effective_approval_mode,
)
from app.agent.loop import (
    AgentLoop,
    FileSummary,
    LoopConfig,
    LoopError,
    LoopOutcome,
    ResumeState,
)
from app.agent.prompt_builder import (
    PromptBuilder,
    SkillPrompt,
    WorkspacePromptInfo,
    prompt_hash,
    task_block,
)
from app.agent.tools import tools_for_groups
from app.agent.tools.base import OutputSink, Tool, ToolDefaults
from app.agent.tools.call_agent import CallAgentTool
from app.agent.tools.submit_result import SUBMIT_RESULT, SubmitResultTool
from app.config import Settings
from app.db.engine import Database
from app.db.models import Message, SessionFile, WorkspaceInstance
from app.db.models.enums import (
    MessageRole,
    RunStatus,
    SessionFileKind,
    WorkspaceInstanceStatus,
)
from app.db.repositories.agents import AgentRepository
from app.db.repositories.messages import MessageRepository
from app.db.repositories.runs import RunRepository
from app.db.repositories.session_files import SessionFileRepository
from app.db.repositories.sessions import SessionRepository
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.llm.base import ModelAdapter
from app.llm.types import CanonicalMessage, ModelParams, TextBlock
from app.runner.supervisor import CancelSignal, Supervisor
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.sandbox.content import StorageProvisionContent
from app.sandbox.paths import resolve_in_workspace
from app.sandbox.process import IS_WINDOWS
from app.sandbox.provisioning import ProvisionEvent, SnapshotSource, WorkspaceProvisioner
from app.sandbox.shell_ps import ShellName, effective_shell
from app.schemas.agent_version import AgentVersionConfig
from app.schemas.common import ApprovalMode, RunLimits
from app.schemas.events import MessageFileData, UsageTotal
from app.schemas.task import TASK_INPUTS_PATH, TaskRunInput
from app.schemas.workspace import WorkspaceSettings
from app.services.agents import AgentService, RuntimeVersion, provision_content, snapshot_source
from app.services.child_runs import (
    ChildRunService,
    ClaimedParent,
    ParentRun,
    claim_parent,
    orphan_runs,
)
from app.services.errors import ServiceError
from app.services.runs import (
    parse_run_key,
    pending_of,
    release_session_workspace,
    task_input_of,
    task_output,
)
from app.storage.base import (
    StorageBackend,
    StorageError,
    safe_tool_call_id,
    session_file_key,
    tool_output_key,
)
from app.streaming.event_bus import EventBus, RunRef

log = structlog.get_logger("app.runner")

MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass
class _Job:
    ref: RunRef
    # ref.session_id와 같다. 2단계에서 RunRef.session_id가 nullable이 되어 에이전트 런은
    # 여기서 한 번 좁혀 둔다.
    session_id: uuid.UUID
    agent_version_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID | None
    model_id: str | None
    limits: RunLimits
    approval_mode: ApprovalMode
    steps: int
    usage: UsageTotal
    started_at: datetime
    session_source: str
    prompt_hash: str | None
    approval_override: str | None
    instance_id: uuid.UUID | None
    instance_created_at: datetime | None
    model_prices: dict[str, dict[str, Any]]
    pending: PendingInteraction | None
    payload: ResumePayload | None
    # 과업 런이면 run.input(4.10).
    task: TaskRunInput | None = None
    # 변경 파일 mtime 기준선(3.13). 처음 시작할 때 작업공간을 준비한 뒤 정하고 재개 때는 이어 쓴다.
    changes_since: datetime | None = None
    # 이 런 앞에서 플랫폼이 작업공간을 마지막으로 만진 시각(앞 런 종료, 작업공간 초기화).
    changes_floor: datetime | None = None
    cancel: CancelSignal | None = None
    agent_id: uuid.UUID | None = None
    external_user: str | None = None
    api_key_id: uuid.UUID | None = None
    # call_agent로 만든 자식 런이면 부모와 correlation(깊이·조상).
    parent_run_id: uuid.UUID | None = None
    # 이번 실행 구간에서 만든 자식. 부모가 대기가 아닌 상태로 끝나면 큐에 넣지 못한 것을
    # 닫는다(3.3).
    children_created: list[uuid.UUID] | None = None
    # call_agent에 붙일 하위 에이전트와 부모 정보. _loop_config가 채운다.
    parent_info: ParentRun | None = None

    @property
    def resumed(self) -> bool:
        return self.pending is not None

    @property
    def ephemeral(self) -> bool:
        return self.task is not None and self.task.session_mode == "ephemeral"

    @property
    def correlation(self) -> ChildCorrelation | None:
        return child_correlation(self.task.correlation) if self.task is not None else None

    @property
    def depth(self) -> int:
        correlation = self.correlation
        return correlation.depth if correlation is not None else 0


class _FileSink:
    """256KB를 넘는 도구 출력을 storage의 tool-outputs/<run_id>/<id>.txt에 흘려 쓴다."""

    def __init__(self, path: Path, key: str) -> None:
        self._path = path
        self._key = key
        self._file: IO[bytes] | None = None

    async def write(self, data: bytes) -> None:
        if self._file is None:
            await asyncio.to_thread(self._path.parent.mkdir, parents=True, exist_ok=True)
            self._file = await asyncio.to_thread(self._path.open, "wb")
        await asyncio.to_thread(self._file.write, data)

    async def close(self) -> str:
        if self._file is not None:
            await asyncio.to_thread(self._file.close)
            self._file = None
        return self._key

    async def abort(self) -> None:
        await self.close()
        await asyncio.to_thread(self._path.unlink, missing_ok=True)


class _DbPorts:
    """AgentLoop의 LoopPorts를 DB·storage·이벤트 버스로 구현한다."""

    def __init__(
        self,
        executor: "RunExecutor",
        job: _Job,
        handle: WorkspaceHandle,
        content: StorageProvisionContent,
        *,
        saved: set[uuid.UUID],
    ) -> None:
        self._x = executor
        self._job = job
        self.handle = handle
        self._content = content
        self._saved = saved
        self._last_assistant = job.assistant_message_id
        self._instance_created_at = job.instance_created_at
        self.collected: FileSummary | None = None
        # 이 런이 이미 storage에 남긴 파일(call_agent 스테이징)의 경로별 sha256. 수집 때 같은 파일을
        # 다시 남기지 않는다(오케스트레이션 4.1의 3번).
        self._stored: dict[str, set[str]] = {}

    @property
    def _ws(self) -> uuid.UUID:
        return self._job.ref.workspace_id

    async def emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        await self._x.publish(self._job.ref, event_type, data)

    async def save_message(self, message: StepMessage) -> None:
        blocks = encode_step(message)
        job = self._job
        async with self._x.db.write() as session:
            messages = MessageRepository(session, self._ws)
            if message.id in self._saved:
                row = await messages.get(message.id)
                if row is None:
                    return
                row.blocks = blocks
                row.text = message.text
                row.stop_reason = message.stop_reason
                row.usage = message.usage
                return
            messages.add(
                Message(
                    id=message.id,
                    session_id=job.session_id,
                    run_id=job.ref.run_id,
                    seq=await messages.next_seq(job.session_id),
                    role=MessageRole.ASSISTANT,
                    blocks=blocks,
                    text=message.text,
                    stop_reason=message.stop_reason,
                    files=[],
                    usage=message.usage,
                )
            )
            await messages.flush()
            self._saved.add(message.id)
            self._last_assistant = message.id
            run = await RunRepository(session, self._ws).get(job.ref.run_id)
            if run is not None:
                run.assistant_message_id = message.id
            chat = await SessionRepository(session, self._ws).get(job.session_id)
            if chat is not None:
                chat.message_count += 1
                chat.last_message_at = self._x.now()

    async def save_summary(self, text: str, usage: Mapping[str, Any] | None) -> uuid.UUID:
        job = self._job
        summary_id = new_id()
        async with self._x.db.write() as session:
            messages = MessageRepository(session, self._ws)
            messages.add(
                Message(
                    id=summary_id,
                    session_id=job.session_id,
                    run_id=job.ref.run_id,
                    seq=await messages.next_seq(job.session_id),
                    role=MessageRole.SYSTEM_SUMMARY,
                    blocks=encode_blocks([TextBlock(text=text)]),
                    text=text,
                    files=[],
                    usage=dict(usage) if usage is not None else None,
                )
            )
            chat = await SessionRepository(session, self._ws).get(job.session_id)
            if chat is not None:
                chat.message_count += 1
                chat.last_message_at = self._x.now()
        return summary_id

    async def save_progress(self, steps: int, usage: UsageTotal) -> None:
        async with self._x.db.write() as session:
            run = await RunRepository(session, self._ws).get(self._job.ref.run_id)
            if run is not None:
                run.steps = steps
                run.usage = usage.model_dump(mode="json")

    async def open_output_sink(self, tool_call_id: str) -> OutputSink:
        key = tool_output_key(self._job.ref.run_id, safe_tool_call_id(tool_call_id))
        return _FileSink(self._x.storage.local_path(key), key)

    async def recreate_workspace(self) -> WorkspaceHandle:
        result = await self._x.provisioner.ensure(
            self._job.session_id,
            instance_created_at=self._instance_created_at,
            content=self._content,
            emit=self._x.provision_emitter(self._job.ref),
        )
        self._instance_created_at = result.created_at
        self.handle = result.handle
        await self._x.record_instance(self._job, result.handle, result.created_at, recreated=True)
        log.warning("workspace.recreated_during_run", run_id=str(self._job.ref.run_id))
        return result.handle

    async def _copy(self, rel_path: str) -> CopiedFile:
        try:
            source = await asyncio.to_thread(resolve_in_workspace, self.handle.root, rel_path)
        except SandboxError as exc:
            raise CopyFailed(exc.message) from exc
        hashes = self._stored.get(rel_path)
        if hashes:
            # 하위 에이전트에 넘기려고 이미 남긴 파일과 내용이 같으면 그 행이 자리를 대신한다.
            try:
                digest = await asyncio.to_thread(_sha256_of, source)
            except OSError as exc:
                raise CopyFailed(str(exc)) from exc
            if digest in hashes:
                raise AlreadyCollected(rel_path)
        key = session_file_key(self._job.ref.run_id, rel_path)
        try:
            stored = await self._x.storage.write_file(key, source, max_bytes=MAX_FILE_BYTES)
        except (StorageError, OSError) as exc:
            raise CopyFailed(str(exc)) from exc
        return CopiedFile(storage_key=key, size=stored.size_bytes, sha256=stored.sha256)

    async def collect_files(self) -> FileSummary:
        if self.collected is not None:
            return self.collected
        job = self._job
        since = job.changes_since or changes_since(job.started_at)
        try:
            changes = await self._x.backend.changes(
                self.handle, since, max_entries=MAX_SCAN_ENTRIES
            )
        except SandboxError as exc:
            log.warning("run.changes_failed", run_id=str(job.ref.run_id), error=exc.message)
            self.collected = FileSummary()
            return self.collected
        async with self._x.db.read() as session:
            self._stored = await SessionFileRepository(session, self._ws).stored_for_run(
                job.ref.run_id
            )
        result = await collect_files(changes, self._copy)
        files: list[MessageFileData] = []
        async with self._x.db.write() as session:
            rows = SessionFileRepository(session, self._ws)
            added: list[SessionFile] = []
            for item in result.files:
                added.append(
                    rows.add(
                        SessionFile(
                            id=new_id(),
                            session_id=job.session_id,
                            run_id=job.ref.run_id,
                            message_id=self._last_assistant,
                            kind=SessionFileKind.OUTPUT,
                            path=item.path,
                            filename=item.filename,
                            mime=item.mime,
                            size_bytes=item.size,
                            sha256=item.sha256 or "",
                            storage_key=item.storage_key,
                            reason=item.reason,
                        )
                    )
                )
            await rows.flush()
            if self._last_assistant is not None and added:
                message = await MessageRepository(session, self._ws).get(self._last_assistant)
                if message is not None:
                    message.files = [*message.files, *(str(row.id) for row in added)]
            for row, item in zip(added, result.files, strict=True):
                files.append(
                    MessageFileData(
                        session_file_id=row.id,
                        path=item.path,
                        size=item.size,
                        downloadable=item.downloadable,
                        filename=item.filename,
                        mime=item.mime,
                    )
                )
        for row, item in zip(added, result.files, strict=True):
            await self.emit(
                "file.changed",
                {
                    "path": item.path,
                    "size": item.size,
                    "session_file_id": row.id,
                    "downloadable": item.downloadable,
                    "reason": item.reason,
                },
            )
        self.collected = FileSummary(files=files, truncated=result.truncated)
        return self.collected

    async def load_image(self, storage_key: str) -> str | None:
        try:
            path = self._x.storage.local_path(storage_key)
            data = await asyncio.to_thread(_read_small, path, MAX_IMAGE_BYTES)
        except (StorageError, OSError) as exc:
            log.warning("run.image_unreadable", key=storage_key, error=str(exc))
            return None
        return base64.b64encode(data).decode("ascii") if data is not None else None

    async def start_child(self, call: ChildCall) -> ChildStarted | ChildStartError:
        parent = self._job.parent_info
        if parent is None:
            return ChildStartError(
                code="agent_not_allowed", message="이 런은 하위 에이전트를 부를 수 없다"
            )
        # 스테이징은 지금 작업공간(폴더를 다시 만들었으면 새 핸들)에서 한다.
        parent = replace(parent, workspace_root=self.handle.root)
        started = await self._x.children().create_child(parent, call)
        if isinstance(started, ChildStarted):
            created = self._job.children_created
            if created is None:
                created = self._job.children_created = []
            created.append(started.child_run_id)
        return started


def _stopped_at_pause(outcome: LoopOutcome) -> LoopOutcome:
    return outcome.model_copy(
        update={"status": "cancelled", "pending": None, "interrupted": "cancel"}
    )


def _read_small(path: Path, limit: int) -> bytes | None:
    with path.open("rb") as file:
        data = file.read(limit + 1)
    return data if len(data) <= limit else None


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class RunExecutor:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        llm: ModelAdapter,
        backend: SandboxBackend,
        storage: StorageBackend,
        bus: EventBus,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.settings = settings
        self.llm = llm
        self.backend = backend
        self.storage = storage
        self.bus = bus
        self.now = clock
        self.provisioner = WorkspaceProvisioner(backend)
        self._agents = AgentService(db, settings)
        self._prompts = PromptBuilder()
        self._shell: ShellName = effective_shell(settings.sandbox_shell, windows=IS_WINDOWS)
        # 이 실행기를 부르는 에이전트 레인. Supervisor가 실행기를 받아 만들어지므로 main.py가 만든
        # 뒤에 넣는다. 하위 에이전트 런·재개하는 부모를 큐에 넣을 때 쓴다. 없으면 queued로 두고
        # 복구가 넣는다.
        self.supervisor: Supervisor | None = None

    def children(self) -> ChildRunService:
        return ChildRunService(
            self.db,
            self.settings,
            bus=self.bus,
            supervisor=self.supervisor,
            storage=self.storage,
            backend=self.backend,
            clock=self.now,
        )

    async def publish(self, ref: RunRef, event_type: str, data: Mapping[str, Any]) -> None:
        await self.bus.publish(ref, event_type, data)

    def provision_emitter(self, ref: RunRef) -> Callable[[ProvisionEvent], Awaitable[None]]:
        async def emit(event: ProvisionEvent) -> None:
            await self.publish(ref, event.type, event.data)

        return emit

    async def __call__(self, key: str, cancel: CancelSignal) -> None:
        try:
            workspace_id, run_id = parse_run_key(key)
        except ValueError:
            log.error("runner.bad_run_key", key=key)
            return
        job = await self._claim(workspace_id, run_id, cancel)
        if job is None:
            return
        try:
            await self._execute(job, cancel)
        except asyncio.CancelledError:
            # 종료 유예가 지나 워커가 강제로 취소됐다. 런 상태만 빨리 정리한다(2.6의 4번).
            await self._abort(job, "shutdown", "서버가 종료되어 런을 멈췄다", retryable=True)
            raise
        except Exception as exc:
            log.exception("runner.run_crashed", run_id=str(run_id))
            message = f"런 실행 중 오류가 났다: {type(exc).__name__}: {exc}"[:500]
            await self._abort(job, "internal_error", message)

    async def _claim(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, cancel: CancelSignal
    ) -> _Job | None:
        now = self.now()
        cancelled: RunRef | None = None
        completed: dict[str, Any] = {}
        claimed: ClaimedParent | None = None
        async with self.db.write() as session:
            run = await RunRepository(session, workspace_id).get(run_id)
            if run is None or run.status != RunStatus.QUEUED:
                # 큐에 있는 동안 중지됐거나 이미 다른 워커가 잡았다.
                return None
            chat = await SessionRepository(session, workspace_id).get(run.session_id)
            ref = RunRef(run_id=run.id, session_id=run.session_id, workspace_id=workspace_id)
            if chat is None or chat.deleted_at is not None or cancel.is_set():
                run.status = RunStatus.CANCELLED
                run.ended_at = now
                cancelled = ref
                completed = {
                    "status": RunStatus.CANCELLED,
                    "usage_total": UsageTotal.model_validate(run.usage or {}).model_dump(),
                    "steps": run.steps,
                }
                claimed = await claim_parent(session, workspace_id, run, now)
            else:
                pending = pending_of(run) if run.resume_payload else None
                payload: ResumePayload | None = None
                if pending is not None:
                    try:
                        payload = ResumePayload.model_validate(run.resume_payload)
                    except ValidationError:
                        pending = None
                instance = await WorkspaceInstanceRepository(session, workspace_id).get_for_session(
                    chat.id
                )
                workspace = await WorkspaceRepository(session).get(workspace_id)
                prices: dict[str, dict[str, Any]] = {}
                if workspace is not None:
                    try:
                        ws_settings = WorkspaceSettings.model_validate(workspace.settings or {})
                        prices = {
                            model: price.model_dump()
                            for model, price in ws_settings.model_prices.items()
                        }
                    except ValidationError:
                        log.warning("workspace.settings_invalid", workspace_id=str(workspace_id))
                resumed = pending if payload is not None else None
                run.status = RunStatus.RUNNING
                run.started_at = run.started_at or now
                run.pending_interaction = None
                run.resume_payload = None
                return _Job(
                    ref=ref,
                    session_id=run.session_id,
                    agent_version_id=run.agent_version_id,
                    user_message_id=run.user_message_id,
                    assistant_message_id=run.assistant_message_id,
                    model_id=(run.model or {}).get("model_id"),
                    limits=RunLimits.model_validate(run.limits or {}),
                    approval_mode=run.approval_mode,  # pyright: ignore[reportArgumentType]
                    steps=run.steps,
                    usage=UsageTotal.model_validate(run.usage or {}),
                    started_at=run.started_at,
                    session_source=chat.source,
                    prompt_hash=chat.prompt_hash,
                    approval_override=chat.approval_override,
                    instance_id=instance.id if instance is not None else None,
                    instance_created_at=instance.created_at if instance is not None else None,
                    model_prices=prices,
                    pending=resumed,
                    payload=payload,
                    task=task_input_of(run),
                    changes_since=resumed.changes_since if resumed is not None else None,
                    changes_floor=(
                        instance.last_activity_at
                        if instance is not None and resumed is None
                        else None
                    ),
                    cancel=cancel,
                    agent_id=chat.agent_id,
                    external_user=chat.external_user,
                    api_key_id=chat.api_key_id,
                    parent_run_id=run.parent_run_id,
                )
        if cancelled is not None:
            await self.publish(cancelled, "run.completed", completed)
            self.bus.end(cancelled.run_id)
            if claimed is not None:
                await self.children().launch_parent(claimed)
        return None

    async def _execute(self, job: _Job, cancel: CancelSignal) -> None:
        ref = job.ref
        if job.pending is not None:
            await self.publish(ref, "run.resumed", {"interaction_id": job.pending.interaction_id})
        mode = effective_approval_mode(job.approval_mode, job.approval_override)
        instance_id = job.instance_id or new_id()
        await self.publish(
            ref,
            "run.started",
            {
                "step_limit": job.limits.max_steps,
                # 지정하지 않았으면 폴백 목록의 첫 모델부터 쓴다. 실제로 쓴 모델은 usage에 있다.
                "model": job.model_id or self._default_model(),
                "workspace_instance_id": instance_id,
                "resumed": job.resumed,
                "approval_mode": mode,
            },
        )
        try:
            runtime = await self._agents.load_runtime_version(
                ref.workspace_id, job.agent_version_id
            )
        except ServiceError as exc:
            await self._finish(job, self._early_failure(job, exc.code, exc.message), None)
            return
        content = provision_content(self.storage, runtime)
        snapshot = await self._snapshot(job, runtime)
        try:
            result = await self.provisioner.ensure(
                job.session_id,
                instance_created_at=job.instance_created_at,
                snapshot=snapshot,
                content=content,
                emit=self.provision_emitter(ref),
            )
        except SandboxError as exc:
            await self._finish(job, self._early_failure(job, exc.code, exc.message), None)
            return
        job.instance_id = await self.record_instance(
            job,
            result.handle,
            result.created_at,
            recreated=result.state != "reused",
            instance_id=instance_id,
            restored_snapshot_id=result.restored_snapshot_id,
        )
        handle = result.handle
        if not job.resumed:
            await self._copy_uploads(job, handle)
            if job.task is not None:
                await self._prepare_task(job, job.task, handle)
            # 복원·첨부 복사로 새로 찍힌 mtime은 이 런의 변경이 아니다. 준비를 마친 뒤부터 센다.
            job.changes_since = changes_since(self.now(), job.changes_floor)
        elif job.pending is not None and job.payload is not None and job.payload.child is not None:
            # 하위 에이전트의 결과 파일을 agents/<slug>/<run_id>/로 받는다. 기준선 뒤에 생긴
            # 파일이라 이 런이 끝날 때 부모 세션의 출력 파일로도 남는다(4.2).
            child = await self.children().copy_results_to_parent(
                ref.workspace_id, handle, job.pending, job.payload.child
            )
            job.payload = job.payload.model_copy(update={"child": child})
        history, open_step = await self._history(job)
        if job.pending is not None and open_step is None:
            await self._finish(
                job,
                self._early_failure(
                    job, "resume_state_invalid", "재개할 스텝 메시지를 찾지 못했다"
                ),
                handle,
            )
            return
        config = await self._loop_config(job, runtime, handle, mode)
        saved = {open_step.id} if open_step is not None else set[uuid.UUID]()
        ports = _DbPorts(self, job, handle, content, saved=saved)
        loop = AgentLoop(
            config=config,
            model=self.llm,
            backend=self.backend,
            handle=handle,
            ports=ports,
            cancel=cancel,
        )
        outcome = await loop.run(history, open_step)
        await self._finish(job, outcome, ports.handle, ports)

    async def _loop_config(
        self,
        job: _Job,
        runtime: RuntimeVersion,
        handle: WorkspaceHandle,
        mode: ApprovalMode,
    ) -> LoopConfig:
        config: AgentVersionConfig = runtime.config
        correlation = job.correlation
        # call_agent로 만든 자식은 사용자에게 직접 묻지 못한다. 확인할 것은 결과 글로 돌려준다(3.5).
        tools: list[Tool] = tools_for_groups(
            config.tools, exclude=("ask_user",) if correlation is not None else ()
        )
        task = job.task
        submit: SubmitResultTool | None = None
        if task is not None and task.output_schema is not None:
            submit = SubmitResultTool(task.output_schema)
            tools.append(submit)
        choices, available = await self._sub_agents(job, runtime, handle)
        if choices:
            tools.append(CallAgentTool(choices))
        system = self._prompts.build(
            instructions=config.instructions,
            skills=[
                SkillPrompt(name=skill.slug, description=skill.description, path=skill.path)
                for skill in runtime.skills
            ],
            workspace=WorkspacePromptInfo(
                root=str(handle.root),
                shell=self._shell,
                platform="Windows 11" if IS_WINDOWS else "Linux",
                approval_mode=job.approval_mode,
            ),
            task_block=(
                task_block(
                    task.output_schema,
                    submit_tool=SUBMIT_RESULT,
                    parent_agent=(
                        correlation.parent_agent_name if correlation is not None else None
                    ),
                )
                if task is not None
                else None
            ),
            sub_agents=choices,
            sub_agents_unavailable=bool(config.sub_agents) and not available,
        )
        digest = prompt_hash(system, [tool.spec() for tool in tools])
        if digest != job.prompt_hash:
            await self._update_prompt_hash(job, digest)
        model_id = job.model_id
        caps = self.llm.capabilities(model_id or self._default_model() or "")
        params = config.model.params
        return LoopConfig(
            run_id=job.ref.run_id,
            session_id=job.session_id,
            system_prompt=system,
            tools=tools,
            params=ModelParams(
                temperature=params.temperature,
                top_p=params.top_p,
                max_output_tokens=params.max_output_tokens,
                reasoning_effort=params.reasoning_effort,
            ),
            limits=job.limits,
            approval_mode=mode,
            model_id=model_id,
            env=runtime.env,
            tool_defaults=ToolDefaults(
                timeout_sec=min(
                    job.limits.tool_timeout_sec, self.settings.sandbox_tool_max_timeout_sec
                ),
                max_timeout_sec=self.settings.sandbox_tool_max_timeout_sec,
            ),
            memory_limit_mb=config.sandbox.memory_limit_mb,
            max_processes=config.sandbox.max_processes,
            context_window=caps.context_window,
            max_output_cap=caps.max_output_tokens,
            model_prices=job.model_prices or None,
            steps_done=job.steps,
            usage=job.usage,
            elapsed_ms=job.pending.elapsed_ms if job.pending is not None else 0,
            resume=(
                ResumeState(pending=job.pending, payload=job.payload)
                if job.pending is not None and job.payload is not None
                else None
            ),
            submit_tool=submit.name if submit is not None else None,
        )

    async def _sub_agents(
        self, job: _Job, runtime: RuntimeVersion, handle: WorkspaceHandle
    ) -> tuple[list[SubAgentChoice], bool]:
        """call_agent에 넣을 하위 에이전트와, 깊이·호출 수 한도 안이라 붙일 수 있는지(3.9)."""
        refs = runtime.config.sub_agents
        if not refs:
            return [], False
        if not can_call_children(
            job.depth, self.settings.orchestration_max_depth, job.limits.max_child_calls
        ):
            return [], False
        workspace_id = job.ref.workspace_id
        choices = await self.children().resolve_choices(workspace_id, refs)
        if not choices:
            return [], True
        async with self.db.read() as session:
            agent = await AgentRepository(session, workspace_id).get(runtime.agent_id)
        correlation = job.correlation
        job.parent_info = ParentRun(
            workspace_id=workspace_id,
            run_id=job.ref.run_id,
            session_id=job.session_id,
            agent_id=runtime.agent_id,
            agent_name=agent.name if agent is not None else "",
            draft=runtime.version_number is None,
            depth=job.depth,
            root_run_id=correlation.root_run_id if correlation is not None else job.ref.run_id,
            ancestors=(
                (*correlation.ancestors, runtime.agent_id)
                if correlation is not None
                else (runtime.agent_id,)
            ),
            max_child_calls=job.limits.max_child_calls,
            approval_override=job.approval_override,
            external_user=job.external_user,
            api_key_id=job.api_key_id,
            workspace_root=handle.root,
        )
        return choices, True

    async def _snapshot(self, job: _Job, runtime: RuntimeVersion) -> SnapshotSource | None:
        # 작업공간 행이 없는 세션의 첫 런만 발행 스냅샷에서 시작한다. 밖에서 지워진 폴더는
        # 스냅샷 없이 다시 만든다(3.3).
        if job.instance_created_at is not None or runtime.snapshot_id is None:
            return None
        try:
            return await snapshot_source(
                self.db, self.storage, job.ref.workspace_id, runtime.snapshot_id
            )
        except ServiceError as exc:
            log.warning("run.snapshot_missing", run_id=str(job.ref.run_id), error=exc.message)
            await self.publish(
                job.ref,
                "workspace.restore_warning",
                {"snapshot_id": str(runtime.snapshot_id), "message": exc.message},
            )
            return None

    async def _prepare_task(self, job: _Job, task: TaskRunInput, handle: WorkspaceHandle) -> None:
        """과업 입력을 작업공간 task/에 둔다(3.4). reuse 세션이면 지난 과업의 입력을 먼저 지운다."""
        # task/는 플랫폼이 채우는 폴더다. 정션이면 rmtree가 따라 들어가지 않고 실패만 한다.
        await asyncio.to_thread(shutil.rmtree, handle.root / "task", True)
        data = json.dumps(task.inputs, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            await self.backend.write_file(handle, TASK_INPUTS_PATH, data)
        except SandboxError as exc:
            log.warning("task.inputs_write_failed", run_id=str(job.ref.run_id), error=exc.message)
        for item in task.files:
            try:
                source = self.storage.local_path(item.storage_key)
                await self.provisioner.copy_file_in(handle, item.path, source)
            except (SandboxError, StorageError, OSError) as exc:
                # 파일 하나를 못 옮겨도 과업은 계속한다. 모델이 파일을 못 찾으면 그렇게 알린다.
                log.warning(
                    "task.file_copy_failed",
                    run_id=str(job.ref.run_id),
                    file_id=str(item.session_file_id or item.wf_file_id),
                    error=str(exc),
                )

    def _default_model(self) -> str | None:
        fallback = self.settings.gemini_model_fallback
        return fallback[0] if fallback else None

    async def _update_prompt_hash(self, job: _Job, digest: str) -> None:
        async with self.db.write() as session:
            chat = await SessionRepository(session, job.ref.workspace_id).get(job.session_id)
            if chat is not None:
                chat.prompt_hash = digest
        if job.prompt_hash is not None:
            # 초안을 고친 뒤 같은 미리보기 세션에서 이어 간다. 히스토리는 그대로 둔다(4.8).
            await self.publish(
                job.ref,
                "context.prompt_changed",
                {"previous_hash": job.prompt_hash, "prompt_hash": digest},
            )
        job.prompt_hash = digest

    async def record_instance(
        self,
        job: _Job,
        handle: WorkspaceHandle,
        created_at: datetime | None,
        *,
        recreated: bool,
        instance_id: uuid.UUID | None = None,
        restored_snapshot_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        now = self.now()
        ws = job.ref.workspace_id
        async with self.db.write() as session:
            instances = WorkspaceInstanceRepository(session, ws)
            row = await instances.get_for_session(job.session_id)
            if row is None:
                row = instances.add(
                    WorkspaceInstance(
                        id=instance_id or new_id(),
                        session_id=job.session_id,
                        backend=handle.backend,
                        root_path=str(handle.root),
                        status=WorkspaceInstanceStatus.BUSY,
                        restored_from_snapshot_id=restored_snapshot_id,
                        # provision이 다음 런에서 "같은 폴더"인지 가리는 값이다(폴더 생성 시각).
                        created_at=created_at or now,
                        last_activity_at=now,
                    )
                )
                await instances.flush()
            else:
                row.status = WorkspaceInstanceStatus.BUSY
                row.root_path = str(handle.root)
                row.last_activity_at = now
                if recreated and created_at is not None:
                    row.created_at = created_at
                if restored_snapshot_id is not None:
                    row.restored_from_snapshot_id = restored_snapshot_id
            chat = await SessionRepository(session, ws).get(job.session_id)
            if chat is not None and chat.workspace_instance_id != row.id:
                chat.workspace_instance_id = row.id
            recorded = row.id
        job.instance_created_at = created_at or job.instance_created_at
        return recorded

    async def _copy_uploads(self, job: _Job, handle: WorkspaceHandle) -> None:
        async with self.db.read() as session:
            uploads = await SessionFileRepository(session, job.ref.workspace_id).list_for_message(
                job.user_message_id
            )
        for row in uploads:
            if row.kind != SessionFileKind.UPLOAD or row.storage_key is None:
                continue
            try:
                source = self.storage.local_path(row.storage_key)
                await self.provisioner.copy_file_in(handle, row.path, source)
            except (SandboxError, StorageError, OSError) as exc:
                # 첨부 하나를 못 옮겨도 런은 계속한다. 모델이 파일을 못 찾으면 그렇게 알린다.
                log.warning(
                    "run.upload_copy_failed",
                    run_id=str(job.ref.run_id),
                    file_id=str(row.id),
                    error=str(exc),
                )

    async def _history(self, job: _Job) -> tuple[list[CanonicalMessage], StepMessage | None]:
        async with self.db.read() as session:
            rows = await MessageRepository(session, job.ref.workspace_id).list_for_history(
                job.session_id
            )
        stored = [
            StoredMessage(
                id=row.id,
                seq=row.seq,
                role=row.role,  # pyright: ignore[reportArgumentType]
                blocks=row.blocks or [],
                text=row.text,
                stop_reason=row.stop_reason,
                usage=row.usage,
            )
            for row in rows
        ]
        if job.pending is None:
            return build_history(stored), None
        index = next((i for i, row in enumerate(stored) if row.id == job.pending.message_id), None)
        if index is None:
            return build_history(stored), None
        if index != len(stored) - 1:
            log.warning("run.resume_rows_after_open_step", run_id=str(job.ref.run_id))
        return build_history(stored[:index]), decode_step(stored[index])

    def _early_failure(self, job: _Job, code: str, message: str) -> LoopOutcome:
        return LoopOutcome(
            status="failed",
            steps=job.steps,
            usage=job.usage,
            elapsed_ms=job.pending.elapsed_ms if job.pending is not None else 0,
            error=LoopError(code=code, message=message),
        )

    def _user_stopped(self, job: _Job) -> bool:
        # 서버 종료 신호는 중지가 아니다. 하위 에이전트 대기는 그대로 저장하고 자식은 복구가 잇는다.
        cancel = job.cancel
        return cancel is not None and cancel.is_set() and cancel.reason != "shutdown"

    async def _stop_requested(self, job: _Job) -> bool:
        if self._user_stopped(job):
            return True
        async with self.db.read() as session:
            run = await RunRepository(session, job.ref.workspace_id).get(job.ref.run_id)
        return run is not None and run.cancel_requested_at is not None

    async def _finish(
        self,
        job: _Job,
        outcome: LoopOutcome,
        handle: WorkspaceHandle | None,
        ports: _DbPorts | None = None,
    ) -> None:
        ref = job.ref
        pending = outcome.pending if outcome.status == "waiting_input" else None
        child_wait = pending is not None and pending.kind == "agent_call"
        if child_wait and await self._stop_requested(job):
            # call_agent로 멈추는 바로 그 스텝에 중지가 왔다. 루프의 멈춤은 취소를 보지 않으므로
            # 여기서 런을 cancelled로 끝내고 자식은 큐에 넣지 않는다(3.6).
            outcome = _stopped_at_pause(outcome)
            pending, child_wait = None, False
        shutdown = outcome.interrupted == "shutdown"
        terminal = outcome.status != "waiting_input"
        # 종료 중에는 지우지 않는다(유예 시간 안에 끝내야 한다). 폴더는 세션과 함께 남는다.
        release = terminal and not shutdown and job.ephemeral
        truncated = outcome.changes_truncated
        if terminal and not outcome.files_collected and not shutdown and ports is not None:
            try:
                truncated = (await ports.collect_files()).truncated
            except Exception:
                log.exception("run.collect_failed", run_id=str(ref.run_id))
        disk: tuple[int, bool] | None = None
        if handle is not None and not shutdown and not release:
            try:
                stats = await self.backend.stats(handle)
                disk = (stats.disk_bytes, stats.disk_estimated)
            except SandboxError as exc:
                log.warning("run.stats_failed", run_id=str(ref.run_id), error=exc.message)
        now = self.now()
        error = outcome.error.model_dump() if outcome.error is not None else None
        output: dict[str, Any] | None = None
        orphaned: list[tuple[RunRef, dict[str, object]]] = []
        claimed: ClaimedParent | None = None
        async with self.db.write() as session:
            run = await RunRepository(session, ref.workspace_id).get(ref.run_id)
            if run is not None:
                if child_wait and run.cancel_requested_at is not None:
                    # 위에서 본 뒤에 중지가 들어왔다. 같은 트랜잭션에서 보고 자식을 넣지 않는다.
                    outcome = _stopped_at_pause(outcome)
                    pending, child_wait, terminal = None, False, True
                    error = None
                run.status = outcome.status
                run.steps = outcome.steps
                run.usage = outcome.usage.model_dump(mode="json")
                run.error = error
                run.model = self._model_info(job, run.model or {}, outcome.models_used)
                if job.task is not None and terminal:
                    # 결과는 submit_result가 받아들인 값뿐이다. 실패·취소면 None.
                    result = outcome.output if outcome.status == "succeeded" else None
                    output = await task_output(
                        session, ref.workspace_id, run, status=outcome.status, result=result
                    )
                    if outcome.limit is not None:
                        # 하위 에이전트로 불린 과업이면 부모가 "스텝 한도"를 알아야 한다(3.4).
                        output["limit"] = outcome.limit
                    run.output = output
                if outcome.status == "waiting_input" and outcome.pending is not None:
                    pending = outcome.pending.model_copy(
                        update={"changes_since": job.changes_since}
                    )
                    run.pending_interaction = pending.model_dump(mode="json")
                    run.ended_at = None
                else:
                    run.pending_interaction = None
                    run.ended_at = now
                if terminal:
                    # 이번 구간에 만들고 큐에 넣지 못한 자식은 기다릴 부모가 없다(3.3).
                    orphaned = await self._orphan_created(session, job, now)
                    claimed = await claim_parent(session, ref.workspace_id, run, now)
            instance = await WorkspaceInstanceRepository(session, ref.workspace_id).get_for_session(
                job.session_id
            )
            if instance is not None:
                instance.status = WorkspaceInstanceStatus.READY
                instance.last_activity_at = now
                if disk is not None:
                    instance.disk_bytes, instance.disk_estimated = disk
                    instance.disk_measured_at = now
        if child_wait and pending is not None and pending.child_run_id is not None:
            # 부모는 run.completed 없이 열린 채로 둔다. 스트림은 자식이 도는 동안 ping으로 이어지고
            # 재개 뒤의 이벤트가 같은 채널로 온다(6.1). 자식은 대기를 저장한 뒤에야 넣는다(3.3).
            child_ref = RunRef(
                run_id=pending.child_run_id,
                session_id=pending.child_session_id,
                workspace_id=ref.workspace_id,
            )
            children = self.children()
            await children.launch_child(child_ref)
            await children.settle_if_finished(ref.workspace_id, pending.child_run_id)
            log.info(
                "run.waiting_child",
                run_id=str(ref.run_id),
                child_run_id=str(pending.child_run_id),
                steps=outcome.steps,
            )
            return
        completed: dict[str, Any] = {
            "status": outcome.status,
            "usage_total": outcome.usage.model_dump(mode="json"),
            "steps": outcome.steps,
            "changes_truncated": truncated,
            "limit": outcome.limit,
            "output": output,
            "error": error,
        }
        if release:
            # run.completed를 받은 호출자가 바로 확인해도 폴더가 없게 먼저 지운다.
            await release_session_workspace(
                self.db,
                ref.workspace_id,
                job.session_id,
                backend=self.backend,
                workspaces_dir=self.settings.data_dir / "workspaces",
            )
        if outcome.status == "failed" and error is not None:
            await self.publish(ref, "run.error", error)
        await self.publish(ref, "run.completed", completed)
        self.bus.end(ref.run_id)
        for orphan_ref, data in orphaned:
            await self.publish(orphan_ref, "run.completed", data)
            self.bus.end(orphan_ref.run_id)
        if claimed is not None:
            await self.children().launch_parent(claimed)
        log.info(
            "run.finished",
            run_id=str(ref.run_id),
            status=outcome.status,
            steps=outcome.steps,
            code=outcome.error.code if outcome.error is not None else None,
            input_tokens=outcome.usage.input_tokens,
            output_tokens=outcome.usage.output_tokens,
        )

    async def _orphan_created(
        self, session: AsyncSession, job: _Job, now: datetime
    ) -> list[tuple[RunRef, dict[str, object]]]:
        created = job.children_created
        if not created:
            return []
        runs = RunRepository(session, job.ref.workspace_id)
        rows = [
            row for row in [await runs.get(child_id) for child_id in created] if row is not None
        ]
        return await orphan_runs(session, job.ref.workspace_id, rows, now)

    def _model_info(self, job: _Job, current: Mapping[str, Any], used: list[str]) -> dict[str, Any]:
        info = dict(current)
        if used:
            expected = job.model_id or self._default_model()
            info["used"] = used[-1]
            info["fallback_used"] = bool(
                info.get("fallback_used") or any(model != expected for model in used)
            )
        return info

    async def _abort(self, job: _Job, code: str, message: str, *, retryable: bool = False) -> None:
        """루프 밖에서 예외로 끝났을 때. 파일 수집 없이 상태만 남긴다."""
        outcome = LoopOutcome(
            status="failed",
            steps=job.steps,
            usage=job.usage,
            elapsed_ms=0,
            error=LoopError(code=code, message=message, retryable=retryable),
            interrupted="shutdown" if code == "shutdown" else None,
        )
        try:
            async with self.db.read() as session:
                run = await RunRepository(session, job.ref.workspace_id).get(job.ref.run_id)
                if run is not None:
                    outcome.steps = run.steps
                    outcome.usage = UsageTotal.model_validate(run.usage or {})
                    if run.status != RunStatus.RUNNING:
                        return
            await self._finish(job, outcome, None)
        except Exception:
            log.exception("runner.abort_failed", run_id=str(job.ref.run_id))
