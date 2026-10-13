import uuid

from sqlalchemy import func

from app.db.models import Skill
from app.db.models.enums import SkillScope
from app.db.repositories.base import WorkspaceScopedRepository


class SkillRepository(WorkspaceScopedRepository[Skill]):
    # 지운(deleted_at) 스킬은 목록에서 빠지지만 버전과 패키지는 남는다. 발행 버전이 계속 쓴다.
    model = Skill

    async def get_active(self, skill_id: uuid.UUID) -> Skill | None:
        return await self._scalar(
            self._select().where(Skill.id == skill_id, Skill.deleted_at.is_(None))
        )

    async def list_library(self) -> list[Skill]:
        return await self._scalars(
            self._select()
            .where(Skill.scope == SkillScope.LIBRARY, Skill.deleted_at.is_(None))
            .order_by(Skill.slug)
        )

    async def list_embedded(self, agent_id: uuid.UUID) -> list[Skill]:
        return await self._scalars(
            self._select()
            .where(
                Skill.scope == SkillScope.EMBEDDED,
                Skill.agent_id == agent_id,
                Skill.deleted_at.is_(None),
            )
            .order_by(Skill.slug)
        )

    async def count_embedded(self, agent_id: uuid.UUID) -> int:
        return (
            await self._scalar(
                self._select(func.count(Skill.id)).where(
                    Skill.scope == SkillScope.EMBEDDED,
                    Skill.agent_id == agent_id,
                    Skill.deleted_at.is_(None),
                )
            )
            or 0
        )

    async def get_active_by_slug(
        self, scope: SkillScope, agent_id: uuid.UUID | None, slug: str
    ) -> Skill | None:
        owner = Skill.agent_id.is_(None) if agent_id is None else Skill.agent_id == agent_id
        return await self._scalar(
            self._select().where(
                Skill.scope == scope, owner, Skill.slug == slug, Skill.deleted_at.is_(None)
            )
        )

    async def current_version_ids(self) -> set[uuid.UUID]:
        """살아 있는 스킬의 현재 버전. 에이전트가 쓰지 않아도 하우스키핑이 지우지 않는다."""
        rows = await self._scalars(
            self._select(Skill.current_version_id).where(
                Skill.deleted_at.is_(None), Skill.current_version_id.is_not(None)
            )
        )
        return set(rows)

    def add(self, skill: Skill) -> Skill:
        return self._add(skill)

    async def flush(self) -> None:
        await self._flush()
