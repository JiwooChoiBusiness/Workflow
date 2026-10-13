"""과업 실행 서비스(설계 4.10, 13.1). AgentTaskService의 구현이다.

과업 하나는 세션(source=task)의 런 하나다. ephemeral이면 새 세션을 만들고 끝나면 작업공간을
지우며(결과 파일은 storage에 남는다), reuse면 지난 과업 세션의 작업공간을 이어 쓴다. 채운 지시는
첫 사용자 메시지로, 입력 값과 파일은 작업공간 task/로 간다. 서비스 API POST /api/v1/agent-tasks와
2단계 엔진이 같은 구현을 쓴다. HTTP에서 온 과업은 scope로 키의 에이전트 범위를 함께 검사한다.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import context_text, user_message_blocks
from app.agent.interactions import InteractionReply
from app.agent.task_service import (
    AgentTaskResult,
    AgentTaskSpec,
    TaskSpecError,
    check_output_schema,
    file_reference,
    render_instruction,
    task_message,
)
from app.config import Settings
from app.db.engine import Database
from app.db.models import Agent, AgentVersion, ChatSession, Message, Run
from app.db.models.enums import (
    MessageRole,
    RunStatus,
    RunTrigger,
    SessionSource,
    SessionStatus,
)
from app.db.repositories.agent_versions import AgentVersionRepository
from app.db.repositories.agents import AgentRepository
from app.db.repositories.messages import MessageRepository
from app.db.repositories.run_events import RunEventRepository
from app.db.repositories.runs import RunRepository
from app.db.repositories.session_files import SessionFileRepository
from app.db.repositories.sessions import SessionRepository
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.schemas.agent_version import ModelConfig
from app.schemas.common import RunLimits
from app.schemas.events import RunEventOut, UsageTotal
from app.schemas.task import (
    TASK_FILES_DIR,
    AgentTaskOut,
    SessionFileRef,
    TaskFile,
    TaskRunInput,
)
from app.services.access import SessionScope, scoped_session
from app.services.errors import ServiceError, not_found
from app.services.runs import RunService, StartedRun, event_out, run_out, task_output
from app.streaming.event_bus import EventBus, RunRef

log = structlog.get_logger("app.services.tasks")

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)
TITLE_CHARS = 50
_WAIT_RETRY_SEC = 0.1


def merged_limits(base: Mapping[str, Any] | None, override: RunLimits | None) -> RunLimits:
    """버전의 limits에 과업이 준 필드만 덮어쓴다."""
    limits = RunLimits.model_validate(dict(base or {}))
    if override is None or not override.model_fields_set:
        return limits
    update = override.model_dump(include=set(override.model_fields_set))
    return RunLimits.model_validate({**limits.model_dump(), **update})


def _title(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:TITLE_CHARS] or "과업"


def _unique_path(filename: str, taken: set[str]) -> str:
    stem, dot, ext = filename.rpartition(".")
    if not dot or not stem:
        stem, ext = filename, ""
    candidate = filename
    number = 2
    while candidate.casefold() in taken:
        candidate = f"{stem} ({number}).{ext}" if ext else f"{stem} ({number})"
        number += 1
    taken.add(candidate.casefold())
    return f"{TASK_FILES_DIR}/{candidate}"


def _spec_error(exc: TaskSpecError) -> ServiceError:
    return ServiceError(422, exc.code, exc.message)


class TaskService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        runs: RunService,
        bus: EventBus,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._runs = runs
        self._bus = bus
        self._clock = clock

    async def create(
        self,
        spec: AgentTaskSpec,
        *,
        subscribe: bool,
        scope: SessionScope | None = None,
        api_key_id: uuid.UUID | None = None,
    ) -> StartedRun:
        """과업 런을 만들어 큐에 넣는다. scope가 있으면 파일·reuse 세션이 그 안에 있어야 한다.

        reuse에 session_id가 없으면 새 과업 세션을 만들고 작업공간을 남긴다. 다음 과업이 그
        session_id로 이어 쓴다.
        """
        self._runs.ensure_accepting()
        if spec.session_mode == "ephemeral" and spec.session_id is not None:
            raise ServiceError(
                422,
                "session_id_not_allowed",
                "ephemeral 과업은 새 세션에서 돈다. 세션을 이어 쓰려면 session_mode를 reuse로 준다",
            )
        reuse = spec.session_id is not None
        try:
            if spec.output_schema is not None:
                check_output_schema(spec.output_schema)
        except TaskSpecError as exc:
            raise _spec_error(exc) from None
        ws = spec.workspace_id
        async with self._db.read() as session:
            agent = await self._agent(session, ws, spec.agent_id)
            await self._version(session, ws, agent, spec.agent_version_id)
            files, context = await self._resolve_inputs(session, ws, spec, scope)
            if reuse:
                await self._reuse_session(session, ws, spec, agent.id, scope)
        try:
            # 템플릿이 무거워도 이벤트 루프와 DB 쓰기 잠금을 붙잡지 않게 스레드에서 채운다.
            rendered = await asyncio.to_thread(render_instruction, spec.instruction, context)
        except TaskSpecError as exc:
            raise _spec_error(exc) from None
        text = task_message(rendered, context, files)
        now = self._clock()
        async with self._db.write() as session:
            agent = await self._agent(session, ws, spec.agent_id)
            version = await self._version(session, ws, agent, spec.agent_version_id)
            if reuse:
                chat = await self._reuse_session(session, ws, spec, agent.id, scope)
            else:
                sessions = SessionRepository(session, ws)
                chat = sessions.add(
                    ChatSession(
                        id=new_id(),
                        agent_id=agent.id,
                        agent_version_id=version.id,
                        source=SessionSource.TASK,
                        api_key_id=api_key_id,
                        external_user=spec.external_user,
                        title=_title(rendered),
                        status=SessionStatus.ACTIVE,
                        meta={"session_mode": spec.session_mode},
                        message_count=0,
                    )
                )
                await sessions.flush()
            run_id = await self._add_run(
                session,
                chat,
                version,
                spec,
                rendered=rendered,
                text=text,
                inputs=context,
                files=files,
                now=now,
            )
        ref = RunRef(run_id=run_id, session_id=chat.id, workspace_id=ws)
        started = await self._runs.enqueue(ref, subscribe=subscribe)
        log.info(
            "task.created",
            run_id=str(run_id),
            session_id=str(chat.id),
            session_mode=spec.session_mode,
            output_schema=spec.output_schema is not None,
            files=len(files),
        )
        return started

    async def _agent(self, session: AsyncSession, ws: uuid.UUID, agent_id: uuid.UUID) -> Agent:
        agent = await AgentRepository(session, ws).get_active(agent_id)
        if agent is None:
            raise not_found("에이전트")
        return agent

    async def _version(
        self,
        session: AsyncSession,
        ws: uuid.UUID,
        agent: Agent,
        version_id: uuid.UUID | None,
    ) -> AgentVersion:
        versions = AgentVersionRepository(session, ws)
        if version_id is not None:
            version = await versions.get_for_agent(agent.id, version_id)
            if version is None:
                raise not_found("에이전트 버전")
            return version
        if agent.published_version_id is None:
            raise ServiceError(
                409, "agent_not_published", "발행한 버전이 없다. 에이전트를 먼저 발행한다"
            )
        version = await versions.get(agent.published_version_id)
        if version is None:
            raise not_found("에이전트 버전")
        return version

    async def _resolve_inputs(
        self,
        session: AsyncSession,
        ws: uuid.UUID,
        spec: AgentTaskSpec,
        scope: SessionScope | None,
    ) -> tuple[list[TaskFile], dict[str, Any]]:
        """files와 {"file_id": ...} 입력을 task/files/의 경로로 바꾼다."""
        try:
            referenced = {name: file_reference(value) for name, value in spec.inputs.items()}
        except TaskSpecError as exc:
            raise _spec_error(exc) from None
        wanted: list[uuid.UUID] = list(dict.fromkeys(spec.files))
        for file_id in referenced.values():
            if file_id is not None and file_id not in wanted:
                wanted.append(file_id)
        rows = await SessionFileRepository(session, ws).get_many(wanted)
        by_id = {row.id: row for row in rows}
        # 입력으로 받는 파일은 키의 에이전트 것이면 된다. 사용자(user) 구분은 대화에만 둔다.
        file_scope = (
            SessionScope(sources=scope.sources, agent_id=scope.agent_id)
            if scope is not None
            else None
        )
        taken: set[str] = set()
        files: dict[uuid.UUID, TaskFile] = {}
        for file_id in wanted:
            row = by_id.get(file_id)
            visible = row is not None and row.storage_key is not None
            if row is not None and visible and file_scope is not None:
                try:
                    await scoped_session(session, ws, row.session_id, file_scope)
                except ServiceError:
                    visible = False
            if row is None or not visible or row.storage_key is None:
                raise ServiceError(
                    422,
                    "file_not_found",
                    "내려받을 수 있는 세션 파일이 아니다",
                    details={"file_id": str(file_id)},
                )
            files[file_id] = TaskFile(
                session_file_id=row.id,
                path=_unique_path(row.filename, taken),
                filename=row.filename,
                mime=row.mime,
                size_bytes=row.size_bytes,
                storage_key=row.storage_key,
            )
        context: dict[str, Any] = {}
        for name, value in spec.inputs.items():
            file_id = referenced[name]
            context[name] = files[file_id].path if file_id is not None else value
        return list(files.values()), context

    async def _reuse_session(
        self,
        session: AsyncSession,
        ws: uuid.UUID,
        spec: AgentTaskSpec,
        agent_id: uuid.UUID,
        scope: SessionScope | None,
    ) -> ChatSession:
        if spec.session_id is None:
            raise ServiceError(422, "session_id_required", "reuse에는 session_id가 필요하다")
        task_scope = (
            scope.only({SessionSource.TASK})
            if scope is not None
            else SessionScope(sources=frozenset({SessionSource.TASK}), agent_id=agent_id)
        )
        chat = await scoped_session(session, ws, spec.session_id, task_scope)
        if chat.agent_id != agent_id:
            raise not_found("세션")
        active = await RunRepository(session, ws).active_for_session(chat.id)
        if active is not None:
            raise ServiceError(
                409,
                "run_in_progress",
                "이 과업 세션에서 진행 중인 런이 있다",
                details={"run_id": str(active.id), "status": active.status},
            )
        instance = await WorkspaceInstanceRepository(session, ws).get_for_session(chat.id)
        limit = self._settings.workspace_max_bytes
        if instance is not None and limit and instance.disk_bytes > limit:
            raise ServiceError(
                409,
                "workspace_disk_limit",
                f"작업공간이 디스크 한도({limit // 2**20}MB)를 넘었다",
                details={"disk_bytes": instance.disk_bytes, "limit_bytes": limit},
            )
        return chat

    async def _add_run(
        self,
        session: AsyncSession,
        chat: ChatSession,
        version: AgentVersion,
        spec: AgentTaskSpec,
        *,
        rendered: str,
        text: str,
        inputs: Mapping[str, Any],
        files: Sequence[TaskFile],
        now: datetime,
    ) -> uuid.UUID:
        ws = spec.workspace_id
        message_id = new_id()
        run_id = new_id()
        context = context_text(
            now=now.astimezone(), source=chat.source, external_user=chat.external_user
        )
        messages = MessageRepository(session, ws)
        messages.add(
            Message(
                id=message_id,
                session_id=chat.id,
                run_id=run_id,
                seq=await messages.next_seq(chat.id),
                role=MessageRole.USER,
                blocks=user_message_blocks(text, context=context),
                text=text,
                files=[],
            )
        )
        # run.user_message_id가 메시지를 가리키므로 메시지를 먼저 넣는다.
        await messages.flush()
        task = TaskRunInput(
            instruction=spec.instruction,
            inputs=dict(inputs),
            output_schema=spec.output_schema,
            files=list(files),
            session_mode=spec.session_mode,
            correlation=spec.correlation,
        )
        model = ModelConfig.model_validate(version.model or {})
        runs = RunRepository(session, ws)
        runs.add(
            Run(
                id=run_id,
                session_id=chat.id,
                agent_version_id=version.id,
                trigger=RunTrigger.TASK,
                status=RunStatus.QUEUED,
                user_message_id=message_id,
                model={"model_id": model.model_id},
                limits=merged_limits(version.limits, spec.limits).model_dump(mode="json"),
                approval_mode=spec.approval_mode or version.approval_mode,
                input=task.model_dump(mode="json"),
                usage=UsageTotal().model_dump(mode="json"),
                steps=0,
                queued_at=now,
            )
        )
        await runs.flush()
        chat.message_count += 1
        chat.last_message_at = now
        if not chat.title:
            chat.title = _title(rendered)
        return run_id

    async def start(self, spec: AgentTaskSpec) -> uuid.UUID:
        return (await self.create(spec, subscribe=False)).run_id

    async def events(
        self, run_id: uuid.UUID, after: int | None = None
    ) -> AsyncIterator[RunEventOut]:
        ws = await self._workspace_of(run_id)
        subscription = await self._runs.run_stream(ws, run_id, after=after or 0)
        try:
            async for event in subscription.events():
                yield event_out(event)
        finally:
            subscription.close()

    async def wait(self, run_id: uuid.UUID, timeout: float | None = None) -> AgentTaskResult:
        ws = await self._workspace_of(run_id)
        async with asyncio.timeout(timeout):
            view = await self.wait_view(ws, run_id)
        return AgentTaskResult.model_validate(view.model_dump())

    async def provide_input(self, run_id: uuid.UUID, payload: InteractionReply) -> None:
        ws = await self._workspace_of(run_id)
        await self._runs.provide_input(ws, run_id, payload, subscribe=False)

    async def cancel(self, run_id: uuid.UUID) -> None:
        ws = await self._workspace_of(run_id)
        try:
            await self._runs.stop(ws, run_id)
        except ServiceError as exc:
            if exc.code != "run_not_active":
                raise

    async def wait_started(self, workspace_id: uuid.UUID, started: StartedRun) -> AgentTaskOut:
        """create(subscribe=True)로 만든 과업을 끝까지(또는 입력 대기까지) 기다린다(blocking)."""
        subscription = started.subscription
        if subscription is None:
            return await self.wait_view(workspace_id, started.run_id)
        finished = False
        async for event in subscription.events():
            if event.type == "run.completed":
                finished = True
                break
        subscription.close()
        if not finished:
            raise ServiceError(503, "shutting_down", "서버가 종료되어 과업 결과를 기다리지 못했다")
        return await self.view(workspace_id, started.run_id)

    async def wait_view(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> AgentTaskOut:
        """끝나거나 입력을 기다릴 때까지 기다린 뒤의 상태.

        재개된 런은 앞 구간의 run.completed(waiting_input)가 기록에 남아 있으므로 이벤트만 보지 않고
        DB 상태로 끝났는지 확인한다(DB를 먼저 고친 뒤에 run.completed를 낸다).
        """
        while True:
            after, settled = await self._progress(workspace_id, run_id)
            if settled:
                return await self.view(workspace_id, run_id)
            subscription = await self._runs.run_stream(workspace_id, run_id, after=after)
            try:
                async for event in subscription.events():
                    if event.type != "run.completed":
                        continue
                    _, settled = await self._progress(workspace_id, run_id)
                    if settled:
                        return await self.view(workspace_id, run_id)
            finally:
                subscription.close()
            if self._bus.closed:
                raise ServiceError(
                    503, "shutting_down", "서버가 종료되어 과업 결과를 기다리지 못했다"
                )
            await asyncio.sleep(_WAIT_RETRY_SEC)

    async def _progress(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> tuple[int, bool]:
        async with self._db.read() as session:
            run = await RunRepository(session, workspace_id).get(run_id)
            if run is None:
                raise not_found("런")
            after = await RunEventRepository(session, workspace_id).max_seq(run_id)
        return after, run.status not in ACTIVE

    async def view(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> AgentTaskOut:
        async with self._db.read() as session:
            run = await RunRepository(session, workspace_id).get(run_id)
            if run is None or run.trigger != RunTrigger.TASK:
                raise not_found("과업")
            # 끝난 런은 run.output, 진행 중·입력 대기면 지금까지의 글과 사용량.
            output = run.output or await task_output(
                session, workspace_id, run, status=run.status, result=None
            )
            out = run_out(run)
        raw_files: list[Any] = output.get("files") or []
        result = output.get("result")
        return AgentTaskOut(
            run_id=out.id,
            session_id=out.session_id,
            status=out.status,
            result=result if isinstance(result, dict) else None,
            text=str(output.get("text") or ""),
            files=[SessionFileRef.model_validate(item) for item in raw_files],
            usage=out.usage,
            error=out.error,
            pending_interaction=out.pending_interaction,
        )

    async def _workspace_of(self, run_id: uuid.UUID) -> uuid.UUID:
        # 1단계는 워크스페이스가 하나다. 런 id만 받는 13.1 인터페이스라 워크스페이스를 찾아 둔다.
        async with self._db.read() as session:
            for workspace in await WorkspaceRepository(session).list_all():
                if await RunRepository(session, workspace.id).get(run_id) is not None:
                    return workspace.id
        raise not_found("런")
