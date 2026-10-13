import uuid
from collections.abc import Collection
from datetime import datetime

from app.db.models import Agent, ChatSession, Run
from app.db.models.enums import RunStatus
from app.db.repositories.base import WorkspaceScopedRepository

# 세션에 동시에 하나만 있을 수 있는 상태(설계 3.16 세션당 동시 런 1).
ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_INPUT)


class RunRepository(WorkspaceScopedRepository[Run]):
    model = Run

    async def active_for_session(self, session_id: uuid.UUID) -> Run | None:
        return await self._scalar(
            self._select()
            .where(Run.session_id == session_id, Run.status.in_(ACTIVE_STATUSES))
            .order_by(Run.queued_at.desc())
            .limit(1)
        )

    async def latest_for_session(self, session_id: uuid.UUID) -> Run | None:
        return await self._scalar(
            self._select()
            .where(Run.session_id == session_id)
            .order_by(Run.queued_at.desc(), Run.id.desc())
            .limit(1)
        )

    async def list_by_status(self, statuses: Collection[str]) -> list[Run]:
        return await self._scalars(
            self._select().where(Run.status.in_(list(statuses))).order_by(Run.queued_at, Run.id)
        )

    async def list_filtered(
        self,
        *,
        session_id: uuid.UUID | None = None,
        session_ids: Collection[uuid.UUID] | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Run]:
        statement = self._select()
        if session_id is not None:
            statement = statement.where(Run.session_id == session_id)
        if session_ids is not None:
            statement = statement.where(Run.session_id.in_(list(session_ids)))
        if status is not None:
            statement = statement.where(Run.status == status)
        if since is not None:
            statement = statement.where(Run.queued_at >= since)
        if until is not None:
            statement = statement.where(Run.queued_at < until)
        return await self._scalars(
            statement.order_by(Run.queued_at.desc(), Run.id.desc()).limit(limit).offset(offset)
        )

    async def list_for_logs(
        self,
        *,
        session_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        source: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[tuple[Run, ChatSession, Agent]]:
        """로그 화면의 런 목록. 세션·에이전트를 함께 읽는다. 지운 세션·에이전트의 런도 남아 있다."""
        statement = (
            self._select(Run, ChatSession, Agent)
            .join(ChatSession, ChatSession.id == Run.session_id)
            .join(Agent, Agent.id == ChatSession.agent_id)
            .where(ChatSession.workspace_id == self.workspace_id)
        )
        if session_id is not None:
            statement = statement.where(Run.session_id == session_id)
        if agent_id is not None:
            statement = statement.where(ChatSession.agent_id == agent_id)
        if source is not None:
            statement = statement.where(ChatSession.source == source)
        if status is not None:
            statement = statement.where(Run.status == status)
        if since is not None:
            statement = statement.where(Run.queued_at >= since)
        if until is not None:
            statement = statement.where(Run.queued_at < until)
        result = await self._execute(
            statement.order_by(Run.queued_at.desc(), Run.id.desc()).limit(limit).offset(offset)
        )
        return [(row[0], row[1], row[2]) for row in result.all()]

    async def active_for_sessions(self, session_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, Run]:
        if not session_ids:
            return {}
        rows = await self._scalars(
            self._select()
            .where(Run.session_id.in_(list(session_ids)), Run.status.in_(ACTIVE_STATUSES))
            .order_by(Run.queued_at)
        )
        # 세션당 하나뿐이지만 혹시 여럿이면 가장 늦게 만든 런을 남긴다.
        return {run.session_id: run for run in rows}

    def add(self, run: Run) -> Run:
        return self._add(run)

    async def flush(self) -> None:
        await self._flush()
