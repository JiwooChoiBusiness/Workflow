import uuid
from collections.abc import Collection
from datetime import datetime

from app.db.models import ChatSession
from app.db.repositories.base import WorkspaceScopedRepository


class SessionRepository(WorkspaceScopedRepository[ChatSession]):
    # 지운(deleted_at) 세션은 로그를 위해 남는다. 조회는 기본으로 살아 있는 행만 본다.
    model = ChatSession

    async def get_active(self, session_id: uuid.UUID) -> ChatSession | None:
        return await self._scalar(
            self._select().where(ChatSession.id == session_id, ChatSession.deleted_at.is_(None))
        )

    async def get_many(self, session_ids: Collection[uuid.UUID]) -> list[ChatSession]:
        if not session_ids:
            return []
        return await self._scalars(self._select().where(ChatSession.id.in_(list(session_ids))))

    async def list_filtered(
        self,
        *,
        agent_id: uuid.UUID | None = None,
        source: str | None = None,
        api_key_id: uuid.UUID | None = None,
        external_user: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        non_empty: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ChatSession]:
        statement = self._select().where(ChatSession.deleted_at.is_(None))
        if non_empty:
            statement = statement.where(ChatSession.message_count > 0)
        if agent_id is not None:
            statement = statement.where(ChatSession.agent_id == agent_id)
        if source is not None:
            statement = statement.where(ChatSession.source == source)
        if api_key_id is not None:
            statement = statement.where(ChatSession.api_key_id == api_key_id)
        if external_user is not None:
            statement = statement.where(ChatSession.external_user == external_user)
        if since is not None:
            statement = statement.where(ChatSession.created_at >= since)
        if until is not None:
            statement = statement.where(ChatSession.created_at < until)
        return await self._scalars(
            statement.order_by(
                ChatSession.last_message_at.desc().nulls_last(),
                ChatSession.created_at.desc(),
                ChatSession.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )

    def add(self, session: ChatSession) -> ChatSession:
        return self._add(session)

    async def flush(self) -> None:
        await self._flush()
