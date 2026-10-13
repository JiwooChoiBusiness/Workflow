import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import SkillScope
from app.db.types import enum_check


class Skill(TimestampMixin, Base):
    __tablename__ = "skill"
    __table_args__ = (
        # 라이브러리 스킬은 agent_id가 NULL이라 그대로 두면 유니크가 걸리지 않는다.
        Index(
            "uq_skill_workspace_id_scope_agent_id_slug",
            "workspace_id",
            "scope",
            text("coalesce(agent_id, '')"),
            "slug",
            unique=True,
        ),
        enum_check("scope", SkillScope),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column(index=False)
    scope: Mapped[str] = mapped_column(String)
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent.id"))
    slug: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    current_version_id: Mapped[uuid.UUID | None]
    deleted_at: Mapped[datetime | None]
