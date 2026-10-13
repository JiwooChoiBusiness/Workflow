import uuid
from collections.abc import Collection

from app.db.models import ChatSession, SessionFile
from app.db.models.enums import SessionFileKind
from app.db.repositories.base import WorkspaceScopedRepository


class SessionFileRepository(WorkspaceScopedRepository[SessionFile]):
    model = SessionFile

    async def list_for_session(
        self, session_id: uuid.UUID, *, kind: str | None = None, run_id: uuid.UUID | None = None
    ) -> list[SessionFile]:
        statement = self._select().where(SessionFile.session_id == session_id)
        if kind is not None:
            statement = statement.where(SessionFile.kind == kind)
        if run_id is not None:
            statement = statement.where(SessionFile.run_id == run_id)
        return await self._scalars(statement.order_by(SessionFile.created_at, SessionFile.id))

    async def get_for_session(
        self, session_id: uuid.UUID, file_id: uuid.UUID
    ) -> SessionFile | None:
        return await self._scalar(
            self._select().where(SessionFile.id == file_id, SessionFile.session_id == session_id)
        )

    async def get_many(self, file_ids: Collection[uuid.UUID]) -> list[SessionFile]:
        if not file_ids:
            return []
        return await self._scalars(self._select().where(SessionFile.id.in_(list(file_ids))))

    async def list_for_message(self, message_id: uuid.UUID) -> list[SessionFile]:
        return await self._scalars(
            self._select()
            .where(SessionFile.message_id == message_id)
            .order_by(SessionFile.created_at, SessionFile.id)
        )

    async def delete_uploads(self, session_id: uuid.UUID) -> None:
        await self._execute(
            self._delete().where(
                SessionFile.session_id == session_id, SessionFile.kind == SessionFileKind.UPLOAD
            )
        )

    async def list_uploads_of_deleted_sessions(self) -> list[SessionFile]:
        """지운 세션에 남은 첨부(하우스키핑, 5.4). 결과 파일(output)은 로그용으로 남긴다."""
        return await self._scalars(
            self._select()
            .join(ChatSession, ChatSession.id == SessionFile.session_id)
            .where(SessionFile.kind == SessionFileKind.UPLOAD, ChatSession.deleted_at.is_not(None))
        )

    async def delete_many(self, file_ids: Collection[uuid.UUID]) -> None:
        if file_ids:
            await self._execute(self._delete().where(SessionFile.id.in_(list(file_ids))))

    def add(self, row: SessionFile) -> SessionFile:
        return self._add(row)

    async def flush(self) -> None:
        await self._flush()
