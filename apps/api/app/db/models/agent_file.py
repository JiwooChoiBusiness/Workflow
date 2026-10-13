import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column


class AgentFile(TimestampMixin, Base):
    __tablename__ = "agent_file"

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent.id"), index=True)
    filename: Mapped[str] = mapped_column(String)
    mime: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String)
