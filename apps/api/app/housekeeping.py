import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import structlog

from app import sysinfo
from app.auth.session_store import SessionStore
from app.db.engine import Database
from app.db.repositories.agent_files import AgentFileRepository
from app.db.repositories.agent_versions import AgentVersionRepository
from app.db.repositories.session_files import SessionFileRepository
from app.db.repositories.sessions import SessionRepository
from app.db.repositories.skill_versions import SkillVersionRepository
from app.db.repositories.skills import SkillRepository
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import utcnow
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.storage.base import StorageBackend, StorageError

log = structlog.get_logger("app.housekeeping")

MEMORY_CHECK_INTERVAL_SEC = 60.0
HOURLY_INTERVAL_SEC = 3600.0
# 참조하는 버전이 없는 에이전트 첨부·스킬 버전은 만든 지 이만큼 지나면 지운다(5.4). 올린 뒤 초안에
# 넣기 전까지 기다려 주는 기간이다.
UNREFERENCED_GRACE = timedelta(days=7)
UPLOADS_PREFIX = "uploads"


class RunJobs(Protocol):
    """런 정리(app/services/runs.py의 RunMaintenance)."""

    # 응답 없이 WAITING_INPUT_TTL_HOURS가 지난 런을 cancelled(code=input_timeout)로 닫는다(3.10).
    async def expire_waiting(self) -> int: ...

    # 보존 기간이 지난 런의 run_event와 tool-outputs 파일을 지운다(5.4).
    async def purge_events(self) -> int: ...


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
    ) -> None:
        self._sessions = SessionStore(db)
        self._runs = runs
        self._retention = retention
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

    async def run_hourly(self) -> None:
        # 한 작업이 실패해도 나머지는 돈다. 실패한 작업은 다음 시간에 다시 한다.
        expired: int | None = None
        purged: int | None = None
        timed_out: int | None = None
        purged_events: int | None = None
        retained: dict[str, int | None] = {}
        try:
            expired = await self._sessions.purge_expired()
        except Exception:
            log.exception("housekeeping.login_sessions_failed")
        if self._runs is not None:
            try:
                timed_out = await self._runs.expire_waiting()
            except Exception:
                log.exception("housekeeping.waiting_input_failed")
            try:
                purged_events = await self._runs.purge_events()
            except Exception:
                log.exception("housekeeping.run_events_failed")
        if self._retention is not None:
            # 고아 작업공간을 지우다 잠겨서 이름만 바꾼 폴더를 아래 purge_deleted가 바로 다시 본다.
            for name, job in self._retention.jobs():
                retained[name] = None
                try:
                    retained[name] = await job()
                except Exception:
                    log.exception("housekeeping.retention_failed", job=name)
        try:
            purged = await self._backend.purge_deleted()
        except Exception:
            log.exception("housekeeping.workspaces_failed")
        log.info(
            "housekeeping.hourly",
            expired_login_sessions=expired,
            purged_workspaces=purged,
            input_timeouts=timed_out,
            purged_run_events=purged_events,
            **{f"purged_{name}": count for name, count in retained.items()},
        )

    async def _memory_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_sec)
            self.check_memory()

    async def _hourly_loop(self) -> None:
        # 켜고 끄기를 자주 하는 개인 PC라 한 시간을 다 기다리지 않고 기동 직후에도 한 번 돈다.
        while True:
            await self.run_hourly()
            await asyncio.sleep(self._hourly_interval_sec)
