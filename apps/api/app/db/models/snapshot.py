import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import SnapshotKind
from app.db.types import enum_check


class Snapshot(TimestampMixin, Base):
    __tablename__ = "snapshot"
    __table_args__ = (enum_check("kind", SnapshotKind),)

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    kind: Mapped[str] = mapped_column(String, default=SnapshotKind.PUBLISHED)
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_version.id"), index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("session.id"))
    storage_key: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int]
    # 압축 전 파일 크기 합계.
    raw_bytes: Mapped[int]
    file_count: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
