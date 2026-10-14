import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import func

from app.db.models import Agent, ChatSession, Run
from app.db.models.enums import RunStatus
from app.db.repositories.base import WorkspaceScopedRepository

# 세션에 동시에 하나만 있을 수 있는 상태(설계 3.16 세션당 동시 런 1).
ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_INPUT)
# 부모 사슬을 따라 올라갈 때의 상한. 깊이 한도(ORCHESTRATION_MAX_DEPTH)는 3까지라 넉넉하다.
MAX_CHAIN = 8


class RunRepository(WorkspaceScopedRepository[Run]):
    model = Run

    async def list_children(
        self, parent_ids: Collection[uuid.UUID], *, statuses: Collection[str] | None = None
    ) -> list[Run]:
        """call_agent가 만든 자식 런(run.parent_run_id). 만든 순서."""
        if not parent_ids:
            return []
        statement = self._select().where(Run.parent_run_id.in_(list(parent_ids)))
        if statuses is not None:
            statement = statement.where(Run.status.in_(list(statuses)))
        return await self._scalars(statement.order_by(Run.queued_at, Run.id))

    async def count_children(self, run_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, int]:
        """런 id별 자식 수. 자식이 없는 id는 빠진다."""
        if not run_ids:
            return {}
        result = await self._execute(
            self._select(Run.parent_run_id, func.count(Run.id))
            .where(Run.parent_run_id.in_(list(run_ids)))
            .group_by(Run.parent_run_id)
        )
        return {row[0]: int(row[1]) for row in result.all()}

    async def tree_child_count(self, root_id: uuid.UUID) -> int:
        """root 아래 모든 자손 런 수(오케스트레이션 3.9의 트리 예산). 깊이만큼 일괄 조회한다."""
        total = 0
        level = [root_id]
        for _ in range(MAX_CHAIN):
            result = await self._execute(self._select(Run.id).where(Run.parent_run_id.in_(level)))
            level = [row[0] for row in result.all()]
            if not level:
                break
            total += len(level)
        return total

    async def parent_chain(self, run_id: uuid.UUID) -> list[Run]:
        """run_id의 부모부터 뿌리까지. 부모가 없으면 빈 목록이다."""
        chain: list[Run] = []
        current = await self.get(run_id)
        seen = {run_id}
        while current is not None and current.parent_run_id is not None and len(chain) < MAX_CHAIN:
            if current.parent_run_id in seen:
                break
            seen.add(current.parent_run_id)
            current = await self.get(current.parent_run_id)
            if current is not None:
                chain.append(current)
        return chain

    async def child_run_in_session(self, session_id: uuid.UUID) -> Run | None:
        """call_agent가 만든 과업 세션이면 그 자식 런. 아니면 None."""
        return await self._scalar(
            self._select()
            .where(Run.session_id == session_id, Run.parent_run_id.is_not(None))
            .order_by(Run.queued_at)
            .limit(1)
        )

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

    async def ended_at_of(self, run_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, datetime | None]:
        """런 id별 ended_at. 행이 없는 id는 빠진다."""
        if not run_ids:
            return {}
        result = await self._execute(
            self._select(Run.id, Run.ended_at).where(Run.id.in_(list(run_ids)))
        )
        return {row[0]: row[1] for row in result.all()}

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
        parent_run_id: uuid.UUID | None = None,
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
        if parent_run_id is not None:
            statement = statement.where(Run.parent_run_id == parent_run_id)
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
