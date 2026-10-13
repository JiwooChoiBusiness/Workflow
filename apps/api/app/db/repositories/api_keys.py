import uuid
from datetime import datetime

from app.db.models import ApiKey
from app.db.repositories.base import WorkspaceScopedRepository


class ApiKeyRepository(WorkspaceScopedRepository[ApiKey]):
    model = ApiKey

    async def list_for_agent(self, agent_id: uuid.UUID) -> list[ApiKey]:
        return await self._scalars(
            self._select()
            .where(ApiKey.agent_id == agent_id)
            .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        )

    async def get_for_agent(self, agent_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey | None:
        return await self._scalar(
            self._select().where(ApiKey.id == key_id, ApiKey.agent_id == agent_id)
        )

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        return await self._scalar(self._select().where(ApiKey.key_hash == key_hash))

    async def revoke_all_for_agent(self, agent_id: uuid.UUID, at: datetime) -> None:
        await self._execute(
            self._update()
            .where(ApiKey.agent_id == agent_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=at, updated_at=at)
        )

    async def touch(self, key_id: uuid.UUID, at: datetime) -> None:
        await self._execute(self._update().where(ApiKey.id == key_id).values(last_used_at=at))

    def add(self, key: ApiKey) -> ApiKey:
        return self._add(key)

    async def flush(self) -> None:
        await self._flush()
