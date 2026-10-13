import uuid
from collections.abc import Collection

from sqlalchemy import func

from app.db.models import AgentVersion
from app.db.repositories.base import WorkspaceScopedRepository


class AgentVersionRepository(WorkspaceScopedRepository[AgentVersion]):
    model = AgentVersion

    async def get_for_agent(
        self, agent_id: uuid.UUID, version_id: uuid.UUID
    ) -> AgentVersion | None:
        return await self._scalar(
            self._select().where(AgentVersion.id == version_id, AgentVersion.agent_id == agent_id)
        )

    async def get_draft(self, agent_id: uuid.UUID) -> AgentVersion | None:
        return await self._scalar(
            self._select().where(
                AgentVersion.agent_id == agent_id, AgentVersion.version_number.is_(None)
            )
        )

    async def get_many(self, version_ids: Collection[uuid.UUID]) -> list[AgentVersion]:
        if not version_ids:
            return []
        return await self._scalars(self._select().where(AgentVersion.id.in_(list(version_ids))))

    async def list_for_agent(self, agent_id: uuid.UUID) -> list[AgentVersion]:
        """초안과 발행 버전 전부. 발행 버전은 번호 역순, 초안이 맨 앞."""
        return await self._scalars(
            self._select()
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version_number.desc().nulls_first())
        )

    async def list_published(self, agent_id: uuid.UUID) -> list[AgentVersion]:
        return await self._scalars(
            self._select()
            .where(AgentVersion.agent_id == agent_id, AgentVersion.version_number.is_not(None))
            .order_by(AgentVersion.version_number.desc())
        )

    async def next_version_number(self, agent_id: uuid.UUID) -> int:
        current = await self._scalar(
            self._select(func.max(AgentVersion.version_number)).where(
                AgentVersion.agent_id == agent_id
            )
        )
        return (current or 0) + 1

    async def referenced_ids(self) -> tuple[set[str], set[str]]:
        """모든 버전(초안·발행, 지운 에이전트 것까지)이 쓰는 (파일 id, 스킬 버전 id). 문자열이다.

        하우스키핑이 참조 없는 파일·스킬 버전을 고를 때 쓴다(5.4).
        """
        result = await self._execute(
            self._select(AgentVersion.file_ids, AgentVersion.skill_version_ids)
        )
        files: set[str] = set()
        skills: set[str] = set()
        for file_ids, skill_version_ids in result.all():
            files.update(str(value) for value in file_ids or [])
            skills.update(str(value) for value in skill_version_ids or [])
        return files, skills

    def add(self, version: AgentVersion) -> AgentVersion:
        return self._add(version)

    async def flush(self) -> None:
        await self._flush()
