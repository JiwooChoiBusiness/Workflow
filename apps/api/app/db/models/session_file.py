import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import SessionFileKind
from app.db.types import enum_check


class SessionFile(TimestampMixin, Base):
    __tablename__ = "session_file"
    __table_args__ = (
        Index(None, "session_id", "created_at"),
        enum_check("kind", SessionFileKind),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("run.id"))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message.id"))
    kind: Mapped[str] = mapped_column(String)
    # 작업공간 기준 상대 경로.
    path: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String)
    mime: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    # NULL이면 storage에 보존하지 않은 파일이고 reason에 이유를 남긴다.
    storage_key: Mapped[str | None] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String)
