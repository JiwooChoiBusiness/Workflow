"""워크플로우 테이블(2단계 설계 4.1)의 리포지토리. 모든 조회는 workspace_id 범위 안이다."""

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func

from app.db.models import Run, WfFile, WfNodeRun, WfRun, WfRunEvent, WfVersion, Workflow
from app.db.models.enums import RunStatus, WfFileKind, WfNodeRunStatus, WfRunTrigger
from app.db.repositories.base import WorkspaceScopedRepository


class WorkflowRepository(WorkspaceScopedRepository[Workflow]):
    # 지운(deleted_at) 워크플로우는 런 로그를 위해 남는다. 조회는 기본으로 살아 있는 행만 본다.
    model = Workflow

    async def get_active(self, workflow_id: uuid.UUID) -> Workflow | None:
        return await self._scalar(
            self._select().where(Workflow.id == workflow_id, Workflow.deleted_at.is_(None))
        )

    async def get_active_by_slug(self, slug: str) -> Workflow | None:
        return await self._scalar(
            self._select().where(Workflow.slug == slug, Workflow.deleted_at.is_(None))
        )

    async def get_many(self, workflow_ids: Collection[uuid.UUID]) -> list[Workflow]:
        """지운 워크플로우도 포함한다(런 로그에 이름을 보일 때)."""
        if not workflow_ids:
            return []
        return await self._scalars(self._select().where(Workflow.id.in_(list(workflow_ids))))

    async def list_active(self, *, kind: str | None = None) -> list[Workflow]:
        statement = self._select().where(Workflow.deleted_at.is_(None))
        if kind is not None:
            statement = statement.where(Workflow.kind == kind)
        return await self._scalars(
            statement.order_by(Workflow.created_at.desc(), Workflow.id.desc())
        )

    async def slug_taken(self, slug: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        statement = self._select(Workflow.id).where(
            Workflow.slug == slug, Workflow.deleted_at.is_(None)
        )
        if exclude_id is not None:
            statement = statement.where(Workflow.id != exclude_id)
        return await self._scalar(statement.limit(1)) is not None

    def add(self, workflow: Workflow) -> Workflow:
        return self._add(workflow)

    async def flush(self) -> None:
        await self._flush()


class WfVersionRepository(WorkspaceScopedRepository[WfVersion]):
    model = WfVersion

    async def get_for_workflow(
        self, workflow_id: uuid.UUID, version_id: uuid.UUID
    ) -> WfVersion | None:
        return await self._scalar(
            self._select().where(WfVersion.id == version_id, WfVersion.workflow_id == workflow_id)
        )

    async def get_draft(self, workflow_id: uuid.UUID) -> WfVersion | None:
        return await self._scalar(
            self._select().where(
                WfVersion.workflow_id == workflow_id, WfVersion.version_number.is_(None)
            )
        )

    async def get_many(self, version_ids: Collection[uuid.UUID]) -> list[WfVersion]:
        if not version_ids:
            return []
        return await self._scalars(self._select().where(WfVersion.id.in_(list(version_ids))))

    async def list_published(self, workflow_id: uuid.UUID) -> list[WfVersion]:
        return await self._scalars(
            self._select()
            .where(WfVersion.workflow_id == workflow_id, WfVersion.version_number.is_not(None))
            .order_by(WfVersion.version_number.desc())
        )

    async def next_version_number(self, workflow_id: uuid.UUID) -> int:
        current = await self._scalar(
            self._select(func.max(WfVersion.version_number)).where(
                WfVersion.workflow_id == workflow_id
            )
        )
        return (current or 0) + 1

    def add(self, version: WfVersion) -> WfVersion:
        return self._add(version)

    async def flush(self) -> None:
        await self._flush()


class WfRunRepository(WorkspaceScopedRepository[WfRun]):
    model = WfRun

    async def list_by_status(self, statuses: Collection[str]) -> list[WfRun]:
        return await self._scalars(
            self._select()
            .where(WfRun.status.in_(list(statuses)))
            .order_by(WfRun.queued_at, WfRun.id)
        )

    async def get_many(self, run_ids: Collection[uuid.UUID]) -> list[WfRun]:
        if not run_ids:
            return []
        return await self._scalars(self._select().where(WfRun.id.in_(list(run_ids))))

    async def active_for_conversation(self, conversation_id: uuid.UUID) -> WfRun | None:
        """대화에서 아직 끝나지 않은 런(큐, 실행 중, 입력 대기)."""
        return await self._scalar(
            self._select()
            .where(
                WfRun.conversation_id == conversation_id,
                WfRun.status.in_([RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_INPUT]),
            )
            .order_by(WfRun.queued_at.desc())
            .limit(1)
        )

    async def latest_for_conversation(self, conversation_id: uuid.UUID) -> WfRun | None:
        return await self._scalar(
            self._select()
            .where(WfRun.conversation_id == conversation_id)
            .order_by(WfRun.queued_at.desc(), WfRun.id.desc())
            .limit(1)
        )

    async def list_for_key(
        self,
        api_key_id: uuid.UUID,
        *,
        status: str | None = None,
        external_user: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[WfRun]:
        """서비스 API 키 하나로 만든 런(최근 것부터)."""
        statement = self._select().where(WfRun.api_key_id == api_key_id)
        if status is not None:
            statement = statement.where(WfRun.status == status)
        if external_user is not None:
            statement = statement.where(WfRun.external_user == external_user)
        return await self._scalars(
            statement.order_by(WfRun.queued_at.desc(), WfRun.id.desc()).limit(limit).offset(offset)
        )

    async def list_filtered(
        self,
        *,
        workflow_id: uuid.UUID | None = None,
        status: str | None = None,
        trigger: str | None = None,
        conversation_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WfRun]:
        statement = self._select()
        if workflow_id is not None:
            statement = statement.where(WfRun.workflow_id == workflow_id)
        if status is not None:
            statement = statement.where(WfRun.status == status)
        if trigger is not None:
            statement = statement.where(WfRun.trigger == trigger)
        else:
            # 단일 노드 실행은 따로 고를 때만 보인다(2단계 11.2).
            statement = statement.where(WfRun.trigger != WfRunTrigger.SINGLE_NODE)
        if conversation_id is not None:
            statement = statement.where(WfRun.conversation_id == conversation_id)
        if since is not None:
            statement = statement.where(WfRun.queued_at >= since)
        if until is not None:
            statement = statement.where(WfRun.queued_at < until)
        return await self._scalars(
            statement.order_by(WfRun.queued_at.desc(), WfRun.id.desc()).limit(limit).offset(offset)
        )

    async def ended_before(
        self, cutoff: datetime, *, statuses: Collection[str], with_state: bool, limit: int
    ) -> list[WfRun]:
        statement = self._select().where(
            WfRun.status.in_(list(statuses)),
            WfRun.ended_at.is_not(None),
            WfRun.ended_at < cutoff,
        )
        if with_state:
            statement = statement.where(WfRun.state.is_not(None))
        return await self._scalars(statement.order_by(WfRun.ended_at).limit(limit))

    async def single_node_ended_before(self, cutoff: datetime, *, limit: int) -> list[uuid.UUID]:
        return await self._scalars(
            self._select(WfRun.id)
            .where(
                WfRun.trigger == WfRunTrigger.SINGLE_NODE,
                WfRun.ended_at.is_not(None),
                WfRun.ended_at < cutoff,
            )
            .limit(limit)
        )

    async def ended_at_of(
        self, run_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str, datetime | None]]:
        """런 id별 (status, ended_at). 행이 없는 id는 빠진다."""
        if not run_ids:
            return {}
        result = await self._execute(
            self._select(WfRun.id, WfRun.status, WfRun.ended_at).where(WfRun.id.in_(list(run_ids)))
        )
        return {row[0]: (row[1], row[2]) for row in result.all()}

    async def delete_many(self, run_ids: Collection[uuid.UUID]) -> None:
        if run_ids:
            await self._execute(self._delete().where(WfRun.id.in_(list(run_ids))))

    def add(self, run: WfRun) -> WfRun:
        return self._add(run)

    async def flush(self) -> None:
        await self._flush()


class WfNodeRunRepository(WorkspaceScopedRepository[WfNodeRun]):
    model = WfNodeRun

    async def get_for_run(self, wf_run_id: uuid.UUID, node_run_id: uuid.UUID) -> WfNodeRun | None:
        return await self._scalar(
            self._select().where(WfNodeRun.id == node_run_id, WfNodeRun.wf_run_id == wf_run_id)
        )

    async def list_for_run(
        self, wf_run_id: uuid.UUID, *, iteration_node_id: str | None = None
    ) -> list[WfNodeRun]:
        """런의 노드 런(실행 순서). iteration_node_id를 주면 그 iteration 하위 노드 런만."""
        statement = self._select().where(WfNodeRun.wf_run_id == wf_run_id)
        if iteration_node_id is None:
            statement = statement.where(WfNodeRun.iteration_node_id.is_(None))
        else:
            statement = statement.where(WfNodeRun.iteration_node_id == iteration_node_id)
        return await self._scalars(statement.order_by(WfNodeRun.index, WfNodeRun.attempt))

    async def children_of(
        self, wf_run_ids: Collection[uuid.UUID], *, statuses: Collection[str] | None = None
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(wf_run_id, child_run_id). agent 노드가 만든 자식 런(1단계 run).

        statuses를 주면 자식 런이 그 상태인 것만. iteration 안의 agent 노드는 자식을 수백 개 만들
        수 있어 끝난 자식까지 하나씩 중지해 보지 않게 한다.
        """
        if not wf_run_ids:
            return []
        statement = self._select(WfNodeRun.wf_run_id, WfNodeRun.child_run_id).where(
            WfNodeRun.wf_run_id.in_(list(wf_run_ids)), WfNodeRun.child_run_id.is_not(None)
        )
        if statuses is not None:
            statement = statement.join(Run, Run.id == WfNodeRun.child_run_id).where(
                Run.workspace_id == self.workspace_id, Run.status.in_(list(statuses))
            )
        result = await self._execute(statement)
        return [(row[0], row[1]) for row in result.all()]

    async def parents_of(
        self, child_run_ids: Collection[uuid.UUID]
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(child_run_id, wf_run_id). 자식 런 id로 워크플로우 런을 거꾸로 찾는다.

        wf_node_run.child_run_id 인덱스를 탄다.
        """
        if not child_run_ids:
            return []
        result = await self._execute(
            self._select(WfNodeRun.child_run_id, WfNodeRun.wf_run_id).where(
                WfNodeRun.child_run_id.in_(list(child_run_ids))
            )
        )
        return [(row[0], row[1]) for row in result.all()]

    async def orphaned_children(
        self, *, parent_statuses: Collection[str], child_statuses: Collection[str]
    ) -> list[Run]:
        """parent_statuses로 끝난 워크플로우 런의 노드가 만든 자식 런 중 child_statuses인 것."""
        return await self._scalars(
            self._select(Run)
            .select_from(WfNodeRun)
            .join(WfRun, WfRun.id == WfNodeRun.wf_run_id)
            .join(Run, Run.id == WfNodeRun.child_run_id)
            .where(
                WfRun.workspace_id == self.workspace_id,
                Run.workspace_id == self.workspace_id,
                WfRun.status.in_(list(parent_statuses)),
                Run.status.in_(list(child_statuses)),
            )
            .distinct()
        )

    async def close_running(self, wf_run_id: uuid.UUID, now: datetime) -> int:
        """프로세스와 함께 사라진 실행 중 노드 런을 cancelled로 닫는다(기동 복구)."""
        rows = await self._scalars(
            self._select().where(
                WfNodeRun.wf_run_id == wf_run_id, WfNodeRun.status == WfNodeRunStatus.RUNNING
            )
        )
        for row in rows:
            row.status = WfNodeRunStatus.CANCELLED
            row.ended_at = now
        return len(rows)

    async def close_waiting(
        self,
        wf_run_id: uuid.UUID,
        now: datetime,
        *,
        status: WfNodeRunStatus,
        error: dict[str, Any] | None = None,
    ) -> int:
        """런이 끝날 때 아직 입력을 기다리던 노드 런을 status로 닫는다."""
        rows = await self._scalars(
            self._select().where(
                WfNodeRun.wf_run_id == wf_run_id,
                WfNodeRun.status == WfNodeRunStatus.WAITING_INPUT,
            )
        )
        for row in rows:
            row.status = status
            row.error = dict(error) if error is not None else None
            row.ended_at = now
        return len(rows)

    async def clear_iteration_io(self, wf_run_ids: Collection[uuid.UUID]) -> None:
        if wf_run_ids:
            await self._execute(
                self._update()
                .where(
                    WfNodeRun.wf_run_id.in_(list(wf_run_ids)),
                    WfNodeRun.iteration_node_id.is_not(None),
                )
                .values(inputs=None, outputs=None)
            )

    async def delete_for_runs(self, wf_run_ids: Collection[uuid.UUID]) -> None:
        if wf_run_ids:
            await self._execute(self._delete().where(WfNodeRun.wf_run_id.in_(list(wf_run_ids))))

    def add(self, row: WfNodeRun) -> WfNodeRun:
        return self._add(row)

    async def flush(self) -> None:
        await self._flush()


class WfRunEventRepository(WorkspaceScopedRepository[WfRunEvent]):
    model = WfRunEvent

    def add_many(self, rows: Sequence[WfRunEvent]) -> None:
        for row in rows:
            self._add(row)

    async def list_after(
        self,
        wf_run_id: uuid.UUID,
        *,
        after: int,
        before: int | None = None,
        limit: int | None = None,
    ) -> list[WfRunEvent]:
        statement = self._select().where(WfRunEvent.wf_run_id == wf_run_id, WfRunEvent.seq > after)
        if before is not None:
            statement = statement.where(WfRunEvent.seq < before)
        statement = statement.order_by(WfRunEvent.seq)
        if limit is not None:
            statement = statement.limit(limit)
        return await self._scalars(statement)

    async def max_seq(self, wf_run_id: uuid.UUID) -> int:
        current = await self._scalar(
            self._select(func.max(WfRunEvent.seq)).where(WfRunEvent.wf_run_id == wf_run_id)
        )
        return current or 0

    async def runs_ended_before(self, cutoff: datetime, *, limit: int) -> list[uuid.UUID]:
        """보존 기간이 지난 런 중 이벤트가 남은 런(2단계 4.4)."""
        return await self._scalars(
            self._select(WfRunEvent.wf_run_id)
            .join(WfRun, WfRun.id == WfRunEvent.wf_run_id)
            .where(
                WfRun.workspace_id == self.workspace_id,
                WfRun.ended_at.is_not(None),
                WfRun.ended_at < cutoff,
            )
            .group_by(WfRunEvent.wf_run_id)
            .limit(limit)
        )

    async def delete_for_runs(self, wf_run_ids: Collection[uuid.UUID]) -> None:
        if wf_run_ids:
            await self._execute(self._delete().where(WfRunEvent.wf_run_id.in_(list(wf_run_ids))))


class WfFileRepository(WorkspaceScopedRepository[WfFile]):
    model = WfFile

    async def get_many(self, file_ids: Collection[uuid.UUID]) -> list[WfFile]:
        if not file_ids:
            return []
        return await self._scalars(self._select().where(WfFile.id.in_(list(file_ids))))

    async def outputs_ended_before(self, cutoff: datetime, *, limit: int) -> list[WfFile]:
        """런이 끝난 지 보존 기간이 지난 출력 파일(2단계 4.4)."""
        return await self._scalars(
            self._select()
            .join(WfRun, WfRun.id == WfFile.wf_run_id)
            .where(
                WfRun.workspace_id == self.workspace_id,
                WfFile.kind == WfFileKind.OUTPUT,
                WfRun.ended_at.is_not(None),
                WfRun.ended_at < cutoff,
            )
            .limit(limit)
        )

    async def list_for_runs(self, wf_run_ids: Collection[uuid.UUID]) -> list[WfFile]:
        if not wf_run_ids:
            return []
        return await self._scalars(self._select().where(WfFile.wf_run_id.in_(list(wf_run_ids))))

    async def uploads_for_conversations(
        self, conversation_ids: Collection[uuid.UUID]
    ) -> list[WfFile]:
        if not conversation_ids:
            return []
        return await self._scalars(
            self._select().where(
                WfFile.kind == WfFileKind.UPLOAD,
                WfFile.conversation_id.in_(list(conversation_ids)),
            )
        )

    async def bind_to_run(
        self, wf_run_id: uuid.UUID, workflow_id: uuid.UUID, file_ids: Collection[uuid.UUID]
    ) -> None:
        """이 워크플로우의 업로드 중 런·대화에 아직 묶이지 않은 것을 런에 묶는다."""
        if file_ids:
            await self._execute(
                self._update()
                .where(
                    WfFile.id.in_(list(file_ids)),
                    WfFile.workflow_id == workflow_id,
                    WfFile.kind == WfFileKind.UPLOAD,
                    WfFile.wf_run_id.is_(None),
                    WfFile.conversation_id.is_(None),
                )
                .values(wf_run_id=wf_run_id)
            )

    async def loose_uploads_before(
        self, cutoff: datetime, *, after: uuid.UUID | None, limit: int
    ) -> list[WfFile]:
        """런·대화를 가리키지 않는 업로드 중 cutoff 전에 올린 것. id 순으로 after 다음부터."""
        statement = self._select().where(
            WfFile.kind == WfFileKind.UPLOAD,
            WfFile.wf_run_id.is_(None),
            WfFile.conversation_id.is_(None),
            WfFile.created_at < cutoff,
        )
        if after is not None:
            statement = statement.where(WfFile.id > after)
        return await self._scalars(statement.order_by(WfFile.id).limit(limit))

    async def conversation_upload_owners(self) -> set[uuid.UUID]:
        """메시지에 붙은 업로드가 가리키는 대화 id."""
        rows = await self._scalars(
            self._select(WfFile.conversation_id)
            .where(WfFile.kind == WfFileKind.UPLOAD, WfFile.conversation_id.is_not(None))
            .distinct()
        )
        return set(rows)

    async def delete_many(self, file_ids: Collection[uuid.UUID]) -> None:
        if file_ids:
            await self._execute(self._delete().where(WfFile.id.in_(list(file_ids))))

    def add(self, row: WfFile) -> WfFile:
        return self._add(row)

    async def flush(self) -> None:
        await self._flush()
