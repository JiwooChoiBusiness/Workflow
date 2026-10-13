import uuid
from collections.abc import Collection

from sqlalchemy import func

from app.db.models import Message
from app.db.models.enums import MessageRole
from app.db.repositories.base import WorkspaceScopedRepository


class MessageRepository(WorkspaceScopedRepository[Message]):
    model = Message

    async def next_seq(self, session_id: uuid.UUID) -> int:
        current = await self._scalar(
            self._select(func.max(Message.seq)).where(Message.session_id == session_id)
        )
        return (current or 0) + 1

    async def list_for_history(self, session_id: uuid.UUID) -> list[Message]:
        """마지막 system_summary부터 끝까지(4.7의 7번). 요약이 없으면 전부."""
        last_summary = await self._scalar(
            self._select(func.max(Message.seq)).where(
                Message.session_id == session_id, Message.role == MessageRole.SYSTEM_SUMMARY
            )
        )
        statement = self._select().where(Message.session_id == session_id)
        if last_summary is not None:
            statement = statement.where(Message.seq >= last_summary)
        return await self._scalars(statement.order_by(Message.seq))

    async def list_page(
        self, session_id: uuid.UUID, *, before_seq: int | None, limit: int
    ) -> list[Message]:
        """before_seq보다 앞의 메시지 limit개를 seq 오름차순으로."""
        statement = self._select().where(Message.session_id == session_id)
        if before_seq is not None:
            statement = statement.where(Message.seq < before_seq)
        rows = await self._scalars(statement.order_by(Message.seq.desc()).limit(limit))
        return list(reversed(rows))

    async def list_for_run(self, run_id: uuid.UUID) -> list[Message]:
        return await self._scalars(
            self._select().where(Message.run_id == run_id).order_by(Message.seq)
        )

    async def get_many(self, message_ids: Collection[uuid.UUID]) -> list[Message]:
        if not message_ids:
            return []
        return await self._scalars(self._select().where(Message.id.in_(list(message_ids))))

    def add(self, message: Message) -> Message:
        return self._add(message)

    async def flush(self) -> None:
        await self._flush()
