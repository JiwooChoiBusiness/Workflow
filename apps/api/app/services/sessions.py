"""세션·메시지·세션 파일·작업공간 관리(설계 3.3, 3.14, 5.3, 6.2).

세션을 지우면 행은 deleted_at으로 남기고(로그용) 작업공간 폴더와 workspace_instance 행, 메시지 첨부
업로드는 지운다. 런이 만든 결과 파일(storage의 session-files)은 남긴다(5.3, 5.4).
"""

import asyncio
import uuid
from collections.abc import AsyncIterable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import structlog

from app.agent.interactions import SESSION_AUTO_APPROVE
from app.config import Settings
from app.db.engine import Database
from app.db.models import ChatSession, Message, SessionFile, WorkspaceInstance
from app.db.models.enums import (
    RunStatus,
    SessionFileKind,
    SessionSource,
    SessionStatus,
    WorkspaceInstanceStatus,
)
from app.db.repositories.agents import AgentRepository
from app.db.repositories.messages import MessageRepository
from app.db.repositories.runs import RunRepository
from app.db.repositories.session_files import SessionFileRepository
from app.db.repositories.sessions import SessionRepository
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.db.types import new_id, utcnow
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.sandbox.disk import SCAN_BUDGET_SEC, Scanner, largest_dirs
from app.sandbox.local_workspace import remove_workspace_dir
from app.sandbox.provisioning import SnapshotSource, WorkspaceProvisioner
from app.schemas.events import MessageFileData
from app.schemas.run import SessionDetail
from app.schemas.session import (
    MessageList,
    MessageOut,
    SessionCreate,
    SessionFileList,
    SessionFileOut,
    SessionList,
    SessionOut,
    SessionPatch,
    WorkspaceDirUsage,
    WorkspaceInstanceOut,
    WorkspaceKillOut,
    WorkspaceList,
    WorkspaceStatusOut,
)
from app.services.access import SessionScope, scoped_session
from app.services.agents import AgentService, provision_content, snapshot_source
from app.services.errors import ServiceError, not_found
from app.services.files import guess_mime, sanitize_filename
from app.services.runs import RunService, run_out, session_file_out, workspace_handle
from app.storage.base import StorageBackend, StorageLimitExceeded, upload_key

log = structlog.get_logger("app.services.sessions")

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
STOP_WAIT_SEC = 10.0
PACKAGE_DIRS = (".packages", ".cache")


def session_out(chat: ChatSession) -> SessionOut:
    return SessionOut(
        id=chat.id,
        agent_id=chat.agent_id,
        agent_version_id=chat.agent_version_id,
        source=chat.source,  # pyright: ignore[reportArgumentType]
        title=chat.title,
        status=chat.status,
        approval_override="off" if chat.approval_override == SESSION_AUTO_APPROVE else None,
        external_user=chat.external_user,
        api_key_id=chat.api_key_id,
        workspace_instance_id=chat.workspace_instance_id,
        message_count=chat.message_count,
        last_message_at=chat.last_message_at,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    )


def instance_out(row: WorkspaceInstance, chat: ChatSession | None = None) -> WorkspaceInstanceOut:
    return WorkspaceInstanceOut(
        id=row.id,
        session_id=row.session_id,
        backend=row.backend,
        root_path=row.root_path,
        status=row.status,  # pyright: ignore[reportArgumentType]
        disk_bytes=row.disk_bytes,
        disk_estimated=row.disk_estimated,
        disk_measured_at=row.disk_measured_at,
        last_activity_at=row.last_activity_at,
        created_at=row.created_at,
        session_title=chat.title if chat is not None else None,
        agent_id=chat.agent_id if chat is not None else None,
        session_source=chat.source if chat is not None else None,  # pyright: ignore[reportArgumentType]
    )


def message_out(row: Message, files: dict[uuid.UUID, SessionFile]) -> MessageOut:
    refs: list[MessageFileData] = []
    for raw in row.files or []:
        try:
            item = files.get(uuid.UUID(str(raw)))
        except ValueError:
            continue
        if item is not None:
            refs.append(
                MessageFileData(
                    session_file_id=item.id,
                    path=item.path,
                    size=item.size_bytes,
                    downloadable=item.storage_key is not None,
                    filename=item.filename,
                    mime=item.mime,
                )
            )
    return MessageOut(
        id=row.id,
        session_id=row.session_id,
        run_id=row.run_id,
        seq=row.seq,
        role=row.role,  # pyright: ignore[reportArgumentType]
        blocks=row.blocks or [],
        text=row.text,
        stop_reason=row.stop_reason,
        files=refs,
        usage=row.usage,
        created_at=row.created_at,
    )


def _scan_largest(root: Path) -> list[tuple[str, int]]:
    return largest_dirs(Scanner(root, budget_sec=SCAN_BUDGET_SEC), top=10, depth=2)


class SessionService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        storage: StorageBackend,
        backend: SandboxBackend,
        runs: RunService,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._storage = storage
        self._backend = backend
        self._runs = runs
        self._clock = clock

    async def _active(self, workspace_id: uuid.UUID, session_id: uuid.UUID) -> ChatSession:
        async with self._db.read() as session:
            chat = await SessionRepository(session, workspace_id).get_active(session_id)
        if chat is None:
            raise not_found("세션")
        return chat

    async def create_preview(
        self, workspace_id: uuid.UUID, agent_id: uuid.UUID, body: SessionCreate
    ) -> SessionOut:
        """빌더 미리보기 세션. 에이전트의 초안을 쓴다(초안이 바뀌면 다음 런부터 반영된다)."""
        async with self._db.write() as session:
            agent = await AgentRepository(session, workspace_id).get_active(agent_id)
            if agent is None:
                raise not_found("에이전트")
            if agent.draft_version_id is None:
                raise ServiceError(409, "draft_missing", "에이전트에 초안이 없다")
            sessions = SessionRepository(session, workspace_id)
            chat = sessions.add(
                ChatSession(
                    id=new_id(),
                    agent_id=agent.id,
                    agent_version_id=agent.draft_version_id,
                    source=SessionSource.PREVIEW,
                    title=body.title,
                    status=SessionStatus.ACTIVE,
                    meta={},
                    message_count=0,
                )
            )
            await sessions.flush()
            out = session_out(chat)
        log.info(
            "session.created", session_id=str(out.id), agent_id=str(agent_id), source="preview"
        )
        return out

    async def create_published(
        self,
        workspace_id: uuid.UUID,
        agent_id: uuid.UUID,
        *,
        source: Literal["webapp", "api", "task"],
        title: str | None = None,
        api_key_id: uuid.UUID | None = None,
        external_user: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> SessionOut:
        """웹앱·서비스 API 세션(6.3, 6.4). 만들 때의 발행 버전을 쓴다.

        다시 발행해도 이미 만든 세션은 그 버전으로 이어 간다. 작업공간은 첫 런에서 그 버전의
        스냅샷으로 시작한다.
        """
        async with self._db.write() as session:
            agent = await AgentRepository(session, workspace_id).get_active(agent_id)
            if source == SessionSource.WEBAPP and (
                agent is None or not agent.webapp_enabled or agent.published_version_id is None
            ):
                raise ServiceError(404, "app_not_found", "웹앱으로 공개한 에이전트가 아니다")
            if agent is None:
                raise not_found("에이전트")
            if agent.published_version_id is None:
                raise ServiceError(
                    409, "agent_not_published", "발행한 버전이 없다. 에이전트를 먼저 발행한다"
                )
            sessions = SessionRepository(session, workspace_id)
            chat = sessions.add(
                ChatSession(
                    id=new_id(),
                    agent_id=agent.id,
                    agent_version_id=agent.published_version_id,
                    source=source,
                    api_key_id=api_key_id,
                    external_user=external_user,
                    title=title,
                    status=SessionStatus.ACTIVE,
                    meta=meta or {},
                    message_count=0,
                )
            )
            await sessions.flush()
            out = session_out(chat)
        log.info("session.created", session_id=str(out.id), agent_id=str(agent_id), source=source)
        return out

    async def check_session(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID, scope: SessionScope
    ) -> SessionOut:
        """범위(웹앱·서비스 API) 안의 살아 있는 세션. 아니면 404."""
        async with self._db.read() as session:
            return session_out(await scoped_session(session, workspace_id, session_id, scope))

    async def upload_session(
        self, workspace_id: uuid.UUID, file_ids: list[uuid.UUID], scope: SessionScope
    ) -> uuid.UUID:
        """첨부들이 함께 속한 대화. 서비스 API가 conversation_id 없이 files만 받았을 때 쓴다."""
        problem = ServiceError(
            422,
            "file_not_found",
            "conversation_id 없이 보낸 files는 한 대화에 올리고 아직 보내지 않은 첨부여야 한다",
            details={"file_ids": [str(item) for item in file_ids]},
        )
        async with self._db.read() as session:
            rows = await SessionFileRepository(session, workspace_id).get_many(file_ids)
            owners = {row.session_id for row in rows}
            if (
                len(rows) != len(set(file_ids))
                or len(owners) != 1
                or any(row.kind != SessionFileKind.UPLOAD or row.message_id for row in rows)
            ):
                raise problem
            try:
                chat = await scoped_session(session, workspace_id, owners.pop(), scope)
            except ServiceError:
                raise problem from None
        return chat.id

    async def scoped_file(
        self, workspace_id: uuid.UUID, file_id: uuid.UUID, scope: SessionScope
    ) -> SessionFile:
        """범위 안 세션의 내려받을 수 있는 파일(서비스 API의 /files/{id}/download)."""
        async with self._db.read() as session:
            row = await SessionFileRepository(session, workspace_id).get(file_id)
            if row is None:
                raise not_found("세션 파일")
            try:
                await scoped_session(session, workspace_id, row.session_id, scope)
            except ServiceError:
                raise not_found("세션 파일") from None
        if row.storage_key is None:
            raise ServiceError(
                404,
                "file_not_stored",
                "보존하지 않은 파일이라 내려받을 수 없다",
                details={"reason": row.reason},
            )
        return row

    async def list_sessions(
        self,
        workspace_id: uuid.UUID,
        *,
        agent_id: uuid.UUID | None = None,
        source: str | None = None,
        api_key_id: uuid.UUID | None = None,
        external_user: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        non_empty: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> SessionList:
        async with self._db.read() as session:
            rows = await SessionRepository(session, workspace_id).list_filtered(
                agent_id=agent_id,
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
        return SessionList(
            items=[session_out(row) for row in rows[:limit]],
            next_offset=offset + limit if more else None,
        )

    async def get_session(self, workspace_id: uuid.UUID, session_id: uuid.UUID) -> SessionDetail:
        async with self._db.read() as session:
            chat = await SessionRepository(session, workspace_id).get_active(session_id)
            if chat is None:
                raise not_found("세션")
            runs = RunRepository(session, workspace_id)
            active = await runs.active_for_session(session_id)
            latest = await runs.latest_for_session(session_id)
            instance = await WorkspaceInstanceRepository(session, workspace_id).get_for_session(
                session_id
            )
            return SessionDetail(
                **session_out(chat).model_dump(),
                active_run=run_out(active) if active is not None else None,
                last_run=run_out(latest) if latest is not None else None,
                workspace=instance_out(instance, chat) if instance is not None else None,
            )

    async def update_session(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID, patch: SessionPatch
    ) -> SessionOut:
        async with self._db.write() as session:
            chat = await SessionRepository(session, workspace_id).get_active(session_id)
            if chat is None:
                raise not_found("세션")
            if "title" in patch.model_fields_set and patch.title is not None:
                chat.title = patch.title
            if "approval_override" in patch.model_fields_set:
                # 자동 승인은 이 세션에서만 켠다. 끄면 에이전트 버전의 승인 모드로 돌아간다(3.10).
                chat.approval_override = patch.approval_override
            chat.updated_at = self._clock()
            return session_out(chat)

    async def delete_session(self, workspace_id: uuid.UUID, session_id: uuid.UUID) -> None:
        chat = await self._active(workspace_id, session_id)
        async with self._db.read() as session:
            active = await RunRepository(session, workspace_id).active_for_session(chat.id)
        if active is not None:
            try:
                await self._runs.stop(workspace_id, active.id)
            except ServiceError as exc:
                if exc.code != "run_not_active":
                    raise
            await self._wait_stopped(workspace_id, active.id)
        upload_keys: list[str] = []
        instance: WorkspaceInstance | None = None
        async with self._db.write() as session:
            chat_row = await SessionRepository(session, workspace_id).get_active(session_id)
            if chat_row is None:
                raise not_found("세션")
            chat_row.deleted_at = self._clock()
            chat_row.workspace_instance_id = None
            files = SessionFileRepository(session, workspace_id)
            for row in await files.list_for_session(session_id, kind=SessionFileKind.UPLOAD):
                if row.storage_key is not None:
                    upload_keys.append(row.storage_key)
            await files.delete_uploads(session_id)
            instances = WorkspaceInstanceRepository(session, workspace_id)
            instance = await instances.get_for_session(session_id)
            if instance is not None:
                await instances.delete(instance.id)
        try:
            await self._backend.release(self._handle(session_id, instance), delete=True)
        except SandboxError as exc:
            log.warning(
                "session.workspace_delete_failed", session_id=str(session_id), error=exc.message
            )
        for key in upload_keys:
            await self._storage.delete(key)
        log.info("session.deleted", session_id=str(session_id))

    async def _wait_stopped(self, workspace_id: uuid.UUID, run_id: uuid.UUID) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_WAIT_SEC
        while loop.time() < deadline:
            async with self._db.read() as session:
                run = await RunRepository(session, workspace_id).get(run_id)
            if run is None or run.status not in (RunStatus.RUNNING, RunStatus.QUEUED):
                return
            await asyncio.sleep(0.1)
        raise ServiceError(409, "run_still_running", "런이 아직 멈추지 않았다. 잠시 뒤 다시 지운다")

    async def list_messages(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID, *, before_seq: int | None, limit: int
    ) -> MessageList:
        async with self._db.read() as session:
            if await SessionRepository(session, workspace_id).get_active(session_id) is None:
                raise not_found("세션")
            rows = await MessageRepository(session, workspace_id).list_page(
                session_id, before_seq=before_seq, limit=limit + 1
            )
            more = len(rows) > limit
            rows = rows[-limit:] if more else rows
            file_ids: set[uuid.UUID] = set()
            for row in rows:
                for raw in row.files or []:
                    try:
                        file_ids.add(uuid.UUID(str(raw)))
                    except ValueError:
                        continue
            files = await SessionFileRepository(session, workspace_id).get_many(file_ids)
        by_id = {item.id: item for item in files}
        return MessageList(items=[message_out(row, by_id) for row in rows], has_more=more)

    async def list_files(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID, *, kind: str | None = None
    ) -> SessionFileList:
        async with self._db.read() as session:
            if await SessionRepository(session, workspace_id).get_active(session_id) is None:
                raise not_found("세션")
            rows = await SessionFileRepository(session, workspace_id).list_for_session(
                session_id, kind=kind
            )
        return SessionFileList(items=[session_file_out(row) for row in rows])

    async def file_for_download(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID, file_id: uuid.UUID
    ) -> SessionFile:
        async with self._db.read() as session:
            # 지운 세션의 결과 파일도 로그 화면에서 내려받을 수 있게 남긴다(5.3).
            row = await SessionFileRepository(session, workspace_id).get_for_session(
                session_id, file_id
            )
        if row is None:
            raise not_found("세션 파일")
        if row.storage_key is None:
            raise ServiceError(
                404,
                "file_not_stored",
                "보존하지 않은 파일이라 내려받을 수 없다",
                details={"reason": row.reason},
            )
        return row

    async def upload(
        self,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        filename: str | None,
        content_type: str | None,
        chunks: AsyncIterable[bytes],
    ) -> SessionFileOut:
        """메시지 첨부. 메시지를 보낼 때 file_ids로 넣으면 작업공간 uploads/<message_id>/로 간다."""
        await self._active(workspace_id, session_id)
        name = sanitize_filename(filename)
        file_id = new_id()
        key = upload_key(session_id, file_id)
        try:
            stored = await self._storage.write_stream(key, chunks, max_bytes=MAX_UPLOAD_BYTES)
        except StorageLimitExceeded:
            raise ServiceError(
                413,
                "file_too_large",
                f"첨부 파일은 {MAX_UPLOAD_BYTES // 2**20}MB 이하여야 한다",
            ) from None
        try:
            async with self._db.write() as session:
                if await SessionRepository(session, workspace_id).get_active(session_id) is None:
                    raise not_found("세션")
                files = SessionFileRepository(session, workspace_id)
                row = files.add(
                    SessionFile(
                        id=file_id,
                        session_id=session_id,
                        kind=SessionFileKind.UPLOAD,
                        # 메시지를 보낼 때 uploads/<message_id>/<filename>으로 바뀐다.
                        path=name,
                        filename=name,
                        mime=guess_mime(name, content_type),
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        storage_key=key,
                    )
                )
                await files.flush()
                out = session_file_out(row)
        except BaseException:
            await self._storage.delete(key)
            raise
        log.info("session.file_uploaded", session_id=str(session_id), file_id=str(file_id))
        return out

    def _handle(self, session_id: uuid.UUID, instance: WorkspaceInstance | None) -> WorkspaceHandle:
        return workspace_handle(self._backend, session_id, instance)

    async def _instance(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID
    ) -> tuple[ChatSession, WorkspaceInstance | None, str | None]:
        async with self._db.read() as session:
            chat = await SessionRepository(session, workspace_id).get_active(session_id)
            if chat is None:
                raise not_found("세션")
            instance = await WorkspaceInstanceRepository(session, workspace_id).get_for_session(
                session_id
            )
            active = await RunRepository(session, workspace_id).active_for_session(session_id)
        return chat, instance, active.status if active is not None else None

    async def workspace_status(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID
    ) -> WorkspaceStatusOut:
        chat, instance, _ = await self._instance(workspace_id, session_id)
        handle = self._handle(session_id, instance)
        exists = await asyncio.to_thread(handle.root.is_dir)
        status = WorkspaceStatusOut(
            session_id=session_id,
            instance=instance_out(instance, chat) if instance is not None else None,
            exists=exists,
            disk_limit_bytes=self._settings.workspace_max_bytes,
        )
        if not exists:
            return status
        try:
            stats = await self._backend.stats(handle)
        except SandboxError as exc:
            log.warning(
                "session.workspace_stats_failed", session_id=str(session_id), error=exc.message
            )
            return status
        status.disk_bytes = stats.disk_bytes
        status.disk_estimated = stats.disk_estimated
        status.running = stats.running
        status.last_exec_at = stats.last_exec_at
        status.largest_dirs = [
            WorkspaceDirUsage(path=path, bytes=size)
            for path, size in await asyncio.to_thread(_scan_largest, handle.root)
        ]
        return status

    async def kill_processes(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID, exec_id: str | None
    ) -> WorkspaceKillOut:
        """작업공간 패널의 "프로세스 종료"(3.14). 런은 멈추지 않고 그 도구 호출만 오류로 끝난다."""
        _, instance, _ = await self._instance(workspace_id, session_id)
        handle = self._handle(session_id, instance)
        targets: list[str]
        if exec_id is not None:
            targets = [exec_id]
        else:
            try:
                targets = [item.exec_id for item in (await self._backend.stats(handle)).running]
            except SandboxError:
                targets = []
        killed: list[str] = []
        for target in targets:
            try:
                if await self._backend.kill(handle, target):
                    killed.append(target)
            except SandboxError as exc:
                log.warning("session.kill_failed", exec_id=target, error=exc.message)
        return WorkspaceKillOut(killed=killed)

    async def clean_packages(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID
    ) -> WorkspaceStatusOut:
        """.packages·.cache를 지운다. 설치 이력(.platform/requirements.txt)은 남긴다."""
        _, instance, active = await self._instance(workspace_id, session_id)
        if active in (RunStatus.QUEUED, RunStatus.RUNNING):
            raise ServiceError(409, "run_in_progress", "런이 실행 중이라 패키지를 지울 수 없다")
        handle = self._handle(session_id, instance)
        for name in PACKAGE_DIRS:
            target = handle.root / name
            try:
                await asyncio.to_thread(remove_workspace_dir, target)
            except SandboxError as exc:
                raise ServiceError(409, exc.code, exc.message) from None
            await asyncio.to_thread(target.mkdir, exist_ok=True)
        await self._remeasure(workspace_id, session_id, handle)
        log.info("session.packages_cleaned", session_id=str(session_id))
        return await self.workspace_status(workspace_id, session_id)

    async def reset_workspace(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID, agents: AgentService
    ) -> WorkspaceStatusOut:
        """작업공간 초기화(3.3). 지우고 처음처럼 스냅샷·첨부·스킬을 다시 채운다. 메시지는 둔다."""
        chat, instance, active = await self._instance(workspace_id, session_id)
        if active in (RunStatus.QUEUED, RunStatus.RUNNING):
            raise ServiceError(
                409, "run_in_progress", "런이 실행 중이라 작업공간을 초기화할 수 없다"
            )
        runtime = await agents.load_runtime_version(workspace_id, chat.agent_version_id)
        snapshot: SnapshotSource | None = None
        if runtime.snapshot_id is not None:
            try:
                snapshot = await snapshot_source(
                    self._db, self._storage, workspace_id, runtime.snapshot_id
                )
            except ServiceError as exc:
                log.warning(
                    "session.reset_snapshot_missing",
                    session_id=str(session_id),
                    error=exc.message,
                )
        provisioner = WorkspaceProvisioner(self._backend)
        try:
            result = await provisioner.reset(
                self._handle(session_id, instance),
                snapshot=snapshot,
                content=provision_content(self._storage, runtime),
            )
        except SandboxError as exc:
            raise ServiceError(409, exc.code, exc.message) from None
        now = self._clock()
        async with self._db.write() as session:
            instances = WorkspaceInstanceRepository(session, workspace_id)
            row = await instances.get_for_session(session_id)
            if row is None:
                row = instances.add(
                    WorkspaceInstance(
                        id=new_id(),
                        session_id=session_id,
                        backend=result.handle.backend,
                        root_path=str(result.handle.root),
                        status=WorkspaceInstanceStatus.READY,
                        created_at=result.created_at or now,
                        last_activity_at=now,
                    )
                )
                await instances.flush()
            else:
                row.root_path = str(result.handle.root)
                row.status = WorkspaceInstanceStatus.READY
                row.created_at = result.created_at or now
                row.last_activity_at = now
            row.disk_bytes = 0
            row.disk_estimated = False
            row.disk_measured_at = None
            row.restored_from_snapshot_id = result.restored_snapshot_id
            chat_row = await SessionRepository(session, workspace_id).get(session_id)
            if chat_row is not None:
                chat_row.workspace_instance_id = row.id
        log.info("session.workspace_reset", session_id=str(session_id))
        return await self.workspace_status(workspace_id, session_id)

    async def _remeasure(
        self, workspace_id: uuid.UUID, session_id: uuid.UUID, handle: WorkspaceHandle
    ) -> None:
        try:
            stats = await self._backend.stats(handle)
        except SandboxError:
            return
        async with self._db.write() as session:
            row = await WorkspaceInstanceRepository(session, workspace_id).get_for_session(
                session_id
            )
            if row is not None:
                row.disk_bytes = stats.disk_bytes
                row.disk_estimated = stats.disk_estimated
                row.disk_measured_at = self._clock()

    async def list_workspaces(self, workspace_id: uuid.UUID) -> WorkspaceList:
        async with self._db.read() as session:
            rows = await WorkspaceInstanceRepository(session, workspace_id).list_all()
            chats = await SessionRepository(session, workspace_id).get_many(
                [row.session_id for row in rows]
            )
        by_id = {chat.id: chat for chat in chats}
        return WorkspaceList(items=[instance_out(row, by_id.get(row.session_id)) for row in rows])
