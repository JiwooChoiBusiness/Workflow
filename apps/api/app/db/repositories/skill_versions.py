import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import func

from app.db.models import Skill, SkillVersion
from app.db.repositories.base import WorkspaceScopedRepository


class SkillVersionRepository(WorkspaceScopedRepository[SkillVersion]):
    model = SkillVersion

    async def list_for_skill(self, skill_id: uuid.UUID) -> list[SkillVersion]:
        return await self._scalars(
            self._select()
            .where(SkillVersion.skill_id == skill_id)
            .order_by(SkillVersion.version_number.desc())
        )

    async def get_for_skill(
        self, skill_id: uuid.UUID, version_id: uuid.UUID
    ) -> SkillVersion | None:
        return await self._scalar(
            self._select().where(SkillVersion.id == version_id, SkillVersion.skill_id == skill_id)
        )

    async def get_many(self, version_ids: Collection[uuid.UUID]) -> list[SkillVersion]:
        if not version_ids:
            return []
        return await self._scalars(self._select().where(SkillVersion.id.in_(list(version_ids))))

    async def get_many_with_skill(
        self, version_ids: Collection[uuid.UUID]
    ) -> list[tuple[SkillVersion, Skill]]:
        """버전과 그 스킬. 지운 스킬의 버전도 돌려준다(발행 버전이 계속 쓴다)."""
        if not version_ids:
            return []
        result = await self._execute(
            self._select(SkillVersion, Skill)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .where(SkillVersion.id.in_(list(version_ids)), Skill.workspace_id == self.workspace_id)
        )
        return [(row[0], row[1]) for row in result.all()]

    async def next_version_number(self, skill_id: uuid.UUID) -> int:
        current = await self._scalar(
            self._select(func.max(SkillVersion.version_number)).where(
                SkillVersion.skill_id == skill_id
            )
        )
        return (current or 0) + 1

    async def list_created_before(self, cutoff: datetime) -> list[SkillVersion]:
        return await self._scalars(self._select().where(SkillVersion.created_at < cutoff))

    async def delete_many(self, version_ids: Collection[uuid.UUID]) -> None:
        if version_ids:
            await self._execute(self._delete().where(SkillVersion.id.in_(list(version_ids))))

    def add(self, version: SkillVersion) -> SkillVersion:
        return self._add(version)

    async def flush(self) -> None:
        await self._flush()
