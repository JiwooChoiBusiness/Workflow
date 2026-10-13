import uuid
from datetime import datetime

from sqlalchemy import Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column


class Agent(TimestampMixin, Base):
    __tablename__ = "agent"
    __table_args__ = (
        # 지운 에이전트의 slug는 다시 쓸 수 있게 살아 있는 행끼리만 유일하다(리비전 0002).
        Index(
            "uq_agent_workspace_id_slug",
            "workspace_id",
            "slug",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index(None, "workspace_id", "deleted_at"),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column(index=False)
    slug: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    icon: Mapped[str | None] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    # 버전 쪽이 agent_id로 이 행을 가리키므로 여기에는 외래 키를 두지 않는다(순환 참조 방지).
    draft_version_id: Mapped[uuid.UUID | None]
    published_version_id: Mapped[uuid.UUID | None]
    webapp_enabled: Mapped[bool] = mapped_column(default=False)
    deleted_at: Mapped[datetime | None]
