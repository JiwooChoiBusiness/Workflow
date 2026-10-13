import uuid

from app.db.models import Agent, ChatSession, WorkspaceInstance
from app.db.repositories.base import WorkspaceScopedRepository


class WorkspaceInstanceRepository(WorkspaceScopedRepository[WorkspaceInstance]):
    model = WorkspaceInstance

    async def get_for_session(self, session_id: uuid.UUID) -> WorkspaceInstance | None:
        return await self._scalar(self._select().where(WorkspaceInstance.session_id == session_id))

    async def list_for_agent_source(
        self, agent_id: uuid.UUID, source: str, *, limit: int = 20
    ) -> list[WorkspaceInstance]:
        """그 에이전트·소스의 살아 있는 세션이 가진 작업공간. 최근에 쓴 것부터."""
        return await self._scalars(
            self._select()
            .join(ChatSession, ChatSession.id == WorkspaceInstance.session_id)
            .where(
                ChatSession.agent_id == agent_id,
                ChatSession.source == source,
                ChatSession.deleted_at.is_(None),
            )
            .order_by(WorkspaceInstance.last_activity_at.desc(), WorkspaceInstance.id.desc())
            .limit(limit)
        )

    async def list_all(self) -> list[WorkspaceInstance]:
        return await self._scalars(
            self._select().order_by(
                WorkspaceInstance.last_activity_at.desc(), WorkspaceInstance.id.desc()
            )
        )

    async def list_by_status(self, status: str) -> list[WorkspaceInstance]:
        return await self._scalars(self._select().where(WorkspaceInstance.status == status))

    async def list_with_session(
        self,
    ) -> list[tuple[WorkspaceInstance, ChatSession | None, Agent | None]]:
        """작업공간 목록 화면용. 세션과 에이전트를 함께 읽는다."""
        result = await self._execute(
            self._select(WorkspaceInstance, ChatSession, Agent)
            .outerjoin(ChatSession, ChatSession.id == WorkspaceInstance.session_id)
            .outerjoin(Agent, Agent.id == ChatSession.agent_id)
            .order_by(WorkspaceInstance.last_activity_at.desc(), WorkspaceInstance.id.desc())
        )
        return [(row[0], row[1], row[2]) for row in result.all()]

    async def list_for_deleted_sessions(self) -> list[WorkspaceInstance]:
        """지운 세션에 남은 작업공간 행(하우스키핑, 5.4). 세션 삭제가 중간에 실패했을 때 생긴다."""
        return await self._scalars(
            self._select()
            .join(ChatSession, ChatSession.id == WorkspaceInstance.session_id)
            .where(ChatSession.deleted_at.is_not(None))
        )

    async def delete(self, instance_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(WorkspaceInstance.id == instance_id))

    def add(self, row: WorkspaceInstance) -> WorkspaceInstance:
        return self._add(row)

    async def flush(self) -> None:
        await self._flush()
