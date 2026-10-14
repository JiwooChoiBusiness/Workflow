"""런 생성·재개·중지·조회(설계 2.5, 4.9, 6.2).

런은 HTTP 요청 수명과 분리된다. 여기서는 런 행을 queued로 만들고 큐에 넣는 데까지 하고, 실행은
Supervisor 워커가 RunExecutor로 한다(run_executor.py). 큐 항목은 "<workspace_id>:<run_id>"다. 워커가
워크스페이스 범위 리포지토리로 런을 읽어야 하기 때문이다.

이벤트 버스에 발행할 때는 DB 쓰기 트랜잭션을 잡고 있지 않는다. 버스가 seq를 이어 쓰려고 writer를
비울 때 같은 쓰기 잠금을 기다리기 때문이다.

워크플로우 agent 노드가 만든 자식 런이 입력 대기에서 벗어나면(응답·만료·중지) 그 자식을 기다리는
워크플로우 런을 다시 큐에 넣어야 한다(2단계 설계 3.7). 그 일은 주입받은 ChildRunListener가 한다.

call_agent의 자식 런(run.parent_run_id)은 app/services/child_runs.py가 맡는다. 자식이 여기서 끝나면
(중지·만료) 같은 쓰기 트랜잭션에서 claim_parent로 부모를 queued로 돌리고, 트랜잭션 밖에서
launch_parent로 알리고 넣는다. 부모를 멈추면 열린 자식·손자도 멈춘다(오케스트레이션 3.3, 3.6).
하위 에이전트를 기다리는 런은 DB에서 waiting_input + agent_call이지만 API에는 running + child로
보인다(6.4).
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.child_runs import child_correlation
from app.agent.context import AttachmentNote, context_text, user_message_blocks
from app.agent.interactions import (
    InteractionError,
    InteractionReply,
    PendingInteraction,
    resolve_reply,
)
from app.config import Settings
from app.db.engine import Database
from app.db.models import ChatSession, Message, Run, SessionFile, WorkspaceInstance
from app.db.models.enums import (
    MessageRole,
    RunStatus,
    RunTrigger,
    SessionFileKind,
    SessionSource,
)
from app.db.repositories.agent_versions import AgentVersionRepository
from app.db.repositories.messages import MessageRepository
from app.db.repositories.run_events import RunEventRepository
from app.db.repositories.runs import RunRepository
from app.db.repositories.session_files import SessionFileRepository
from app.db.repositories.sessions import SessionRepository
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.llm.types import ImageBlock
from app.runner.supervisor import Supervisor, SupervisorClosed
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.sandbox.disk import format_size
from app.sandbox.local_workspace import remove_workspace_dir
from app.schemas.agent_version import ModelConfig
from app.schemas.common import RunLimits
from app.schemas.events import TERMINAL_EVENT_TYPES, RunEventOut, UsageTotal
from app.schemas.run import (
    PendingInteractionOut,
    RunChildOut,
    RunErrorOut,
    RunEventList,
    RunOut,
    RunResult,
)
from app.schemas.session import MessageCreate, SessionFileOut
from app.schemas.task import SessionFileRef, TaskRunInput
from app.schemas.workspace import WorkspaceSettings
from app.services.access import SessionScope, scoped_session
from app.services.errors import ServiceError, not_found
from app.storage.base import StorageBackend, StorageError
from app.streaming.event_bus import BusEvent, EventBus, RunRef, Subscription

if TYPE_CHECKING:
    from app.services.child_runs import ChildRunService, ClaimedParent

log = structlog.get_logger("app.services.runs")

TERMINAL_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})
# 모델에 이미지로 보내는 첨부(설계 12.1 Q7). 나머지 첨부는 작업공간 파일로만 쓴다.
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_IMAGE_BYTES = 5 * 1024 * 1024
TITLE_CHARS = 50
MAX_EVENTS_PAGE = 1000
TOOL_OUTPUTS_PREFIX = "tool-outputs"


def run_key(workspace_id: uuid.UUID, run_id: uuid.UUID) -> str:
    return f"{workspace_id}:{run_id}"


def parse_run_key(key: str) -> tuple[uuid.UUID, uuid.UUID]:
    workspace, _, run = key.partition(":")
    return uuid.UUID(workspace), uuid.UUID(run)


def run_ref(run: Run) -> RunRef:
    return RunRef(run_id=run.id, session_id=run.session_id, workspace_id=run.workspace_id)


def session_of(ref: RunRef) -> uuid.UUID:
    """에이전트 런의 세션. 에이전트 런 ref에는 늘 있고 워크플로우 런 ref에만 없다."""
    if ref.session_id is None:
        raise ValueError(f"세션이 없는 런이다: {ref.run_id}")
    return ref.session_id


class ChildRunListener(Protocol):
    """자식 런(워크플로우 agent 노드의 과업 런)이 입력 대기에서 벗어났을 때 부르는 곳.

    그 자식을 기다리던 워크플로우 런을 queued로 바꿔 워크플로우 레인에 다시 넣는다. 자식이 아닌
    런이나 이미 재개된 부모는 건너뛴다. 다시 넣은 부모 수를 돌려준다.
    """

    async def resume_parents(
        self, workspace_id: uuid.UUID, child_run_ids: Sequence[uuid.UUID]
    ) -> int: ...


def pending_of(run: Run) -> PendingInteraction | None:
    if not run.pending_interaction:
        return None
    try:
        return PendingInteraction.model_validate(run.pending_interaction)
    except ValidationError:
        log.warning("run.pending_invalid", run_id=str(run.id))
        return None


def waits_for_child(run: Run, pending: PendingInteraction | None = None) -> bool:
    """하위 에이전트(call_agent의 자식 런)를 기다리는 런인지. 사람 입력을 기다리는 게 아니다."""
    if run.status != RunStatus.WAITING_INPUT:
        return False
    pending = pending or pending_of(run)
    return pending is not None and pending.kind == "agent_call"


def run_depth(run: Run) -> int:
    correlation = child_correlation((run.input or {}).get("correlation"))
    return correlation.depth if correlation is not None else 0


def _child_out(pending: PendingInteraction) -> RunChildOut | None:
    if pending.child_run_id is None or pending.child_session_id is None or pending.agent_id is None:
        return None
    return RunChildOut(
        run_id=pending.child_run_id,
        session_id=pending.child_session_id,
        agent_id=pending.agent_id,
        agent_slug=pending.agent_slug or "",
        agent_name=pending.agent_name or "",
        tool_call_id=pending.tool_call_id,
        version=pending.version or "published",
        since=pending.created_at,
    )


def run_out(run: Run) -> RunOut:
    pending = pending_of(run)
    child = waits_for_child(run, pending)
    # 하위 호출 대기는 밖에서 보면 call_agent 도구를 실행하는 중이다(오케스트레이션 6.4). 슬롯을
    # 놓고 waiting_input으로 둔 것은 구현 사정이라 1단계 클라이언트가 답하라는 뜻으로 읽지 않게
    # 한다.
    return RunOut(
        id=run.id,
        session_id=run.session_id,
        agent_version_id=run.agent_version_id,
        trigger=run.trigger,
        status=RunStatus.RUNNING if child else run.status,  # pyright: ignore[reportArgumentType]
        user_message_id=run.user_message_id,
        assistant_message_id=run.assistant_message_id,
        model=run.model or {},
        limits=run.limits or {},
        approval_mode=run.approval_mode,
        steps=run.steps,
        usage=UsageTotal.model_validate(run.usage or {}),
        error=RunErrorOut.model_validate(run.error) if run.error else None,
        pending_interaction=(
            PendingInteractionOut.model_validate(
                {**pending.event_data(), "created_at": pending.created_at}
            )
            if pending is not None and not pending.waits_for_child
            else None
        ),
        output=run.output,
        cancel_requested_at=run.cancel_requested_at,
        queued_at=run.queued_at,
        started_at=run.started_at,
        ended_at=run.ended_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
        parent_run_id=run.parent_run_id,
        depth=run_depth(run),
        child=_child_out(pending) if child and pending is not None else None,
    )


def session_file_out(row: SessionFile) -> SessionFileOut:
    return SessionFileOut(
        id=row.id,
        session_id=row.session_id,
        run_id=row.run_id,
        message_id=row.message_id,
        kind=row.kind,  # pyright: ignore[reportArgumentType]
        path=row.path,
        filename=row.filename,
        mime=row.mime,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        downloadable=row.storage_key is not None,
        reason=row.reason,
        created_at=row.created_at,
    )


def event_out(event: BusEvent) -> RunEventOut:
    return RunEventOut(
        run_id=event.run_id,
        session_id=event.session_id,
        conversation_id=event.conversation_id,
        seq=event.seq,
        ts=event.ts,
        type=event.type,
        data=event.data,
    )


def completed_data(run: Run, *, status: str) -> dict[str, object]:
    return {
        "status": status,
        "usage_total": UsageTotal.model_validate(run.usage or {}).model_dump(mode="json"),
        "steps": run.steps,
        "error": run.error,
        "output": run.output,
    }


def task_input_of(run: Run) -> TaskRunInput | None:
    """과업 런이면 run.input. 채팅 런이나 읽을 수 없는 값이면 None."""
    if run.trigger != RunTrigger.TASK or not run.input:
        return None
    try:
        return TaskRunInput.model_validate(run.input)
    except ValidationError:
        log.warning("run.task_input_invalid", run_id=str(run.id))
        return None


def session_file_ref(row: SessionFile) -> SessionFileRef:
    return SessionFileRef(
        id=row.id,
        path=row.path,
        filename=row.filename,
        mime=row.mime,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        downloadable=row.storage_key is not None,
    )


async def task_output(
    session: AsyncSession, workspace_id: uuid.UUID, run: Run, *, status: str, result: Any
) -> dict[str, Any]:
    """run.output(4.10): {status, result, text(마지막 어시스턴트 글), files, usage}."""
    messages = await MessageRepository(session, workspace_id).list_for_run(run.id)
    text = next(
        (row.text for row in reversed(messages) if row.role == MessageRole.ASSISTANT and row.text),
        "",
    )
    files = await SessionFileRepository(session, workspace_id).list_for_session(
        run.session_id, kind=SessionFileKind.OUTPUT, run_id=run.id
    )
    return {
        "status": status,
        "result": result,
        "text": text,
        "files": [session_file_ref(row).model_dump(mode="json") for row in files],
        "usage": UsageTotal.model_validate(run.usage or {}).model_dump(mode="json"),
    }


def workspace_handle(
    backend: SandboxBackend, session_id: uuid.UUID, instance: WorkspaceInstance | None
) -> WorkspaceHandle:
    # 로컬 백엔드는 세션 id로 폴더가 정해진다. DATA_DIR을 옮겼어도 지금 위치를 가리키게 한다.
    workspaces = getattr(backend, "workspaces_dir", None)
    if isinstance(workspaces, Path):
        root = workspaces.resolve() / str(session_id)
        return WorkspaceHandle(session_id=session_id, root=root, backend="local")
    if instance is None:
        raise ServiceError(404, "workspace_missing", "이 세션에는 작업공간이 없다")
    return WorkspaceHandle(
        session_id=session_id,
        root=Path(instance.root_path),
        backend=instance.backend,  # pyright: ignore[reportArgumentType]
    )


async def release_session_workspace(
    db: Database,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    backend: SandboxBackend | None,
    workspaces_dir: Path,
    only_if: Callable[[AsyncSession], Awaitable[bool]] | None = None,
) -> bool:
    """과업 ephemeral 세션의 작업공간을 지운다(3.3). 결과 파일은 이미 storage에 있다.

    세션 행은 로그로 남기고 workspace_instance 행만 지운다. 같은 세션을 reuse로 다시 쓰면 처음처럼
    스냅샷부터 새로 채운다. 백엔드가 없는 곳(하우스키핑)은 로컬 폴더를 바로 지운다. only_if는 행을
    지우는 쓰기 트랜잭션 안에서 다시 보는 조건이고, 거짓이면 아무것도 지우지 않는다. 폴더까지
    지웠으면 True.
    """
    async with db.write() as session:
        if only_if is not None and not await only_if(session):
            return False
        instances = WorkspaceInstanceRepository(session, workspace_id)
        row = await instances.get_for_session(session_id)
        if row is not None:
            await instances.delete(row.id)
        chat = await SessionRepository(session, workspace_id).get(session_id)
        if chat is not None:
            chat.workspace_instance_id = None
    try:
        if backend is not None:
            await backend.release(workspace_handle(backend, session_id, row), delete=True)
        else:
            root = workspaces_dir.resolve() / str(session_id)
            await asyncio.to_thread(remove_workspace_dir, root)
    except (SandboxError, ServiceError) as exc:
        log.warning("task.workspace_release_failed", session_id=str(session_id), error=str(exc))
        return False
    log.info("task.workspace_released", session_id=str(session_id))
    return True


async def _ephemeral_task_left_over(
    session: AsyncSession, *, workspace_id: uuid.UUID, session_id: uuid.UUID
) -> bool:
    """마지막 런이 끝난 ephemeral 과업이고 열린 런이 없는 세션인지.

    러너는 서버 종료 중에 끝난 ephemeral 런의 작업공간을 지우지 않고 남긴다(유예 시간 안에 끝내야
    한다). 하우스키핑이 이런 작업공간을 찾을 때 쓴다. 같은 세션을 reuse로 이어 썼으면 마지막 런이
    reuse라 남긴다.
    """
    runs = RunRepository(session, workspace_id)
    if await runs.active_for_session(session_id) is not None:
        return False
    last = await runs.latest_for_session(session_id)
    if last is None or last.status not in TERMINAL_STATUSES:
        return False
    task = task_input_of(last)
    return task is not None and task.session_mode == "ephemeral"


async def _claim_parent(
    session: AsyncSession, workspace_id: uuid.UUID, run: Run, now: datetime
) -> "ClaimedParent | None":
    """call_agent의 자식이 끝났으면 같은 트랜잭션에서 기다리던 부모를 queued로 돌린다(3.3)."""
    if run.parent_run_id is None:
        return None
    # child_runs가 이 모듈을 import하므로 쓸 때 불러온다.
    from app.services.child_runs import claim_parent

    return await claim_parent(session, workspace_id, run, now)


async def _propagate_up(session: AsyncSession, workspace_id: uuid.UUID, run: Run) -> None:
    from app.services.child_runs import propagate_auto_approve_up

    await propagate_auto_approve_up(session, workspace_id, run)


def run_error_of(completed: dict[str, object]) -> dict[str, object]:
    error = completed.get("error")
    return dict(error) if isinstance(error, dict) else {"code": "internal_error", "message": ""}


def _title(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:TITLE_CHARS] or "새 대화"


@dataclass
class StartedRun:
    run_id: uuid.UUID
    session_id: uuid.UUID
    subscription: Subscription | None


@dataclass
class ToolOutput:
    filename: str
    storage_key: str | None
    text: str


class RunService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        bus: EventBus,
        supervisor: Supervisor,
        storage: StorageBackend,
        backend: SandboxBackend | None = None,
        clock: Callable[[], datetime] = utcnow,
        parents: ChildRunListener | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._bus = bus
        self._supervisor = supervisor
        self._storage = storage
        self._backend = backend
        self._clock = clock
        self._parents = parents

    def ensure_accepting(self) -> None:
        if self._supervisor.shutting_down or self._bus.closed:
            raise ServiceError(503, "shutting_down", "서버가 종료 중이라 새 런을 받지 않는다")

    def children(self) -> "ChildRunService":
        # child_runs가 이 모듈을 import하므로 쓸 때 불러온다.
        from app.services.child_runs import ChildRunService

        return ChildRunService(
            self._db,
            self._settings,
            bus=self._bus,
            supervisor=self._supervisor,
            storage=self._storage,
            backend=self._backend,
            clock=self._clock,
            parents=self._parents,
        )

    async def enqueue(
        self, ref: RunRef, *, subscribe: bool, leave_if_closed: bool = False
    ) -> StartedRun:
        """이미 queued로 저장한 런을 큐에 넣는다(과업 런을 만드는 app/services/tasks.py가 쓴다).

        leave_if_closed면 종료 중이라 넣지 못해도 런을 실패시키지 않고 queued로 둔다. 다음 기동의
        복구가 넣는다. 하위 에이전트 호출의 자식·재개하는 부모가 쓴다(오케스트레이션 3.3, 3.6).
        """
        subscription = await self._start(ref, subscribe=subscribe, leave_if_closed=leave_if_closed)
        return StartedRun(run_id=ref.run_id, session_id=session_of(ref), subscription=subscription)

    async def send_message(
        self,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        body: MessageCreate,
        *,
        subscribe: bool,
        inputs: Mapping[str, Any] | None = None,
    ) -> StartedRun:
        """사용자 메시지와 런을 만들어 큐에 넣는다.

        질문(ask_user)을 기다리는 런이 있으면 새 런을 만들지 않고 이 글을 답으로 쓴다(4.9).
        inputs는 서비스 API chat-messages의 inputs로, 그 메시지의 <context>에 들어간다.
        """
        self.ensure_accepting()
        now = self._clock()
        answer_for: uuid.UUID | None = None
        async with self._db.write() as session:
            chat = await SessionRepository(session, workspace_id).get_active(session_id)
            if chat is None:
                raise not_found("세션")
            runs = RunRepository(session, workspace_id)
            active = await runs.active_for_session(session_id)
            if active is not None:
                pending = pending_of(active)
                waiting_question = (
                    active.status == RunStatus.WAITING_INPUT
                    and pending is not None
                    and pending.kind == "question"
                )
                if not waiting_question or body.file_ids:
                    child = waits_for_child(active, pending)
                    details: dict[str, object] = {
                        "run_id": str(active.id),
                        "status": RunStatus.RUNNING if child else active.status,
                        "interaction": (
                            pending.kind if pending is not None and not child else None
                        ),
                    }
                    if child and pending is not None:
                        details["child_run_id"] = str(pending.child_run_id)
                    raise ServiceError(
                        409,
                        "run_in_progress",
                        "이 세션에서 진행 중인 런이 있다. 끝나거나 승인·답을 받은 뒤 다시 보낸다",
                        details=details,
                    )
                answer_for = active.id
            else:
                run_id = await self._create_run(session, workspace_id, chat, body, now, inputs)
        if answer_for is not None:
            return await self.provide_input(
                workspace_id, answer_for, InteractionReply(answer=body.text), subscribe=subscribe
            )
        ref = RunRef(run_id=run_id, session_id=session_id, workspace_id=workspace_id)
        subscription = await self._start(ref, subscribe=subscribe)
        log.info("run.created", run_id=str(run_id), session_id=str(session_id))
        return StartedRun(run_id=run_id, session_id=session_id, subscription=subscription)

    async def _create_run(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        chat: ChatSession,
        body: MessageCreate,
        now: datetime,
        inputs: Mapping[str, Any] | None = None,
    ) -> uuid.UUID:
        instance = await WorkspaceInstanceRepository(session, workspace_id).get_for_session(chat.id)
        limit = self._settings.workspace_max_bytes
        if instance is not None and limit and instance.disk_bytes > limit:
            raise ServiceError(
                409,
                "workspace_disk_limit",
                f"작업공간이 디스크 한도({format_size(limit)})를 넘었다. 작업공간 패널에서 "
                "패키지를 지우거나 초기화한다",
                details={"disk_bytes": instance.disk_bytes, "limit_bytes": limit},
            )
        version = await AgentVersionRepository(session, workspace_id).get(chat.agent_version_id)
        if version is None:
            raise not_found("에이전트 버전")
        model = ModelConfig.model_validate(version.model or {})
        limits = RunLimits.model_validate(version.limits or {})
        files = SessionFileRepository(session, workspace_id)
        uploads = await files.get_many(body.file_ids)
        by_id = {row.id: row for row in uploads}
        attachments: list[SessionFile] = []
        for file_id in body.file_ids:
            row = by_id.get(file_id)
            if (
                row is None
                or row.session_id != chat.id
                or row.kind != SessionFileKind.UPLOAD
                or row.message_id is not None
                or row.storage_key is None
            ):
                raise ServiceError(
                    422,
                    "file_not_found",
                    "이 세션에 올린 뒤 아직 보내지 않은 첨부 파일이 아니다",
                    details={"file_id": str(file_id)},
                )
            attachments.append(row)

        message_id = new_id()
        run_id = new_id()
        notes: list[AttachmentNote] = []
        images: list[ImageBlock] = []
        for row in attachments:
            row.path = f"uploads/{message_id}/{row.filename}"
            notes.append(AttachmentNote(path=row.path, size=row.size_bytes, mime=row.mime))
            if row.mime in IMAGE_MIME_TYPES and row.size_bytes <= MAX_IMAGE_BYTES:
                images.append(
                    ImageBlock(
                        media_type=row.mime, storage_key=row.storage_key, filename=row.filename
                    )
                )
        context = context_text(
            now=now.astimezone(),
            source=chat.source,
            external_user=chat.external_user,
            attachments=notes,
            inputs=inputs,
        )
        messages = MessageRepository(session, workspace_id)
        messages.add(
            Message(
                id=message_id,
                session_id=chat.id,
                run_id=run_id,
                seq=await messages.next_seq(chat.id),
                role=MessageRole.USER,
                blocks=user_message_blocks(body.text, context=context, images=images),
                text=body.text,
                files=[str(row.id) for row in attachments],
            )
        )
        # run.user_message_id가 메시지를 가리키므로 메시지를 먼저 넣는다(외래 키는 즉시 검사된다).
        await messages.flush()
        runs = RunRepository(session, workspace_id)
        runs.add(
            Run(
                id=run_id,
                session_id=chat.id,
                agent_version_id=chat.agent_version_id,
                trigger=RunTrigger.CHAT,
                status=RunStatus.QUEUED,
                user_message_id=message_id,
                model={"model_id": model.model_id},
                limits=limits.model_dump(mode="json"),
                approval_mode=version.approval_mode,
                usage=UsageTotal().model_dump(mode="json"),
                steps=0,
                queued_at=now,
            )
        )
        await runs.flush()
        for row in attachments:
            row.message_id = message_id
            row.run_id = run_id
        chat.message_count += 1
        chat.last_message_at = now
        if not chat.title:
            chat.title = _title(body.text)
        return run_id

    async def _start(
        self,
        ref: RunRef,
        *,
        subscribe: bool,
        after: int | None = None,
        leave_if_closed: bool = False,
    ) -> Subscription | None:
        """run.queued를 먼저 발행해 채널을 연 뒤 구독하고 큐에 넣는다.

        구독이 큐 투입보다 먼저라 짧은 런이 구독 전에 끝나도 이벤트를 놓치지 않는다. after가 없으면
        run.queued부터 준다.
        """
        stats = self._supervisor.stats()
        position = int(stats["queued"]) + 1
        published = await self._bus.publish(ref, "run.queued", {"position": position})
        start = (published[0].seq - 1 if published else 0) if after is None else after
        subscription = self._bus.subscribe(ref, after=start) if subscribe else None
        try:
            self._supervisor.enqueue(run_key(ref.workspace_id, ref.run_id))
        except SupervisorClosed:
            if subscription is not None:
                subscription.close()
            if leave_if_closed:
                log.info("run.enqueue_deferred", run_id=str(ref.run_id))
                return None
            await self._fail_unstarted(ref, "shutdown", "서버가 종료 중이라 런을 시작하지 못했다")
            raise ServiceError(
                503, "shutting_down", "서버가 종료 중이라 새 런을 받지 않는다"
            ) from None
        return subscription

    async def _fail_unstarted(self, ref: RunRef, code: str, message: str) -> None:
        async with self._db.write() as session:
            run = await RunRepository(session, ref.workspace_id).get(ref.run_id)
            if run is None or run.status != RunStatus.QUEUED:
                return
            run.status = RunStatus.FAILED
            run.error = {"code": code, "message": message, "retryable": True}
            run.ended_at = self._clock()
            data = completed_data(run, status=RunStatus.FAILED)
        await self._bus.publish(ref, "run.error", run_error_of(data))
        await self._bus.publish(ref, "run.completed", data)
        self._bus.end(ref.run_id)

    async def provide_input(
        self,
        workspace_id: uuid.UUID,
        run_id: uuid.UUID,
        reply: InteractionReply,
        *,
        subscribe: bool,
    ) -> StartedRun:
        self.ensure_accepting()
        now = self._clock()
        expired: tuple[RunRef, dict[str, object]] | None = None
        claimed: ClaimedParent | None = None
        release = False
        async with self._db.write() as session:
            runs = RunRepository(session, workspace_id)
            run = await runs.get(run_id)
            if run is None:
                raise not_found("런")
            pending = pending_of(run)
            if run.status != RunStatus.WAITING_INPUT or pending is None:
                raise ServiceError(
                    409,
                    "run_not_waiting",
                    "답이나 승인을 기다리는 런이 아니다",
                    details={"status": run.status},
                )
            if pending.kind == "agent_call":
                # 하위 에이전트를 기다리는 부모다. 승인·질문은 자식 런에 답한다(3.2).
                raise ServiceError(
                    409,
                    "child_in_progress",
                    "하위 에이전트가 진행 중이다. 승인·답은 하위 에이전트의 런에 보낸다",
                    details={"child_run_id": str(pending.child_run_id)},
                )
            ref = run_ref(run)
            if pending.expired(now, self._settings.waiting_input_ttl_hours):
                self._mark_input_timeout(run, now)
                release = await self._settle_task(session, workspace_id, run)
                expired = (ref, completed_data(run, status=RunStatus.CANCELLED))
                claimed = await _claim_parent(session, workspace_id, run, now)
            else:
                try:
                    payload = resolve_reply(pending, reply)
                except InteractionError as exc:
                    raise ServiceError(422, exc.code, exc.message) from None
                if payload.session_auto_approve:
                    chat = await SessionRepository(session, workspace_id).get(run.session_id)
                    if chat is not None:
                        chat.approval_override = "off"
                    if run.parent_run_id is not None:
                        # 하위 에이전트 카드에서 켰다. 사용자가 보는 대화는 부모 대화다(3.5).
                        await _propagate_up(session, workspace_id, run)
                run.status = RunStatus.QUEUED
                run.resume_payload = payload.model_dump(mode="json")
                run.cancel_requested_at = None
        if expired is not None:
            if release:
                await self._release_workspace(expired[0])
            await self._publish_input_timeout(*expired)
            await self._resume_parents(workspace_id, run_id)
            if claimed is not None:
                await self.children().launch_parent(claimed)
            raise ServiceError(409, "input_timeout", "응답 대기 시간이 지나 런이 취소되었다")
        [resolved] = await self._bus.publish(
            ref, "interaction.resolved", payload.resolved_event_data()
        )
        # 재접속하는 화면이 받는 첫 이벤트가 interaction.resolved가 되게 그 앞에서 구독한다.
        subscription = await self._start(ref, subscribe=subscribe, after=resolved.seq - 1)
        log.info("run.resumed_by_input", run_id=str(run_id), kind=payload.kind)
        # 자식을 큐에 넣은 뒤에 부모를 넣는다(2단계 3.7의 3번 순서).
        await self._resume_parents(workspace_id, run_id)
        return StartedRun(run_id=run_id, session_id=session_of(ref), subscription=subscription)

    async def _resume_parents(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
        if self._parents is None:
            return
        try:
            await self._parents.resume_parents(workspace_id, [run_id])
        except Exception:
            # 부모를 못 깨워도 자식 런 처리는 끝났다. 남은 부모는 기동 복구가 다시 본다.
            log.exception("run.parent_resume_failed", run_id=str(run_id))

    def _mark_input_timeout(self, run: Run, now: datetime) -> None:
        run.status = RunStatus.CANCELLED
        run.error = {
            "code": "input_timeout",
            "message": f"{self._settings.waiting_input_ttl_hours}시간 안에 응답이 없어 취소했다",
            "retryable": False,
        }
        run.pending_interaction = None
        run.ended_at = now

    async def _publish_input_timeout(self, ref: RunRef, completed: dict[str, object]) -> None:
        # 취소라 run.error는 내지 않는다. 이유는 run.completed.error에 있다.
        await self._bus.publish(ref, "run.completed", completed)
        self._bus.end(ref.run_id)

    async def _settle_task(self, session: AsyncSession, workspace_id: uuid.UUID, run: Run) -> bool:
        """러너를 거치지 않고 끝나는 과업 런(대기 중 중지·만료)의 run.output을 채운다.

        ephemeral이라 작업공간을 지워야 하면 True.
        """
        task = task_input_of(run)
        if task is None:
            return False
        run.output = await task_output(session, workspace_id, run, status=run.status, result=None)
        return task.session_mode == "ephemeral"

    async def _release_workspace(self, ref: RunRef) -> None:
        await release_session_workspace(
            self._db,
            ref.workspace_id,
            session_of(ref),
            backend=self._backend,
            workspaces_dir=self._settings.data_dir / "workspaces",
        )

    async def stop(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> RunOut:
        now = self._clock()
        finalized: dict[str, object] | None = None
        claimed: ClaimedParent | None = None
        release = False
        async with self._db.write() as session:
            run = await RunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("런")
            if run.status in TERMINAL_STATUSES:
                raise ServiceError(
                    409, "run_not_active", "이미 끝난 런이다", details={"status": run.status}
                )
            ref = run_ref(run)
            run.cancel_requested_at = now
            if run.status in (RunStatus.QUEUED, RunStatus.WAITING_INPUT):
                # 워커가 잡지 않은 런은 여기서 바로 끝낸다. 큐에 남은 항목은 워커가 꺼내도 상태가
                # queued가 아니라 건너뛴다. 하위 에이전트를 기다리던 부모를 먼저 cancelled로 써 두면
                # 자식이 끝날 때 claim_parent가 부모를 다시 넣지 않는다(3.6).
                run.status = RunStatus.CANCELLED
                run.pending_interaction = None
                run.resume_payload = None
                run.ended_at = now
                release = await self._settle_task(session, workspace_id, run)
                finalized = completed_data(run, status=RunStatus.CANCELLED)
                claimed = await _claim_parent(session, workspace_id, run, now)
            out = run_out(run)
        signalled = self._supervisor.cancel(run_key(workspace_id, run_id), "user")
        if finalized is not None:
            if release:
                await self._release_workspace(ref)
            await self._bus.publish(ref, "run.completed", finalized)
            self._bus.end(run_id)
            await self._resume_parents(workspace_id, run_id)
            if claimed is not None:
                await self.children().launch_parent(claimed)
            # 기다리던 자식·손자를 같은 경로로 멈춘다. 자기 트랜잭션으로 돈다(3.3).
            await self.children().cancel_subtree(workspace_id, run_id)
        elif not signalled:
            # running인데 이 프로세스에 실행 중인 태스크가 없다. 기동 복구 전에만 생기는 상태다.
            out = await self._cancel_orphan(ref)
        log.info("run.stop_requested", run_id=str(run_id), status=out.status)
        return out

    async def _cancel_orphan(self, ref: RunRef) -> RunOut:
        release = False
        claimed: ClaimedParent | None = None
        async with self._db.write() as session:
            run = await RunRepository(session, ref.workspace_id).get(ref.run_id)
            if run is None:
                raise not_found("런")
            if run.status == RunStatus.RUNNING:
                run.status = RunStatus.CANCELLED
                run.ended_at = self._clock()
                release = await self._settle_task(session, ref.workspace_id, run)
                claimed = await _claim_parent(session, ref.workspace_id, run, self._clock())
            data = completed_data(run, status=run.status)
            out = run_out(run)
        if out.status == RunStatus.CANCELLED:
            if release:
                await self._release_workspace(ref)
            await self._bus.publish(ref, "run.completed", data)
            self._bus.end(ref.run_id)
            if claimed is not None:
                await self.children().launch_parent(claimed)
            await self.children().cancel_subtree(ref.workspace_id, ref.run_id)
        return out

    async def get_run(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> RunOut:
        async with self._db.read() as session:
            run = await RunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("런")
            return run_out(run)

    async def check_run(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, scope: SessionScope
    ) -> RunOut:
        """범위(웹앱·서비스 API) 안의 세션에 속한 런. 아니면 404."""
        async with self._db.read() as session:
            run = await RunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("런")
            try:
                await scoped_session(session, workspace_id, run.session_id, scope)
            except ServiceError:
                raise not_found("런") from None
            return run_out(run)

    async def run_stream(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, *, after: int
    ) -> Subscription:
        """런 하나의 이벤트를 after 다음부터(서비스 API의 재접속). 끝난 런이면 남은 기록만 준다."""
        async with self._db.read() as session:
            run = await RunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("런")
            ref = run_ref(run)
            self._open_if_running(run, ref)
        return self._bus.subscribe(ref, after=after)

    def _open_if_running(self, run: Run, ref: RunRef) -> None:
        """DB상 열린 런(queued·running·하위 에이전트 대기)은 live로 구독하게 채널을 먼저 연다(6.1).

        사람 입력을 기다리는 런은 1단계처럼 기록만 주고 닫는다(run.completed(waiting_input)가
        끝이다).
        """
        if run.status in (RunStatus.QUEUED, RunStatus.RUNNING) or waits_for_child(run):
            self._bus.open_channel(ref)

    async def list_runs(
        self,
        workspace_id: uuid.UUID,
        *,
        session_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RunOut], int | None]:
        async with self._db.read() as session:
            session_ids: list[uuid.UUID] | None = None
            if agent_id is not None:
                sessions = await SessionRepository(session, workspace_id).list_filtered(
                    agent_id=agent_id, limit=10_000
                )
                session_ids = [row.id for row in sessions]
            rows = await RunRepository(session, workspace_id).list_filtered(
                session_id=session_id,
                session_ids=session_ids,
                status=status,
                since=since,
                until=until,
                limit=limit + 1,
                offset=offset,
            )
        more = len(rows) > limit
        return [run_out(row) for row in rows[:limit]], offset + limit if more else None

    async def _ref(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> RunRef:
        async with self._db.read() as session:
            run = await RunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("런")
            return run_ref(run)

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

    async def session_stream(
        self,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        after: int,
        run_id: uuid.UUID | None = None,
    ) -> Subscription:
        """진행 중인 런(없으면 마지막 런)의 이벤트(6.2 GET /sessions/{sid}/events)."""
        async with self._db.read() as session:
            if await SessionRepository(session, workspace_id).get_active(session_id) is None:
                raise not_found("세션")
            runs = RunRepository(session, workspace_id)
            if run_id is not None:
                run = await runs.get(run_id)
                if run is None or run.session_id != session_id:
                    raise not_found("런")
            else:
                run = await runs.active_for_session(session_id) or await runs.latest_for_session(
                    session_id
                )
                if run is None:
                    raise ServiceError(404, "no_run", "이 세션에는 아직 런이 없다")
            ref = run_ref(run)
            self._open_if_running(run, ref)
        return self._bus.subscribe(ref, after=after)

    async def wait_result(self, workspace_id: uuid.UUID, started: StartedRun) -> RunResult:
        """blocking 응답. run.completed(waiting_input 포함)까지 기다린다."""
        subscription = started.subscription
        if subscription is None:
            raise ValueError("wait_result에는 구독이 필요하다")
        finished = False
        async for event in subscription.events():
            if event.type in TERMINAL_EVENT_TYPES:
                finished = True
        if not finished:
            raise ServiceError(503, "shutting_down", "서버가 종료되어 런 결과를 기다리지 못했다")
        return await self.result(workspace_id, started.run_id)

    async def result(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> RunResult:
        async with self._db.read() as session:
            run = await RunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("런")
            answer = ""
            if run.assistant_message_id is not None:
                message = await MessageRepository(session, workspace_id).get(
                    run.assistant_message_id
                )
                answer = message.text if message is not None else ""
            files = await SessionFileRepository(session, workspace_id).list_for_session(
                run.session_id, kind=SessionFileKind.OUTPUT, run_id=run.id
            )
            return RunResult(
                run=run_out(run),
                answer=answer,
                message_id=run.assistant_message_id,
                files=[session_file_out(row) for row in files],
            )

    async def tool_output(
        self, workspace_id: uuid.UUID, run_id: uuid.UUID, tool_call_id: str
    ) -> ToolOutput:
        ref = await self._ref(workspace_id, run_id)
        await self._bus.flush()
        async with self._db.read() as session:
            rows = await RunEventRepository(session, workspace_id).list_by_type(
                ref.run_id, "tool_call.completed"
            )
        for row in reversed(rows):
            if row.payload.get("tool_call_id") != tool_call_id:
                continue
            key = row.payload.get("output_storage_key")
            return ToolOutput(
                filename=f"{tool_call_id}.txt",
                storage_key=key if isinstance(key, str) else None,
                text=str(row.payload.get("output_excerpt") or ""),
            )
        raise not_found("도구 호출 출력")


def _run_dirs(root: Path) -> list[uuid.UUID]:
    # 이름이 런 id인 폴더만 본다. 다른 이름은 플랫폼이 만든 것이 아니므로 건드리지 않는다.
    found: list[uuid.UUID] = []
    if not root.is_dir():
        return found
    for path in root.iterdir():
        try:
            run_id = uuid.UUID(path.name)
        except ValueError:
            continue
        if path.is_dir() and str(run_id) == path.name:
            found.append(run_id)
    return found


def _file_names(folder: Path) -> list[str]:
    try:
        return [path.name for path in folder.iterdir() if path.is_file()]
    except FileNotFoundError:
        return []


class RunMaintenance:
    """하우스키핑이 매시 부르는 런 정리.

    5.4 run_event·tool-outputs 보존, 3.10 waiting_input TTL, 서버 종료로 남은 ephemeral 과업의
    작업공간.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        bus: EventBus,
        storage: StorageBackend,
        backend: SandboxBackend | None = None,
        clock: Callable[[], datetime] = utcnow,
        batch_size: int = 200,
        parents: ChildRunListener | None = None,
        supervisor: Supervisor | None = None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._bus = bus
        self._storage = storage
        self._backend = backend
        self._clock = clock
        self._batch = batch_size
        self._parents = parents
        # 에이전트 레인. 만료된 자식의 부모를 다시 넣을 때 쓴다. 없으면 부모는 queued로 남아 기동
        # 복구가 넣는다.
        self._supervisor = supervisor

    def children(self) -> "ChildRunService":
        from app.services.child_runs import ChildRunService

        return ChildRunService(
            self._db,
            self._settings,
            bus=self._bus,
            supervisor=self._supervisor,
            storage=self._storage,
            backend=self._backend,
            clock=self._clock,
            parents=self._parents,
        )

    async def reconcile_children(self) -> int:
        """이미 끝난 자식을 아직 기다리는 부모를 다시 넣는다(오케스트레이션 3.8의 안전망)."""
        return await self.children().reconcile_children()

    async def _workspaces(self) -> list[tuple[uuid.UUID, WorkspaceSettings | None]]:
        async with self._db.read() as session:
            rows = await WorkspaceRepository(session).list_all()
        found: list[tuple[uuid.UUID, WorkspaceSettings | None]] = []
        for row in rows:
            try:
                found.append((row.id, WorkspaceSettings.model_validate(row.settings or {})))
            except ValidationError:
                found.append((row.id, None))
        return found

    def _cutoff(self, workspace_settings: WorkspaceSettings | None) -> datetime:
        days = (
            workspace_settings.run_event_retention_days
            if workspace_settings is not None
            else self._settings.run_event_retention_days
        )
        return self._clock() - timedelta(days=days)

    async def expire_waiting(self) -> int:
        now = self._clock()
        ttl = self._settings.waiting_input_ttl_hours
        expired: list[tuple[RunRef, dict[str, object], bool]] = []
        claimed: list[ClaimedParent] = []
        for workspace_id, _ in await self._workspaces():
            async with self._db.write() as session:
                runs = await RunRepository(session, workspace_id).list_by_status(
                    [RunStatus.WAITING_INPUT]
                )
                for run in runs:
                    pending = pending_of(run)
                    if pending is not None and (
                        pending.kind == "agent_call" or not pending.expired(now, ttl)
                    ):
                        # 하위 에이전트를 기다리는 부모는 만료하지 않는다. 자식의 한도와 대기 TTL이
                        # 상한이고, 자식이 만료되면 아래 claim_parent가 부모를 다시 넣는다(3.8).
                        continue
                    run.status = RunStatus.CANCELLED
                    run.error = {
                        "code": "input_timeout",
                        "message": f"{ttl}시간 안에 응답이 없어 취소했다",
                        "retryable": False,
                    }
                    run.pending_interaction = None
                    run.ended_at = now
                    task = task_input_of(run)
                    if task is not None:
                        run.output = await task_output(
                            session, workspace_id, run, status=run.status, result=None
                        )
                    release = task is not None and task.session_mode == "ephemeral"
                    expired.append(
                        (run_ref(run), completed_data(run, status=RunStatus.CANCELLED), release)
                    )
                    found = await _claim_parent(session, workspace_id, run, now)
                    if found is not None:
                        claimed.append(found)
        for ref, completed, release in expired:
            if release:
                await release_session_workspace(
                    self._db,
                    ref.workspace_id,
                    session_of(ref),
                    backend=self._backend,
                    workspaces_dir=self._settings.data_dir / "workspaces",
                )
            await self._bus.publish(ref, "run.completed", completed)
            self._bus.end(ref.run_id)
        if claimed:
            children = self.children()
            for item in claimed:
                await children.launch_parent(item)
        if self._parents is not None:
            # 만료된 자식을 기다리던 워크플로우 런을 다시 넣는다. 그 런의 agent 노드가 자식의
            # cancelled(input_timeout)를 받아 오류 정책으로 넘어간다(2단계 3.7의 4번).
            by_workspace: dict[uuid.UUID, list[uuid.UUID]] = {}
            for ref, _, _ in expired:
                by_workspace.setdefault(ref.workspace_id, []).append(ref.run_id)
            for workspace_id, run_ids in by_workspace.items():
                try:
                    await self._parents.resume_parents(workspace_id, run_ids)
                except Exception:
                    # 자식 만료는 이미 끝났다. 남은 부모는 기동 복구가 다시 본다.
                    log.exception("run.parent_resume_failed", workspace_id=str(workspace_id))
        return len(expired)

    async def purge_events(self) -> int:
        """보존 기간이 지난 런의 run_event와 tool-outputs 파일을 지운다(메시지·런 요약은 둔다)."""
        purged = 0
        await self._bus.flush()
        for workspace_id, workspace_settings in await self._workspaces():
            cutoff = self._cutoff(workspace_settings)
            while True:
                async with self._db.write() as session:
                    events = RunEventRepository(session, workspace_id)
                    run_ids = await events.runs_ended_before(cutoff, limit=self._batch)
                    keys: list[str] = []
                    for run_id in run_ids:
                        for row in await events.list_by_type(run_id, "tool_call.completed"):
                            key = row.payload.get("output_storage_key")
                            if isinstance(key, str):
                                keys.append(key)
                    await events.delete_for_runs(run_ids)
                await self._delete_keys(keys)
                purged += len(run_ids)
                if len(run_ids) < self._batch:
                    break
        return purged

    async def purge_tool_outputs(self) -> int:
        """런 행이 없거나 보존 기간이 지난 런의 tool-outputs/<run_id> 폴더를 지운다.

        purge_events가 이벤트를 지운 뒤 파일 삭제에 실패해(백신이 잡고 있는 등) 남은 파일과 파일만
        지우고 남긴 빈 폴더다. 끝나지 않은 런의 폴더는 건드리지 않는다.
        """
        try:
            root = self._storage.local_path(TOOL_OUTPUTS_PREFIX)
        except StorageError:
            return 0
        run_ids = await asyncio.to_thread(_run_dirs, root)
        stale = set(run_ids)
        for workspace_id, workspace_settings in await self._workspaces():
            cutoff = self._cutoff(workspace_settings)
            for start in range(0, len(run_ids), self._batch):
                async with self._db.read() as session:
                    ended = await RunRepository(session, workspace_id).ended_at_of(
                        run_ids[start : start + self._batch]
                    )
                for run_id, ended_at in ended.items():
                    if ended_at is None or ended_at >= cutoff:
                        stale.discard(run_id)
        removed = 0
        for run_id in sorted(stale):
            folder = root / str(run_id)
            names = await asyncio.to_thread(_file_names, folder)
            await self._delete_keys([f"{TOOL_OUTPUTS_PREFIX}/{run_id}/{name}" for name in names])
            try:
                await asyncio.to_thread(folder.rmdir)
            except OSError as exc:
                log.warning("run.tool_output_dir_delete_failed", run_id=str(run_id), error=str(exc))
                continue
            removed += 1
        return removed

    async def release_task_workspaces(self) -> int:
        """서버 종료 중에 끝나 지우지 못한 ephemeral 과업 작업공간을 지운다(3.3, 12.2 M8).

        reuse 세션과 열린 런이 있는 세션은 건드리지 않는다. 고른 뒤 같은 세션에 reuse 과업이 들어올
        수 있어 조건을 행을 지우는 쓰기 트랜잭션에서 한 번 더 본다.
        """
        released = 0
        for workspace_id, _ in await self._workspaces():
            candidates: list[uuid.UUID] = []
            async with self._db.read() as session:
                instances = await WorkspaceInstanceRepository(
                    session, workspace_id
                ).list_for_source(SessionSource.TASK)
                for row in instances:
                    if await _ephemeral_task_left_over(
                        session, workspace_id=workspace_id, session_id=row.session_id
                    ):
                        candidates.append(row.session_id)
            for session_id in candidates:
                if await release_session_workspace(
                    self._db,
                    workspace_id,
                    session_id,
                    backend=self._backend,
                    workspaces_dir=self._settings.data_dir / "workspaces",
                    only_if=partial(
                        _ephemeral_task_left_over, workspace_id=workspace_id, session_id=session_id
                    ),
                ):
                    released += 1
        return released

    async def _delete_keys(self, keys: Sequence[str]) -> None:
        for key in keys:
            try:
                await self._storage.delete(key)
            except Exception:
                log.warning("run.tool_output_delete_failed", key=key)
