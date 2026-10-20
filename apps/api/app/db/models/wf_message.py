import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import MessageRole, RunStatus
from app.db.types import JSONValue, enum_check


class WfMessage(TimestampMixin, Base):
    # 챗플로우 대화의 한 턴은 user 행과 assistant 행 둘이고 둘 다 그 턴의 런을 가리킨다.
    __tablename__ = "wf_message"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq"),
        enum_check("role", [MessageRole.USER, MessageRole.ASSISTANT]),
        enum_check("status", RunStatus),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wf_conversation.id"))
    # wf_run.message_id는 이 표가 생기기 전에 만든 열이라 외래 키가 없다. 이쪽에서만 건다.
    wf_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wf_run.id"))
    seq: Mapped[int]
    role: Mapped[str] = mapped_column(String)
    # assistant 행은 answer 노드들의 글을 이은 것이다. 런이 끝나기 전에는 비어 있거나 멈춘 데까지다.
    text: Mapped[str] = mapped_column(Text, default="")
    # user 행: 이 턴에 쓴 start 필드 값.
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    # user 행에 붙인 wf_file id.
    files: Mapped[list[str]] = mapped_column(JSONValue, default=list)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    # assistant 행만. 런 상태를 따라간다.
    status: Mapped[str | None] = mapped_column(String)
