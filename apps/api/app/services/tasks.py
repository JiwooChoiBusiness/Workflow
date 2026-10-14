"""과업 실행 서비스(설계 4.10, 13.1). AgentTaskService의 구현이다.

과업 하나는 세션(source=task)의 런 하나다. ephemeral이면 새 세션을 만들고 끝나면 작업공간을
지우며(결과 파일은 storage에 남는다), reuse면 지난 과업 세션의 작업공간을 이어 쓴다. 채운 지시는
첫 사용자 메시지로, 입력 값과 파일은 작업공간 task/로 간다. 서비스 API POST /api/v1/agent-tasks와
2단계 엔진이 같은 구현을 쓴다. HTTP에서 온 과업은 scope로 키의 에이전트 범위를 함께 검사한다.

워크플로우 agent 노드는 파일을 FileRef(wf_file·session_file)로 준다. 참조 속 저장소 키는 노드
출력에서 온 값이라 믿지 않고 id로 이 워크스페이스의 행을 다시 찾는다. wf_file은 키 범위가 없는
엔진 호출(scope 없음)에서만 받는다.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

import structlog
from pydantic import ValidationError
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
from app.db.repositories.workflows import WfFileRepository
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.sandbox.disk import format_size
from app.schemas.agent_version import ModelConfig
from app.schemas.common import RunLimits
from app.schemas.events import TERMINAL_EVENT_TYPES, RunEventOut, UsageTotal
from app.schemas.task import (
    TASK_FILES_DIR,
    AgentTaskOut,
    SessionFileRef,
    TaskFile,
    TaskRunInput,
)
from app.services.access import SessionScope, scoped_session
from app.services.errors import ServiceError, not_found
from app.services.runs import RunService, StartedRun, event_out, pending_of, run_out, task_output
from app.streaming.event_bus import EventBus, RunRef
from app.workflow.variables import FileRef, is_file

log = structlog.get_logger("app.services.tasks")

ACTIVE = (RunStatus.QUEUED, RunStatus.RUNNING)
TITLE_CHARS = 50
_WAIT_RETRY_SEC = 0.1

FileSource = Literal["session_file", "wf_file"]
FileKey = tuple[FileSource, uuid.UUID]


@dataclass(frozen=True)
class ChildTaskOrigin:
    """다른 런이 만드는 자식 과업. 일반 과업과 다른 점만 담는다.

    call_agent의 자식(오케스트레이션 3.3, 7장)과 워크플로우 agent 노드의 자식(2단계 3.7)이다. 지시는
    이미 채운 글(모델이 쓴 task, 노드가 렌더한 instruction)이라 템플릿으로 다시 채우지 않는다
    ({{ }}가 들어 있어도 글이다).
    """

    # call_agent의 부모 런. 워크플로우 런이 부모면 None이고 wf_node_run.child_run_id가 가리킨다.
    parent_run_id: uuid.UUID | None
    title: str
    meta: dict[str, Any]
    api_key_id: uuid.UUID | None
    # 자식이 다시 부를 수 있는 호출 수 상한. 뿌리 런의 남은 수다(3.9). None이면 버전 설정 그대로.
    child_calls_cap: int | None


def _spec_file_key(item: uuid.UUID | FileRef) -> FileKey:
    if isinstance(item, FileRef):
        return item.source, _ref_id(item)
    return "session_file", item


def _ref_id(ref: FileRef) -> uuid.UUID:
    try:
        return uuid.UUID(ref.file_id)
    except ValueError:
        raise TaskSpecError("file_not_found", f"올바른 file_id가 아니다: {ref.file_id!r}") from None


def _input_file_keys(value: object) -> FileKey | list[FileKey] | None:
    """입력 값이 파일이면 그 키(배열이면 키 목록). {"file_id": id}는 session_file이다."""
    session_file = file_reference(value)
    if session_file is not None:
        return "session_file", session_file
    if is_file(value):
        return _spec_file_key(_file_ref(value))
    if isinstance(value, list) and value and all(is_file(item) for item in cast(list[Any], value)):
        return [_spec_file_key(_file_ref(item)) for item in cast(list[Any], value)]
    return None


def _file_ref(value: object) -> FileRef:
    try:
        return FileRef.model_validate(value)
    except ValidationError:
        raise TaskSpecError("file_not_found", "파일 참조가 아니다") from None


def _as_list(found: FileKey | list[FileKey] | None) -> list[FileKey]:
    if found is None:
        return []
    return found if isinstance(found, list) else [found]


def merged_limits(
    base: Mapping[str, Any] | None,
    override: RunLimits | None,
    *,
    child_calls_cap: int | None = None,
) -> RunLimits:
    """버전의 limits에 과업이 준 필드만 덮어쓴다.

    child_calls_cap이 있으면 max_child_calls를 그 값 이하로 내린다(트리 전체 호출 수, 3.9).
    """
    limits = RunLimits.model_validate(dict(base or {}))
    if override is not None and override.model_fields_set:
        update = override.model_dump(include=set(override.model_fields_set))
        limits = RunLimits.model_validate({**limits.model_dump(), **update})
    if child_calls_cap is not None and limits.max_child_calls > child_calls_cap:
        limits = limits.model_copy(update={"max_child_calls": max(child_calls_cap, 0)})
    return limits


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
        enqueue: bool = True,
        origin: ChildTaskOrigin | None = None,
    ) -> StartedRun:
        """과업 런을 만들어 큐에 넣는다. scope가 있으면 파일·reuse 세션이 그 안에 있어야 한다.

        reuse에 session_id가 없으면 새 과업 세션을 만들고 작업공간을 남긴다. 다음 과업이 그
        session_id로 이어 쓴다. enqueue=False면 행만 queued로 만들고 큐에는 넣지 않는다(call_agent의
        자식은 부모가 대기 상태를 저장한 뒤에 넣는다, 오케스트레이션 3.3).
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
        if origin is not None:
            rendered = spec.instruction
            if not rendered.strip():
                raise ServiceError(422, "instruction_invalid", "과업 지시가 비어 있다")
        else:
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
                        api_key_id=origin.api_key_id if origin is not None else api_key_id,
                        external_user=spec.external_user,
                        title=origin.title if origin is not None else _title(rendered),
                        status=SessionStatus.ACTIVE,
                        meta=(
                            dict(origin.meta)
                            if origin is not None
                            else {"session_mode": spec.session_mode}
                        ),
                        approval_override=spec.session_approval_override,
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
                origin=origin,
            )
        ref = RunRef(run_id=run_id, session_id=chat.id, workspace_id=ws)
        if enqueue:
            started = await self._runs.enqueue(ref, subscribe=subscribe)
        else:
            started = StartedRun(run_id=run_id, session_id=chat.id, subscription=None)
        log.info(
            "task.created",
            run_id=str(run_id),
            session_id=str(chat.id),
            session_mode=spec.session_mode,
            output_schema=spec.output_schema is not None,
            files=len(files),
            parent_run_id=(
                str(origin.parent_run_id)
                if origin is not None and origin.parent_run_id is not None
                else None
            ),
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
        """files와 파일 입력({"file_id": ...}, FileRef)을 task/files/의 경로로 바꾼다."""
        try:
            wanted: list[FileKey] = list(dict.fromkeys(_spec_file_key(item) for item in spec.files))
            referenced = {name: _input_file_keys(value) for name, value in spec.inputs.items()}
        except TaskSpecError as exc:
            raise _spec_error(exc) from None
        for found in referenced.values():
            for key in _as_list(found):
                if key not in wanted:
                    wanted.append(key)
        by_key = await self._file_rows(session, ws, wanted, scope)
        taken: set[str] = set()
        files: dict[FileKey, TaskFile] = {}
        for key in wanted:
            row = by_key.get(key)
            if row is None:
                raise ServiceError(
                    422,
                    "file_not_found",
                    "내려받을 수 있는 파일이 아니다",
                    details={"file_id": str(key[1])},
                )
            files[key] = row.model_copy(update={"path": _unique_path(row.filename, taken)})
        context: dict[str, Any] = {}
        for name, value in spec.inputs.items():
            found = referenced[name]
            if found is None:
                context[name] = value
            elif isinstance(found, list):
                context[name] = [files[key].path for key in found]
            else:
                context[name] = files[found].path
        return list(files.values()), context

    async def _file_rows(
        self,
        session: AsyncSession,
        ws: uuid.UUID,
        keys: Sequence[FileKey],
        scope: SessionScope | None,
    ) -> dict[FileKey, TaskFile]:
        """보이는 파일만 TaskFile(경로는 비움)로. 없거나 범위 밖이면 빠진다."""
        found: dict[FileKey, TaskFile] = {}
        session_ids = [file_id for source, file_id in keys if source == "session_file"]
        # 입력으로 받는 파일은 키의 에이전트 것이면 된다. 사용자(user) 구분은 대화에만 둔다.
        file_scope = (
            SessionScope(sources=scope.sources, agent_id=scope.agent_id)
            if scope is not None
            else None
        )
        for row in await SessionFileRepository(session, ws).get_many(session_ids):
            if row.storage_key is None:
                continue
            if file_scope is not None:
                try:
                    await scoped_session(session, ws, row.session_id, file_scope)
                except ServiceError:
                    continue
            found["session_file", row.id] = TaskFile(
                session_file_id=row.id,
                path="",
                filename=row.filename,
                mime=row.mime,
                size_bytes=row.size_bytes,
                storage_key=row.storage_key,
            )
        wf_ids = [file_id for source, file_id in keys if source == "wf_file"]
        if wf_ids and scope is None:
            # 워크플로우 파일은 키 범위가 없는 엔진 호출(agent 노드)만 넘긴다.
            for wf_row in await WfFileRepository(session, ws).get_many(wf_ids):
                found["wf_file", wf_row.id] = TaskFile(
                    wf_file_id=wf_row.id,
                    path="",
                    filename=wf_row.filename,
                    mime=wf_row.mime,
                    size_bytes=wf_row.size_bytes,
                    storage_key=wf_row.storage_key,
                )
        return found

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
                f"작업공간이 디스크 한도({format_size(limit)})를 넘었다",
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
        origin: ChildTaskOrigin | None = None,
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
                limits=merged_limits(
                    version.limits,
                    spec.limits,
                    child_calls_cap=origin.child_calls_cap if origin is not None else None,
                ).model_dump(mode="json"),
                approval_mode=spec.approval_mode or version.approval_mode,
                input=task.model_dump(mode="json"),
                usage=UsageTotal().model_dump(mode="json"),
                steps=0,
                queued_at=now,
                parent_run_id=origin.parent_run_id if origin is not None else None,
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
            if event.type in TERMINAL_EVENT_TYPES:
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
                    if event.type not in TERMINAL_EVENT_TYPES:
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
        # 하위 에이전트를 기다리는 런(waiting_input + agent_call)은 사람 입력을 기다리는 게 아니라
        # 도구를 실행하는 중이다. 끝난 것으로 보지 않아 wait()가 HITL로 돌아오지 않는다(3.2).
        pending = pending_of(run) if run.status == RunStatus.WAITING_INPUT else None
        waits_child = pending is not None and pending.kind == "agent_call"
        return after, run.status not in ACTIVE and not waits_child

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
