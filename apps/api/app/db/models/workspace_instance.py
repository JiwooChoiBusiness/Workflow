import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import WorkspaceBackend, WorkspaceInstanceStatus
from app.db.types import enum_check, utcnow


class WorkspaceInstance(TimestampMixin, Base):
    __tablename__ = "workspace_instance"
    __table_args__ = (
        enum_check("backend", WorkspaceBackend),
        enum_check("status", WorkspaceInstanceStatus),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"), unique=True)
    backend: Mapped[str] = mapped_column(String, default=WorkspaceBackend.LOCAL)
    root_path: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=WorkspaceInstanceStatus.READY)
    restored_from_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("snapshot.id"))
    disk_bytes: Mapped[int] = mapped_column(default=0)
    disk_estimated: Mapped[bool] = mapped_column(default=False)
    disk_measured_at: Mapped[datetime | None]
    last_activity_at: Mapped[datetime] = mapped_column(default=utcnow)
