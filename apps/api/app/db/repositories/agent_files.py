import uuid
from collections.abc import Collection
from datetime import datetime

from app.db.models import AgentFile
from app.db.repositories.base import WorkspaceScopedRepository


class AgentFileRepository(WorkspaceScopedRepository[AgentFile]):
    model = AgentFile

    async def list_for_agent(self, agent_id: uuid.UUID) -> list[AgentFile]:
        return await self._scalars(
            self._select()
            .where(AgentFile.agent_id == agent_id)
            .order_by(AgentFile.created_at.desc(), AgentFile.id.desc())
        )

    async def get_for_agent(self, agent_id: uuid.UUID, file_id: uuid.UUID) -> AgentFile | None:
        return await self._scalar(
            self._select().where(AgentFile.id == file_id, AgentFile.agent_id == agent_id)
        )

    async def get_many(self, file_ids: Collection[uuid.UUID]) -> list[AgentFile]:
        if not file_ids:
            return []
        return await self._scalars(self._select().where(AgentFile.id.in_(list(file_ids))))

    async def list_created_before(self, cutoff: datetime) -> list[AgentFile]:
        return await self._scalars(self._select().where(AgentFile.created_at < cutoff))

    def add(self, row: AgentFile) -> AgentFile:
        return self._add(row)

    async def delete(self, file_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(AgentFile.id == file_id))

    async def flush(self) -> None:
        await self._flush()
