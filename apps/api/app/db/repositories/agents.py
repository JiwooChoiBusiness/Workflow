import uuid

from app.db.models import Agent
from app.db.repositories.base import WorkspaceScopedRepository


class AgentRepository(WorkspaceScopedRepository[Agent]):
    # 지운(deleted_at) 에이전트는 로그 보존용으로만 남는다. 조회는 기본으로 살아 있는 행만 본다.
    model = Agent

    async def get_active(self, agent_id: uuid.UUID) -> Agent | None:
        return await self._scalar(
            self._select().where(Agent.id == agent_id, Agent.deleted_at.is_(None))
        )

    async def list_active(self) -> list[Agent]:
        return await self._scalars(
            self._select()
            .where(Agent.deleted_at.is_(None))
            .order_by(Agent.created_at.desc(), Agent.id.desc())
        )

    async def get_active_by_slug(self, slug: str) -> Agent | None:
        return await self._scalar(
            self._select().where(Agent.slug == slug, Agent.deleted_at.is_(None))
        )

    async def slug_taken(self, slug: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        statement = self._select(Agent.id).where(Agent.slug == slug, Agent.deleted_at.is_(None))
        if exclude_id is not None:
            statement = statement.where(Agent.id != exclude_id)
        return await self._scalar(statement.limit(1)) is not None

    def add(self, agent: Agent) -> Agent:
        return self._add(agent)

    async def flush(self) -> None:
        await self._flush()
