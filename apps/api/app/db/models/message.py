import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import MessageRole
from app.db.types import JSONValue, enum_check


class Message(TimestampMixin, Base):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("session_id", "seq"),
        enum_check("role", MessageRole),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"))
    # run.user_message_id·assistant_message_id가 이 행을 가리키므로 외래 키를 두지 않는다.
    run_id: Mapped[uuid.UUID | None]
    seq: Mapped[int]
    role: Mapped[str] = mapped_column(String)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONValue, default=list)
    text: Mapped[str] = mapped_column(Text, default="")
    stop_reason: Mapped[str | None] = mapped_column(String)
    files: Mapped[list[str]] = mapped_column(JSONValue, default=list)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
