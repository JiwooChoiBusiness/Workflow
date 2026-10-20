import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app import sysinfo
from app.auth.session_store import SessionStore
from app.db.engine import Checkpoint, Database
from app.db.models.enums import RunStatus
from app.db.repositories.agent_files import AgentFileRepository
from app.db.repositories.agent_versions import AgentVersionRepository
from app.db.repositories.session_files import SessionFileRepository
from app.db.repositories.sessions import SessionRepository
from app.db.repositories.skill_versions import SkillVersionRepository
from app.db.repositories.skills import SkillRepository
from app.db.repositories.wf_conversations import WfConversationRepository, WfMessageRepository
from app.db.repositories.workflows import (
    WfFileRepository,
    WfNodeRunRepository,
    WfRunEventRepository,
    WfRunRepository,
)
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import utcnow
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.schemas.workspace import WorkspaceSettings
from app.services.workflow_files import collect_file_refs
from app.storage.base import StorageBackend, StorageError

log = structlog.get_logger("app.housekeeping")

MEMORY_CHECK_INTERVAL_SEC = 60.0
HOURLY_INTERVAL_SEC = 3600.0
# 참조하는 버전이 없는 에이전트 첨부·스킬 버전은 만든 지 이만큼 지나면 지운다(5.4). 올린 뒤 초안에
# 넣기 전까지 기다려 주는 기간이다.
UNREFERENCED_GRACE = timedelta(days=7)
UPLOADS_PREFIX = "uploads"
DISK_FIELDS = (*(name for name, _, _ in sysinfo.DISK_FOLDERS), "disk_estimated")
# 워크플로우 보존(2단계 4.4).
WF_STATE_KEEP = timedelta(days=7)
WF_SINGLE_RUN_KEEP = timedelta(days=7)
WF_OUTPUT_FILE_KEEP = timedelta(days=90)
# 런·대화를 가리키지 않는 업로드(올리고 쓰지 않은 파일)는 만든 지 이만큼 지나면 지운다.
WF_LOOSE_UPLOAD_KEEP = timedelta(days=7)
OPEN_RUN_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_INPUT)
# 실패 런의 작업 폴더는 로그를 확인할 수 있게 이만큼 남긴다.
WF_WORKSPACE_KEEP = timedelta(hours=1)


class RunJobs(Protocol):
    """런 정리(app/services/runs.py의 RunMaintenance)."""

    # 응답 없이 WAITING_INPUT_TTL_HOURS가 지난 런을 cancelled(code=input_timeout)로 닫는다(3.10).
    async def expire_waiting(self) -> int: ...

    # 보존 기간이 지난 런의 run_event와 tool-outputs 파일을 지운다(5.4).
    async def purge_events(self) -> int: ...

    # 이벤트를 지운 뒤에도 남은 tool-outputs/<run_id> 폴더를 지운다(5.4).
    async def purge_tool_outputs(self) -> int: ...

    # 서버 종료 중에 끝나 남은 ephemeral 과업 작업공간을 지운다(3.3).
    async def release_task_workspaces(self) -> int: ...

    # 이미 끝난 하위 에이전트를 아직 기다리는 부모를 다시 넣는다(오케스트레이션 3.8의 안전망).
    async def reconcile_children(self) -> int: ...


def _mb(size: int) -> float:
    return round(size / 2**20, 1)


async def _attempt[T](
    step: Callable[[], Awaitable[T]], failed_event: str, **context: Any
) -> T | None:
    try:
        return await step()
    except Exception:
        log.exception(failed_event, **context)
        return None


def _uuid_dirs(root: Path) -> set[uuid.UUID]:
    # 이름이 uuid인 폴더만 세션 것으로 본다. .deleted-* 같은 다른 이름은 건드리지 않는다.
    found: set[uuid.UUID] = set()
    if not root.is_dir():
        return found
    for path in root.iterdir():
        try:
            session_id = uuid.UUID(path.name)
        except ValueError:
            continue
        if path.is_dir() and str(session_id) == path.name:
            found.add(session_id)
    return found


def _file_names(folder: Path) -> list[str]:
    try:
        return [path.name for path in folder.iterdir() if path.is_file()]
    except FileNotFoundError:
        return []


class Retention:
    """보존 정리(5.4) 중 런 이벤트 밖의 것.

    참조 없는 에이전트 첨부·스킬 버전, 지운 세션에 남은 첨부, 살아 있는 세션이 없는 작업공간 폴더를
    지운다. DB 행을 먼저 지우고 파일은 그다음에 지운다. 파일 삭제가 실패하면 가리키는 행 없는 파일만
    남는다(쓰이지 않는다). 첨부 폴더(uploads/)는 매번 다시 훑어 그런 파일도 지운다.
    """

    def __init__(
        self,
        db: Database,
        *,
        storage: StorageBackend,
        backend: SandboxBackend,
        clock: Callable[[], datetime] = utcnow,
        grace: timedelta = UNREFERENCED_GRACE,
    ) -> None:
        self._db = db
        self._storage = storage
        self._backend = backend
        self._clock = clock
        self._grace = grace

    def jobs(self) -> list[tuple[str, Callable[[], Awaitable[int]]]]:
        return [
            ("agent_files", self.purge_agent_files),
            ("skill_versions", self.purge_skill_versions),
            ("session_uploads", self.purge_session_uploads),
            ("orphan_workspaces", self.purge_orphan_workspaces),
        ]

    async def _workspace_ids(self) -> list[uuid.UUID]:
        async with self._db.read() as session:
            return [row.id for row in await WorkspaceRepository(session).list_all()]

    async def _delete_keys(self, keys: Iterable[str]) -> None:
        for key in keys:
            try:
                await self._storage.delete(key)
            except (OSError, StorageError) as exc:
                log.warning("housekeeping.file_delete_failed", key=key, error=str(exc))

    async def purge_agent_files(self) -> int:
        cutoff = self._clock() - self._grace
        removed = 0
        for workspace_id in await self._workspace_ids():
            # 참조 확인과 삭제를 한 쓰기 트랜잭션에서 해서 그 사이 초안 저장이 끼지 못하게 한다.
            async with self._db.write() as session:
                referenced, _ = await AgentVersionRepository(session, workspace_id).referenced_ids()
                files = AgentFileRepository(session, workspace_id)
                stale = [
                    row
                    for row in await files.list_created_before(cutoff)
                    if str(row.id) not in referenced
                ]
                for row in stale:
                    await files.delete(row.id)
            await self._delete_keys(row.storage_key for row in stale)
            removed += len(stale)
        return removed

    async def purge_skill_versions(self) -> int:
        cutoff = self._clock() - self._grace
        removed = 0
        for workspace_id in await self._workspace_ids():
            async with self._db.write() as session:
                _, referenced = await AgentVersionRepository(session, workspace_id).referenced_ids()
                current = await SkillRepository(session, workspace_id).current_version_ids()
                versions = SkillVersionRepository(session, workspace_id)
                stale = [
                    row
                    for row in await versions.list_created_before(cutoff)
                    if str(row.id) not in referenced and row.id not in current
                ]
                await versions.delete_many([row.id for row in stale])
            await self._delete_keys(row.storage_key for row in stale)
            removed += len(stale)
        return removed

    async def purge_session_uploads(self) -> int:
        """지운 세션의 첨부 행과 파일. 행 없이 남은 첨부 폴더도 지운다."""
        workspace_ids = await self._workspace_ids()
        removed = 0
        for workspace_id in workspace_ids:
            async with self._db.write() as session:
                files = SessionFileRepository(session, workspace_id)
                rows = await files.list_uploads_of_deleted_sessions()
                await files.delete_many([row.id for row in rows])
            await self._delete_keys(row.storage_key for row in rows if row.storage_key)
            removed += len(rows)
        # 세션 삭제는 행을 먼저 지우고 파일을 지우므로 파일 삭제가 실패하면(백신이 잡고 있는 등)
        # 행 없는 파일이 남는다.
        try:
            root = self._storage.local_path(UPLOADS_PREFIX)
        except StorageError:
            return removed
        folders = await asyncio.to_thread(_uuid_dirs, root)
        live = await self._live_sessions(workspace_ids, folders)
        for session_id in sorted(folders - live):
            folder = root / str(session_id)
            names = await asyncio.to_thread(_file_names, folder)
            await self._delete_keys(f"{UPLOADS_PREFIX}/{session_id}/{name}" for name in names)
            removed += len(names)
            with contextlib.suppress(OSError):
                await asyncio.to_thread(folder.rmdir)
        return removed

    async def purge_orphan_workspaces(self) -> int:
        """지운 세션에 남은 작업공간 행, 살아 있는 세션이 없는 작업공간 폴더(3.3)."""
        workspace_ids = await self._workspace_ids()
        for workspace_id in workspace_ids:
            async with self._db.write() as session:
                instances = WorkspaceInstanceRepository(session, workspace_id)
                for row in await instances.list_for_deleted_sessions():
                    await instances.delete(row.id)
        workspaces = getattr(self._backend, "workspaces_dir", None)
        if not isinstance(workspaces, Path):
            return 0
        folders = await asyncio.to_thread(_uuid_dirs, workspaces)
        live = await self._live_sessions(workspace_ids, folders)
        root = await asyncio.to_thread(workspaces.resolve)
        removed = 0
        for session_id in sorted(folders - live):
            handle = WorkspaceHandle(
                session_id=session_id, root=root / str(session_id), backend="local"
            )
            try:
                # 잠겨서 못 지우면 .deleted-*로 이름을 바꿔 두고 purge_deleted가 다시 지운다.
                await self._backend.release(handle, delete=True)
            except SandboxError as exc:
                log.warning(
                    "housekeeping.workspace_delete_failed",
                    session_id=str(session_id),
                    error=exc.message,
                )
                continue
            removed += 1
        return removed

    async def _live_sessions(
        self, workspace_ids: Collection[uuid.UUID], candidates: Collection[uuid.UUID]
    ) -> set[uuid.UUID]:
        live: set[uuid.UUID] = set()
        if not candidates:
            return live
        for workspace_id in workspace_ids:
            async with self._db.read() as session:
                rows = await SessionRepository(session, workspace_id).get_many(candidates)
            live.update(row.id for row in rows if row.deleted_at is None)
        return live


class FormTimeouts(Protocol):
    """폼 대기 만료(app/services/workflow_runs.py의 WorkflowParents)."""

    # 응답 없이 WAITING_INPUT_TTL_HOURS가 지난 human_input 폼 대기 런을 timeout 응답으로
    # 다시 넣는다.
    async def expire_forms(self) -> int: ...


class WorkflowRetention:
    """워크플로우 보존 정리(2단계 4.4).

    보존 기간이 지난 런의 이벤트(iteration 하위 노드 런의 입출력도 비운다), 실패 런의 체크포인트,
    단일 노드 실행 런, 출력 파일, 런 작업 폴더(DATA_DIR/wf-workspaces/<wf_run_id>), 지운 챗플로우
    대화에 붙어 남은 업로드, 런·대화를 가리키지 않는 오래된 업로드를 지운다. 대기 중인 런의
    체크포인트는 재개에 필요해 건드리지 않는다. 대기는 자식 런의 입력 대기 상한에서 끝나고, 자식이
    없는 폼 대기(human_input)는 forms가 같은 상한으로 끝낸다. 대화·메시지 행은 로그로 남긴다.
    """

    def __init__(
        self,
        db: Database,
        *,
        storage: StorageBackend,
        backend: SandboxBackend,
        retention_days: int,
        forms: FormTimeouts | None = None,
        clock: Callable[[], datetime] = utcnow,
        batch_size: int = 200,
    ) -> None:
        self._db = db
        self._storage = storage
        # DATA_DIR/wf-workspaces의 두 번째 로컬 백엔드.
        self._backend = backend
        self._retention_days = retention_days
        self._forms = forms
        self._clock = clock
        self._batch = batch_size

    def jobs(self) -> list[tuple[str, Callable[[], Awaitable[int]]]]:
        jobs: list[tuple[str, Callable[[], Awaitable[int]]]] = [
            ("wf_run_events", self.purge_events),
            ("wf_states", self.clear_states),
            ("wf_single_runs", self.purge_single_runs),
            ("wf_output_files", self.purge_output_files),
            ("wf_workspaces", self.purge_workspaces),
            ("wf_conversation_uploads", self.purge_conversation_uploads),
            ("wf_loose_uploads", self.purge_loose_uploads),
        ]
        if self._forms is not None:
            jobs.append(("wf_form_timeouts", self._forms.expire_forms))
        return jobs

    async def _workspaces(self) -> list[tuple[uuid.UUID, int]]:
        async with self._db.read() as session:
            rows = await WorkspaceRepository(session).list_all()
        found: list[tuple[uuid.UUID, int]] = []
        for row in rows:
            try:
                days = WorkspaceSettings.model_validate(row.settings or {}).run_event_retention_days
            except ValueError:
                days = self._retention_days
            found.append((row.id, days))
        return found

    async def _delete_keys(self, keys: Iterable[str]) -> None:
        for key in keys:
            try:
                await self._storage.delete(key)
            except (OSError, StorageError) as exc:
                log.warning("housekeeping.file_delete_failed", key=key, error=str(exc))

    async def purge_events(self) -> int:
        purged = 0
        for workspace_id, days in await self._workspaces():
            cutoff = self._clock() - timedelta(days=days)
            while True:
                async with self._db.write() as session:
                    events = WfRunEventRepository(session, workspace_id)
                    run_ids = await events.runs_ended_before(cutoff, limit=self._batch)
                    await events.delete_for_runs(run_ids)
                    # 요약 열은 남기고 반복마다 생긴 하위 노드 런의 입출력만 비운다(3.10).
                    await WfNodeRunRepository(session, workspace_id).clear_iteration_io(run_ids)
                purged += len(run_ids)
                if len(run_ids) < self._batch:
                    break
        return purged

    async def clear_states(self) -> int:
        cutoff = self._clock() - WF_STATE_KEEP
        cleared = 0
        for workspace_id, _ in await self._workspaces():
            while True:
                async with self._db.write() as session:
                    rows = await WfRunRepository(session, workspace_id).ended_before(
                        cutoff, statuses=[RunStatus.FAILED], with_state=True, limit=self._batch
                    )
                    for row in rows:
                        row.state = None
                cleared += len(rows)
                if len(rows) < self._batch:
                    break
        return cleared

    async def purge_single_runs(self) -> int:
        cutoff = self._clock() - WF_SINGLE_RUN_KEEP
        purged = 0
        for workspace_id, _ in await self._workspaces():
            while True:
                async with self._db.write() as session:
                    runs = WfRunRepository(session, workspace_id)
                    run_ids = await runs.single_node_ended_before(cutoff, limit=self._batch)
                    files = WfFileRepository(session, workspace_id)
                    rows = await files.list_for_runs(run_ids)
                    await files.delete_many([row.id for row in rows])
                    await WfRunEventRepository(session, workspace_id).delete_for_runs(run_ids)
                    await WfNodeRunRepository(session, workspace_id).delete_for_runs(run_ids)
                    await runs.delete_many(run_ids)
                await self._delete_keys(row.storage_key for row in rows)
                purged += len(run_ids)
                if len(run_ids) < self._batch:
                    break
        return purged

    async def purge_output_files(self) -> int:
        cutoff = self._clock() - WF_OUTPUT_FILE_KEEP
        removed = 0
        for workspace_id, _ in await self._workspaces():
            while True:
                async with self._db.write() as session:
                    files = WfFileRepository(session, workspace_id)
                    rows = await files.outputs_ended_before(cutoff, limit=self._batch)
                    await files.delete_many([row.id for row in rows])
                # 행을 먼저 지운다. 파일 삭제가 실패해도 가리키는 행 없는 파일만 남는다.
                await self._delete_keys(row.storage_key for row in rows)
                removed += len(rows)
                if len(rows) < self._batch:
                    break
        return removed

    async def purge_conversation_uploads(self) -> int:
        """지운 대화에 붙은 업로드(4.4). 대화를 지울 때 바로 지우고 실패해 남은 것만 여기서 본다."""
        removed = 0
        for workspace_id, _ in await self._workspaces():
            async with self._db.write() as session:
                files = WfFileRepository(session, workspace_id)
                owners = await files.conversation_upload_owners()
                deleted = await WfConversationRepository(session, workspace_id).deleted_ids(owners)
                rows = await files.uploads_for_conversations(deleted)
                await files.delete_many([row.id for row in rows])
            await self._delete_keys(row.storage_key for row in rows)
            removed += len(rows)
        return removed

    async def purge_loose_uploads(self) -> int:
        """wf_run_id·conversation_id가 둘 다 없고 올린 지 7일이 지난 업로드(4.4).

        워크플로우 런 입력(start 파일 필드)과 폼 파일 칸으로 쓴 업로드는 런을 만들거나 폼에 답할 때
        그 런에 묶이므로(bind_run_uploads) 대상이 아니다. 묶이지 않은 것이라도 끝나지 않은 런의
        입력·체크포인트·재개 값이 가리키는 파일과, 지우지 않은 대화의 마지막 user 메시지 입력이
        가리키는 파일은 남긴다. 재개와 대화의 다음 턴이 그 파일을 다시 읽는다.
        """
        cutoff = self._clock() - WF_LOOSE_UPLOAD_KEEP
        removed = 0
        for workspace_id, _ in await self._workspaces():
            after: uuid.UUID | None = None
            while True:
                # 런을 만드는 쓰기와 겹치지 않게 쓰기 트랜잭션 안에서 쓰는 곳을 다시 본다.
                async with self._db.write() as session:
                    files = WfFileRepository(session, workspace_id)
                    rows = await files.loose_uploads_before(cutoff, after=after, limit=self._batch)
                    owners = {row.workflow_id for row in rows}
                    used = await _uploads_in_use(session, workspace_id, owners) if rows else set()
                    stale = [row for row in rows if str(row.id) not in used]
                    await files.delete_many([row.id for row in stale])
                await self._delete_keys(row.storage_key for row in stale)
                removed += len(stale)
                if len(rows) < self._batch:
                    break
                after = rows[-1].id
        return removed

    async def purge_workspaces(self) -> int:
        """런 행이 없거나 끝난 지 1시간이 지난 런의 작업 폴더. 대기·실행 중인 런의 폴더는 둔다."""
        workspaces = getattr(self._backend, "workspaces_dir", None)
        if not isinstance(workspaces, Path):
            return 0
        folders = await asyncio.to_thread(_uuid_dirs, workspaces)
        cutoff = self._clock() - WF_WORKSPACE_KEEP
        keep: set[uuid.UUID] = set()
        for workspace_id, _ in await self._workspaces():
            async with self._db.read() as session:
                ended = await WfRunRepository(session, workspace_id).ended_at_of(folders)
            for run_id, (_, ended_at) in ended.items():
                if ended_at is None or ended_at >= cutoff:
                    keep.add(run_id)
        root = await asyncio.to_thread(workspaces.resolve)
        removed = 0
        for run_id in sorted(folders - keep):
            handle = WorkspaceHandle(session_id=run_id, root=root / str(run_id), backend="local")
            try:
                await self._backend.release(handle, delete=True)
            except SandboxError as exc:
                log.warning(
                    "housekeeping.wf_workspace_delete_failed", run_id=str(run_id), error=exc.message
                )
                continue
            removed += 1
        # 잠겨서 이름만 바꿔 둔 .deleted-* 폴더도 다시 지운다.
        removed += await self._backend.purge_deleted()
        return removed


async def _uploads_in_use(
    session: AsyncSession, workspace_id: uuid.UUID, workflow_ids: Collection[uuid.UUID | None]
) -> set[str]:
    """끝나지 않은 런과 지우지 않은 대화의 마지막 user 메시지가 가리키는 파일 id."""
    refs: dict[str, str] = {}
    runs = await WfRunRepository(session, workspace_id).list_by_status(OPEN_RUN_STATUSES)
    for run in runs:
        for value in (run.inputs, run.state, run.resume_payload):
            collect_file_refs(value, refs)
    owners = [workflow_id for workflow_id in workflow_ids if workflow_id is not None]
    conversations = await WfConversationRepository(session, workspace_id).live_ids(owners)
    for inputs in await WfMessageRepository(session, workspace_id).latest_user_inputs(
        conversations
    ):
        collect_file_refs(inputs, refs)
    return set(refs)


class Housekeeping:
    def __init__(
        self,
        *,
        db: Database,
        backend: SandboxBackend,
        memory_budget_mb: int,
        interval_sec: float = MEMORY_CHECK_INTERVAL_SEC,
        hourly_interval_sec: float = HOURLY_INTERVAL_SEC,
        runs: RunJobs | None = None,
        retention: Retention | None = None,
        workflows: WorkflowRetention | None = None,
    ) -> None:
        self._db = db
        self._sessions = SessionStore(db)
        self._runs = runs
        self._retention = retention
        self._workflows = workflows
        self._backend = backend
        self._budget_mb = memory_budget_mb
        self._interval_sec = interval_sec
        self._hourly_interval_sec = hourly_interval_sec
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._memory_loop(), name="housekeeping-memory"),
                asyncio.create_task(self._hourly_loop(), name="housekeeping-hourly"),
            ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def check_memory(self) -> float | None:
        rss = sysinfo.rss_bytes()
        if rss is None:
            return None
        rss_mb = rss / 2**20
        # 예산을 넘어도 재시작하지 않는다. 로그로 알리기만 한다.
        if rss_mb > self._budget_mb:
            log.warning("memory.over_budget", rss_mb=round(rss_mb, 1), budget_mb=self._budget_mb)
        return rss_mb

    async def run_hourly(self) -> dict[str, Any]:
        """매시 작업을 돌리고 housekeeping.hourly 로그에 남긴 값을 돌려준다.

        한 작업이 실패해도 나머지는 돈다. 실패한 작업의 값은 None이고 다음 시간에 다시 한다.
        """
        summary: dict[str, Any] = {
            "expired_login_sessions": await _attempt(
                self._sessions.purge_expired, "housekeeping.login_sessions_failed"
            ),
            "input_timeouts": None,
            "reconciled_parents": None,
            "purged_run_events": None,
            "purged_tool_outputs": None,
            "released_task_workspaces": None,
        }
        if self._runs is not None:
            runs = self._runs
            summary["input_timeouts"] = await _attempt(
                runs.expire_waiting, "housekeeping.waiting_input_failed"
            )
            summary["reconciled_parents"] = await _attempt(
                runs.reconcile_children, "housekeeping.reconcile_children_failed"
            )
            summary["purged_run_events"] = await _attempt(
                runs.purge_events, "housekeeping.run_events_failed"
            )
            # purge_events가 파일을 지우고 남긴 빈 폴더까지 보도록 그 뒤에 둔다.
            summary["purged_tool_outputs"] = await _attempt(
                runs.purge_tool_outputs, "housekeeping.tool_outputs_failed"
            )
            summary["released_task_workspaces"] = await _attempt(
                runs.release_task_workspaces, "housekeeping.task_workspaces_failed"
            )
        if self._retention is not None:
            # 고아 작업공간을 지우다 잠겨서 이름만 바꾼 폴더를 아래 purge_deleted가 바로 다시 본다.
            for name, job in self._retention.jobs():
                summary[f"purged_{name}"] = await _attempt(
                    job, "housekeeping.retention_failed", job=name
                )
        if self._workflows is not None:
            for name, job in self._workflows.jobs():
                summary[f"purged_{name}"] = await _attempt(
                    job, "housekeeping.workflow_retention_failed", job=name
                )
        summary["purged_workspaces"] = await _attempt(
            self._backend.purge_deleted, "housekeeping.workspaces_failed"
        )
        # 지울 것을 다 지운 뒤에 잰다.
        sizes = await _attempt(self._measure_disk, "housekeeping.disk_measure_failed")
        summary.update(sizes or dict.fromkeys(DISK_FIELDS))
        # 위 작업들이 쓴 것까지 DB 파일로 옮긴다.
        checkpoint: Checkpoint | None = await _attempt(
            lambda: self._db.checkpoint("PASSIVE"), "housekeeping.checkpoint_failed"
        )
        summary["wal_frames"] = checkpoint.wal_frames if checkpoint is not None else None
        summary["wal_checkpointed"] = checkpoint.checkpointed if checkpoint is not None else None
        log.info("housekeeping.hourly", **summary)
        return summary

    async def _measure_disk(self) -> dict[str, Any]:
        """작업공간·pip 캐시와 워크플로우 폴더의 크기(3.14, 2단계 11.3). 재기만 한다.

        cache/pip·cache/wf-extract는 사용자가 지운다.
        """
        workspaces = getattr(self._backend, "workspaces_dir", None)
        if not isinstance(workspaces, Path):
            return dict.fromkeys(DISK_FIELDS)
        # 로컬 백엔드의 작업공간 폴더가 DATA_DIR/workspaces라서 나머지는 그 부모에서 잇는다(2.8).
        # 하우스키핑은 설정을 받지 않으므로(main.py) 경로를 여기서 잇는다.
        usage = await asyncio.to_thread(sysinfo.disk_usage, workspaces.parent)
        sizes: dict[str, Any] = {name: _mb(item.total_bytes) for name, item in usage.items()}
        sizes["disk_estimated"] = any(item.estimated for item in usage.values())
        return sizes

    async def _memory_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_sec)
            self.check_memory()

    async def _hourly_loop(self) -> None:
        # 켜고 끄기를 자주 하는 개인 PC라 한 시간을 다 기다리지 않고 기동 직후에도 한 번 돈다.
        while True:
            await self.run_hourly()
            await asyncio.sleep(self._hourly_interval_sec)
