import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, id_column, workspace_id_column
from app.db.models.enums import WfFileKind
from app.db.types import enum_check, utcnow


class WfFile(Base):
    # 올린 파일과 런이 만든 파일. 저장한 뒤 바꾸지 않아 updated_at이 없다.
    __tablename__ = "wf_file"
    __table_args__ = (
        Index(None, "wf_run_id"),
        Index(None, "conversation_id"),
        enum_check("kind", WfFileKind),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    kind: Mapped[str] = mapped_column(String)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow.id"))
    wf_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wf_run.id"))
    # 챗플로우 대화(M17). 그 테이블이 생기기 전이라 외래 키가 없다.
    conversation_id: Mapped[uuid.UUID | None]
    filename: Mapped[str] = mapped_column(String)
    mime: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
