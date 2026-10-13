import uuid
from collections.abc import Collection, Sequence
from datetime import datetime

from sqlalchemy import func

from app.db.models import Run, RunEvent
from app.db.repositories.base import WorkspaceScopedRepository


class RunEventRepository(WorkspaceScopedRepository[RunEvent]):
    model = RunEvent

    def add_many(self, rows: Sequence[RunEvent]) -> None:
        for row in rows:
            self._add(row)

    async def flush(self) -> None:
        await self._flush()

    async def list_after(
        self,
        run_id: uuid.UUID,
        *,
        after: int,
        before: int | None = None,
        limit: int | None = None,
    ) -> list[RunEvent]:
        statement = self._select().where(RunEvent.run_id == run_id, RunEvent.seq > after)
        if before is not None:
            statement = statement.where(RunEvent.seq < before)
        statement = statement.order_by(RunEvent.seq)
        if limit is not None:
            statement = statement.limit(limit)
        return await self._scalars(statement)

    async def list_by_type(self, run_id: uuid.UUID, event_type: str) -> list[RunEvent]:
        return await self._scalars(
            self._select()
            .where(RunEvent.run_id == run_id, RunEvent.type == event_type)
            .order_by(RunEvent.seq)
        )

    async def max_seq(self, run_id: uuid.UUID) -> int:
        current = await self._scalar(
            self._select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
        )
        return current or 0

    async def runs_ended_before(self, cutoff: datetime, *, limit: int) -> list[uuid.UUID]:
        """보존 기간이 지난 런 중 이벤트가 남은 런(5.4)."""
        return await self._scalars(
            self._select(RunEvent.run_id)
            .join(Run, Run.id == RunEvent.run_id)
            .where(
                Run.workspace_id == self.workspace_id,
                Run.ended_at.is_not(None),
                Run.ended_at < cutoff,
            )
            .group_by(RunEvent.run_id)
            .limit(limit)
        )

    async def delete_for_runs(self, run_ids: Collection[uuid.UUID]) -> None:
        if run_ids:
            await self._execute(self._delete().where(RunEvent.run_id.in_(list(run_ids))))
