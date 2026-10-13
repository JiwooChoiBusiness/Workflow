"""작업공간 목록(설계 6.2 GET /workspaces, 7.3 WorkspaceTable).

크기는 런이 끝날 때 잰 값(workspace_instance.disk_bytes)을 그대로 보인다. 목록을 열 때마다
모든 폴더를 다시 재면 작업공간마다 몇 초씩 걸린다. 실행 중인 도구 프로세스는 런이 도는
작업공간에서만 백엔드에 묻는다(동시 런이 1이라 많아야 몇 개다). 세션별 상세·종료·정리는
SessionService가 맡는다.
"""

import uuid

import structlog

from app.config import Settings
from app.db.engine import Database
from app.db.models import WorkspaceInstance
from app.db.models.enums import RunStatus, WorkspaceInstanceStatus
from app.db.repositories.runs import RunRepository
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.sandbox.base import RunningExec, SandboxBackend, SandboxError
from app.schemas.logs import WorkspaceOverview, WorkspaceOverviewList
from app.services.runs import workspace_handle

log = structlog.get_logger("app.services.workspaces")


class WorkspaceOverviewService:
    def __init__(self, db: Database, settings: Settings, *, backend: SandboxBackend) -> None:
        self._db = db
        self._settings = settings
        self._backend = backend

    async def list_workspaces(self, workspace_id: uuid.UUID) -> WorkspaceOverviewList:
        async with self._db.read() as session:
            rows = await WorkspaceInstanceRepository(session, workspace_id).list_with_session()
            active = await RunRepository(session, workspace_id).active_for_sessions(
                [instance.session_id for instance, _, _ in rows]
            )
        limit = self._settings.workspace_max_bytes
        items: list[WorkspaceOverview] = []
        for instance, chat, agent in rows:
            run = active.get(instance.session_id)
            running: list[RunningExec] = []
            if instance.status == WorkspaceInstanceStatus.BUSY or (
                run is not None and run.status == RunStatus.RUNNING
            ):
                running = await self._running(instance)
            peaks = [item.peak_memory_bytes for item in running if item.peak_memory_bytes]
            items.append(
                WorkspaceOverview(
                    id=instance.id,
                    session_id=instance.session_id,
                    backend=instance.backend,
                    root_path=instance.root_path,
                    status=instance.status,  # pyright: ignore[reportArgumentType]
                    disk_bytes=instance.disk_bytes,
                    disk_estimated=instance.disk_estimated,
                    disk_measured_at=instance.disk_measured_at,
                    last_activity_at=instance.last_activity_at,
                    created_at=instance.created_at,
                    session_title=chat.title if chat is not None else None,
                    agent_id=chat.agent_id if chat is not None else None,
                    session_source=chat.source if chat is not None else None,  # pyright: ignore[reportArgumentType]
                    agent_name=agent.name if agent is not None else None,
                    agent_deleted=agent is not None and agent.deleted_at is not None,
                    active_run_id=run.id if run is not None else None,
                    active_run_status=run.status if run is not None else None,  # pyright: ignore[reportArgumentType]
                    running=running,
                    peak_memory_bytes=max(peaks) if peaks else None,
                    over_limit=bool(limit) and instance.disk_bytes > limit,
                )
            )
        return WorkspaceOverviewList(
            items=items,
            disk_limit_bytes=limit,
            total_disk_bytes=sum(item.disk_bytes for item in items),
        )

    async def _running(self, instance: WorkspaceInstance) -> list[RunningExec]:
        try:
            handle = workspace_handle(self._backend, instance.session_id, instance)
            stats = await self._backend.stats(handle)
        except SandboxError as exc:
            log.warning(
                "workspaces.stats_failed", session_id=str(instance.session_id), error=exc.message
            )
            return []
        return stats.running
