"""챗플로우 대화와 메시지(2단계 설계 4.1, 6.1~6.3).

대화는 만든 때의 버전(미리보기는 초안)으로 이어 간다. 메시지를 보내면 user·assistant 메시지 두 행과
워크플로우 런 하나를 만들어 워크플로우 레인에 넣는다. assistant 행은 런과 1:1이고, 실행기가 런을
끝낼 때 답 글·사용량·상태를 채운다. 대화 변수는 성공한 런이 끝날 때만 바뀐다. 대화에 아직 끝나지
않은 런(입력 대기 포함)이 있으면 새 메시지는 409 run_in_progress다.

웹앱·서비스 API는 access.FlowScope로 범위를 확인한 뒤 같은 메서드를 부른다. 콘솔은 범위가 없다.
"""

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import Database
from app.db.models import WfConversation, WfMessage, WfRun, Workflow
from app.db.models.enums import (
    MessageRole,
    RunStatus,
    SessionStatus,
    WfConversationSource,
    WfFileKind,
    WfRunTrigger,
    WorkflowKind,
)
from app.db.repositories.wf_conversations import WfConversationRepository, WfMessageRepository
from app.db.repositories.workflows import (
    WfFileRepository,
    WfRunRepository,
    WfVersionRepository,
    WorkflowRepository,
)
from app.db.types import new_id, utcnow
from app.runner.supervisor import Supervisor, SupervisorClosed
from app.schemas.events import TERMINAL_EVENT_TYPES, UsageTotal
from app.schemas.wf_conversation import (
    ConversationVariableOut,
    WfChatResult,
    WfConversationCreate,
    WfConversationDetail,
    WfConversationList,
    WfConversationOut,
    WfConversationPatch,
    WfConversationVariables,
    WfMessageCreate,
    WfMessageList,
    WfMessageOut,
)
from app.services.access import FlowScope, scoped_conversation
from app.services.errors import ServiceError, not_found
from app.services.runs import TITLE_CHARS
from app.services.workflow_files import WorkflowFileService, wf_file_out
from app.services.workflow_runs import (
    WorkflowRunService,
    enqueue,
    fail_unstarted,
    pending_of,
    run_limits,
    wf_run_out,
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
from app.storage.base import StorageBackend, StorageError
from app.streaming.event_bus import EventBus, Subscription
from app.workflow import graph as graph_check
from app.workflow.nodes.base import NodeRegistry
from app.workflow.nodes.start import InputError, StartConfig, coerce_inputs

log = structlog.get_logger("app.services.wf_conversations")

STOP_WAIT_SEC = 10.0
ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)
_TRIGGERS = {
    WfConversationSource.PREVIEW: WfRunTrigger.PREVIEW,
    WfConversationSource.WEBAPP: WfRunTrigger.WEBAPP,
    WfConversationSource.API: WfRunTrigger.API,
}


def conversation_out(chat: WfConversation) -> WfConversationOut:
    return WfConversationOut(
        id=chat.id,
        workflow_id=chat.workflow_id,
        wf_version_id=chat.wf_version_id,
        source=chat.source,  # pyright: ignore[reportArgumentType]
        title=chat.title,
        status=chat.status,
        external_user=chat.external_user,
        api_key_id=chat.api_key_id,
        message_count=chat.message_count,
        last_message_at=chat.last_message_at,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    )


def _title(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:TITLE_CHARS] or "새 대화"


def _file_ids(raw: Sequence[object]) -> list[uuid.UUID]:
    found: list[uuid.UUID] = []
    for item in raw:
        try:
            found.append(uuid.UUID(str(item)))
        except ValueError:
            continue
    return found


def wrong_kind() -> ServiceError:
    return ServiceError(409, "wrong_kind", "대화는 챗플로우에만 있다. 워크플로우는 런으로 실행한다")


@dataclass
class StartedChat:
    run_id: uuid.UUID
    conversation_id: uuid.UUID
    # 이 턴의 assistant 메시지(wf_run.message_id).
    message_id: uuid.UUID
    subscription: Subscription | None


class WfConversationService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        bus: EventBus,
        supervisor: Supervisor,
        storage: StorageBackend,
        runs: WorkflowRunService | None = None,
        registry: NodeRegistry | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._bus = bus
        # 워크플로우 레인.
        self._supervisor = supervisor
        self._storage = storage
        # 대화를 지울 때 돌던 런을 멈춘다.
        self._runs = runs
        self._registry = registry
        self._clock = clock

    def ensure_accepting(self) -> None:
        if self._supervisor.shutting_down or self._bus.closed:
            raise ServiceError(503, "shutting_down", "서버가 종료 중이라 새 런을 받지 않는다")

    async def _chatflow(
        self, session: AsyncSession, workspace_id: uuid.UUID, workflow_id: uuid.UUID
    ) -> Workflow:
        workflow = await WorkflowRepository(session, workspace_id).get_active(workflow_id)
        if workflow is None:
            raise not_found("워크플로우")
        if workflow.kind != WorkflowKind.CHATFLOW:
            raise wrong_kind()
        return workflow

    async def create_preview(
        self, workspace_id: uuid.UUID, workflow_id: uuid.UUID, body: WfConversationCreate
    ) -> WfConversationOut:
        """미리보기 대화. 초안으로 돌고, 초안이 바뀌면 다음 메시지부터 반영된다."""
        async with self._db.write() as session:
            workflow = await self._chatflow(session, workspace_id, workflow_id)
            draft = await WfVersionRepository(session, workspace_id).get_draft(workflow.id)
            if draft is None:
                raise not_found("워크플로우 초안")
            out = await self._create(
                session,
                workspace_id,
                workflow,
                draft.id,
                source=WfConversationSource.PREVIEW,
                title=body.title,
            )
        log.info("wf_conversation.created", conversation_id=str(out.id), source="preview")
        return out

    async def create_published(
        self,
        workspace_id: uuid.UUID,
        workflow_id: uuid.UUID,
        *,
        source: WfConversationSource,
        title: str | None = None,
        api_key_id: uuid.UUID | None = None,
        external_user: str | None = None,
    ) -> WfConversationOut:
        """웹앱·서비스 API 대화. 지금의 발행 버전을 쓰고 다시 발행해도 이 대화는 그 버전이다."""
        async with self._db.write() as session:
            workflow = await self._chatflow(session, workspace_id, workflow_id)
            if workflow.published_version_id is None:
                raise ServiceError(
                    409, "workflow_not_published", "발행한 버전이 없다. 챗플로우를 먼저 발행한다"
                )
            out = await self._create(
                session,
                workspace_id,
                workflow,
                workflow.published_version_id,
                source=source,
                title=title,
                api_key_id=api_key_id,
                external_user=external_user,
            )
        log.info("wf_conversation.created", conversation_id=str(out.id), source=str(source))
        return out

    async def _create(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        workflow: Workflow,
        version_id: uuid.UUID,
        *,
        source: WfConversationSource,
        title: str | None,
        api_key_id: uuid.UUID | None = None,
        external_user: str | None = None,
    ) -> WfConversationOut:
        conversations = WfConversationRepository(session, workspace_id)
        chat = conversations.add(
            WfConversation(
                id=new_id(),
                workflow_id=workflow.id,
                wf_version_id=version_id,
                source=source,
                api_key_id=api_key_id,
                external_user=external_user,
                title=title,
                status=SessionStatus.ACTIVE,
                conversation_vars={},
                message_count=0,
            )
        )
        await conversations.flush()
        return conversation_out(chat)

    async def check(
        self, workspace_id: uuid.UUID, conversation_id: uuid.UUID, scope: FlowScope | None
    ) -> WfConversationOut:
        """범위 안의 살아 있는 대화. 아니면 404."""
        async with self._db.read() as session:
            chat = await scoped_conversation(session, workspace_id, conversation_id, scope)
            return conversation_out(chat)

    async def list_conversations(
        self,
        workspace_id: uuid.UUID,
        *,
        workflow_id: uuid.UUID | None = None,
        source: str | None = None,
        api_key_id: uuid.UUID | None = None,
        external_user: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        non_empty: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> WfConversationList:
        async with self._db.read() as session:
            rows = await WfConversationRepository(session, workspace_id).list_filtered(
                workflow_id=workflow_id,
                source=source,
                api_key_id=api_key_id,
                external_user=external_user,
                since=since,
                until=until,
                non_empty=non_empty,
                limit=limit + 1,
                offset=offset,
            )
        more = len(rows) > limit
        return WfConversationList(
            items=[conversation_out(row) for row in rows[:limit]],
            next_offset=offset + limit if more else None,
        )

    async def get(
        self, workspace_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> WfConversationDetail:
        async with self._db.read() as session:
            chat = await scoped_conversation(session, workspace_id, conversation_id, None)
            runs = WfRunRepository(session, workspace_id)
            active = await runs.active_for_conversation(chat.id)
            latest = await runs.latest_for_conversation(chat.id)
            return WfConversationDetail(
                **conversation_out(chat).model_dump(),
                active_run=wf_run_out(active) if active is not None else None,
                last_run=wf_run_out(latest) if latest is not None else None,
            )

    async def update(
        self, workspace_id: uuid.UUID, conversation_id: uuid.UUID, patch: WfConversationPatch
    ) -> WfConversationOut:
        async with self._db.write() as session:
            chat = await scoped_conversation(session, workspace_id, conversation_id, None)
            chat.title = patch.title
            chat.updated_at = self._clock()
            return conversation_out(chat)

    async def delete(self, workspace_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        """돌던 런을 멈추고 지운다. 행·메시지·런은 로그로 남고 붙인 업로드는 지운다(4.4)."""
        async with self._db.read() as session:
            chat = await scoped_conversation(session, workspace_id, conversation_id, None)
            active = await WfRunRepository(session, workspace_id).active_for_conversation(chat.id)
        if active is not None and self._runs is not None:
            try:
                await self._runs.stop(workspace_id, active.id)
            except ServiceError as exc:
                if exc.code != "run_not_active":
                    raise
            await self._wait_stopped(workspace_id, active.id)
        async with self._db.write() as session:
            chat = await scoped_conversation(session, workspace_id, conversation_id, None)
            chat.deleted_at = self._clock()
            files = WfFileRepository(session, workspace_id)
            uploads = await files.uploads_for_conversations([chat.id])
            await files.delete_many([row.id for row in uploads])
        # 행을 먼저 지운다. 파일 삭제가 실패해도 가리키는 행 없는 파일만 남는다.
        for row in uploads:
            try:
                await self._storage.delete(row.storage_key)
            except (OSError, StorageError) as exc:
                log.warning(
                    "wf_conversation.file_delete_failed", file_id=str(row.id), error=str(exc)
                )
        log.info("wf_conversation.deleted", conversation_id=str(conversation_id))

    async def _wait_stopped(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_WAIT_SEC
        while loop.time() < deadline:
            async with self._db.read() as session:
                run = await WfRunRepository(session, workspace_id).get(run_id)
            if run is None or run.status not in ACTIVE_STATUSES:
                return
            await asyncio.sleep(0.1)
        raise ServiceError(409, "run_still_running", "런이 아직 멈추지 않았다. 잠시 뒤 다시 지운다")

    async def list_messages(
        self,
        workspace_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        before_seq: int | None,
        limit: int,
    ) -> WfMessageList:
        async with self._db.read() as session:
            chat = await scoped_conversation(session, workspace_id, conversation_id, None)
            rows = await WfMessageRepository(session, workspace_id).list_page(
                chat.id, before_seq=before_seq, limit=limit + 1
            )
            more = len(rows) > limit
            rows = rows[-limit:] if more else rows
            ids = {file_id for row in rows for file_id in _file_ids(row.files or [])}
            files = await WfFileRepository(session, workspace_id).get_many(ids)
        by_id = {row.id: row for row in files}
        return WfMessageList(
            items=[
                WfMessageOut(
                    id=row.id,
                    conversation_id=row.conversation_id,
                    run_id=row.wf_run_id,
                    seq=row.seq,
                    role=row.role,  # pyright: ignore[reportArgumentType]
                    text=row.text,
                    inputs=row.inputs,
                    files=[
                        wf_file_out(by_id[file_id])
                        for file_id in _file_ids(row.files or [])
                        if file_id in by_id
                    ],
                    usage=row.usage,
                    status=row.status,  # pyright: ignore[reportArgumentType]
                    created_at=row.created_at,
                )
                for row in rows
            ],
            has_more=more,
        )

    async def variables(
        self, workspace_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> WfConversationVariables:
        async with self._db.read() as session:
            chat = await scoped_conversation(session, workspace_id, conversation_id, None)
            version = await WfVersionRepository(session, workspace_id).get(chat.wf_version_id)
        declared = conversation_vars(version.conversation_vars) if version is not None else []
        stored = dict(chat.conversation_vars or {})
        return WfConversationVariables(
            items=[
                ConversationVariableOut(
                    name=item.name, type=item.type, value=stored.get(item.name, item.default)
                )
                for item in declared
            ]
        )

    async def send_message(
        self,
        workspace_id: uuid.UUID,
        conversation_id: uuid.UUID,
        body: WfMessageCreate,
        *,
        subscribe: bool,
    ) -> StartedChat:
        """user·assistant 메시지와 런을 만들어 워크플로우 레인에 넣는다."""
        self.ensure_accepting()
        async with self._db.read() as session:
            chat = await scoped_conversation(session, workspace_id, conversation_id, None)
            workflow = await self._chatflow(session, workspace_id, chat.workflow_id)
            version = await WfVersionRepository(session, workspace_id).get(chat.wf_version_id)
            if version is None:
                raise not_found("워크플로우 버전")
            await self._ensure_idle(session, workspace_id, chat.id)
            previous = None
            if not body.inputs:
                previous = await WfMessageRepository(session, workspace_id).last_user(chat.id)
            draft = version.version_number is None
            agents = await live_agent_ids(session, workspace_id, version.graph) if draft else None
        invalid = "draft_invalid" if draft else "version_invalid"
        graph = parse_graph(version.graph)
        if graph is None:
            raise ServiceError(422, invalid, "저장된 그래프를 읽지 못했다")
        analysis = graph_check.analyze(
            graph,
            "chatflow",
            registry=self._registry,
            env_names=[item.name for item in stored_env(version.env_vars)],
            conversation_vars=conversation_types(
                workflow.kind, conversation_vars(version.conversation_vars)
            ),
            agent_ids=agents,
        )
        if not analysis.ok:
            raise ServiceError(
                422,
                invalid,
                "그래프에 검증 오류가 있다. 고쳐 저장(발행)한 뒤 다시 보낸다",
                details=issue_details(analysis.issues),
            )
        start_id = next(
            node.id for node in graph.nodes if node.type == "start" and node.parent_id is None
        )
        start = cast(StartConfig, analysis.configs[start_id])
        raw = body.inputs or (previous.inputs if previous is not None and previous.inputs else {})
        inputs = await WorkflowFileService(
            self._db, self._storage, clock=self._clock
        ).resolve_inputs(workspace_id, workflow.id, start.fields, raw)
        try:
            coerce_inputs(start.fields, inputs)
        except InputError as exc:
            raise ServiceError(
                422, "invalid_inputs", "런 입력이 올바르지 않다", details=exc.errors
            ) from None
        attached = await self._attachments(workspace_id, workflow.id, chat.id, body.files)
        limits = run_limits(self._settings, parse_settings(version.settings))
        now = self._clock()
        async with self._db.write() as session:
            chat = await scoped_conversation(session, workspace_id, conversation_id, None)
            await self._ensure_idle(session, workspace_id, chat.id)
            messages = WfMessageRepository(session, workspace_id)
            seq = await messages.next_seq(chat.id)
            run_id, question_id, answer_id = new_id(), new_id(), new_id()
            runs = WfRunRepository(session, workspace_id)
            run = runs.add(
                WfRun(
                    id=run_id,
                    workflow_id=workflow.id,
                    wf_version_id=version.id,
                    kind=WorkflowKind.CHATFLOW,
                    trigger=_TRIGGERS[WfConversationSource(chat.source)],
                    status=RunStatus.QUEUED,
                    api_key_id=chat.api_key_id,
                    external_user=chat.external_user,
                    conversation_id=chat.id,
                    message_id=answer_id,
                    # 재기동 뒤에도 같은 입력으로 시작해야 해서 가리지 않는다(워크플로우 런과 같다).
                    inputs=inputs,
                    usage=UsageTotal().model_dump(mode="json"),
                    node_run_count=0,
                    limits=limits,
                    graph_hash=version.graph_hash,
                    queued_at=now,
                )
            )
            # 메시지가 런을 외래 키로 가리키므로 런을 먼저 넣는다.
            await runs.flush()
            messages.add(
                WfMessage(
                    id=question_id,
                    conversation_id=chat.id,
                    wf_run_id=run_id,
                    seq=seq,
                    role=MessageRole.USER,
                    text=body.text,
                    inputs=inputs,
                    files=[str(file_id) for file_id in attached],
                )
            )
            messages.add(
                WfMessage(
                    id=answer_id,
                    conversation_id=chat.id,
                    wf_run_id=run_id,
                    seq=seq + 1,
                    role=MessageRole.ASSISTANT,
                    text="",
                    files=[],
                    status=RunStatus.QUEUED,
                )
            )
            await messages.flush()
            if attached:
                # 메시지에 붙인 업로드는 대화를 지울 때 함께 지운다(4.4).
                for row in await WfFileRepository(session, workspace_id).get_many(attached):
                    row.conversation_id = chat.id
            chat.message_count += 2
            chat.last_message_at = now
            if not chat.title:
                chat.title = _title(body.text)
            ref = wf_run_ref(run)
        try:
            subscription = await enqueue(self._bus, self._supervisor, ref, subscribe=subscribe)
        except SupervisorClosed:
            await fail_unstarted(self._db, self._bus, ref, self._clock())
            raise ServiceError(
                503, "shutting_down", "서버가 종료 중이라 새 런을 받지 않는다"
            ) from None
        log.info(
            "wf_conversation.message_sent",
            conversation_id=str(conversation_id),
            run_id=str(run_id),
            turn_seq=seq,
        )
        return StartedChat(
            run_id=run_id,
            conversation_id=conversation_id,
            message_id=answer_id,
            subscription=subscription,
        )

    async def _ensure_idle(
        self, session: AsyncSession, workspace_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> None:
        active = await WfRunRepository(session, workspace_id).active_for_conversation(
            conversation_id
        )
        if active is None:
            return
        pending = pending_of(active)
        raise ServiceError(
            409,
            "run_in_progress",
            "이 대화에서 진행 중인 런이 있다. 끝나거나 승인·답을 받은 뒤 다시 보낸다",
            details={
                "run_id": str(active.id),
                "status": active.status,
                # approval·question·form. 폼(human_input)이면 /input에 kind=form으로 답한다.
                "interaction": pending.kind if pending is not None else None,
            },
        )

    async def _attachments(
        self,
        workspace_id: uuid.UUID,
        workflow_id: uuid.UUID,
        conversation_id: uuid.UUID,
        file_ids: Sequence[uuid.UUID],
    ) -> list[uuid.UUID]:
        """이 워크플로우에 올렸고 다른 대화에 붙지 않은 업로드만 받는다."""
        wanted = list(dict.fromkeys(file_ids))
        if not wanted:
            return []
        async with self._db.read() as session:
            rows = await WfFileRepository(session, workspace_id).get_many(wanted)
        by_id = {row.id: row for row in rows}
        for file_id in wanted:
            row = by_id.get(file_id)
            if (
                row is None
                or row.workflow_id != workflow_id
                or row.kind != WfFileKind.UPLOAD
                or row.conversation_id not in (None, conversation_id)
            ):
                raise ServiceError(
                    422,
                    "file_not_found",
                    "이 워크플로우에 올린 파일이 아니다",
                    details={"file_id": str(file_id)},
                )
        return wanted

    async def files_conversation(
        self, workspace_id: uuid.UUID, file_ids: Sequence[uuid.UUID]
    ) -> uuid.UUID | None:
        """업로드들이 함께 붙은 대화. 붙은 대화가 없으면 None, 여러 대화에 걸치면 422다.

        서비스 API가 conversation_id 없이 files만 받았을 때 그 파일을 올린 대화를 이어 쓴다(1단계와
        같다).
        """
        if not file_ids:
            return None
        async with self._db.read() as session:
            rows = await WfFileRepository(session, workspace_id).get_many(list(file_ids))
        owners = {row.conversation_id for row in rows if row.conversation_id is not None}
        if len(owners) > 1:
            raise ServiceError(
                422,
                "file_not_found",
                "conversation_id 없이 보낸 files는 한 대화에 올린 파일이어야 한다",
                details={"file_ids": [str(item) for item in file_ids]},
            )
        return owners.pop() if owners else None

    async def wait_result(self, workspace_id: uuid.UUID, started: StartedChat) -> WfChatResult:
        """blocking 응답. workflow.completed(입력 대기 포함)까지 기다린다."""
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

    async def result(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> WfChatResult:
        async with self._db.read() as session:
            run = await WfRunRepository(session, workspace_id).get(run_id)
            if run is None or run.conversation_id is None:
                raise not_found("챗플로우 런")
            message = (
                await WfMessageRepository(session, workspace_id).get(run.message_id)
                if run.message_id is not None
                else None
            )
            return WfChatResult(
                conversation_id=run.conversation_id,
                message_id=run.message_id,
                run=wf_run_out(run),
                answer=message.text if message is not None else "",
            )
