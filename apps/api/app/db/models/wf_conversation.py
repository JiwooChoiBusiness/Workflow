import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import SessionStatus, WfConversationSource
from app.db.types import JSONValue, enum_check


class WfConversation(TimestampMixin, Base):
    __tablename__ = "wf_conversation"
    __table_args__ = (
        enum_check("source", WfConversationSource),
        enum_check("status", SessionStatus),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column(index=False)
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow.id"))
    # 만든 때의 발행 버전. 다시 발행해도 이 대화는 이 버전으로 이어 간다. 미리보기는 초안 행이다.
    wf_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wf_version.id"))
    source: Mapped[str] = mapped_column(String)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_key.id"))
    external_user: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=SessionStatus.ACTIVE)
    # 대화 변수 이름 → 값. 성공한 런이 끝날 때만 고친다.
    conversation_vars: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    last_message_at: Mapped[datetime | None]
    message_count: Mapped[int] = mapped_column(default=0)
    deleted_at: Mapped[datetime | None]


Index(
    "ix_wf_conversation_workspace_id_workflow_id_last_message_at",
    WfConversation.workspace_id,
    WfConversation.workflow_id,
    WfConversation.last_message_at.desc(),
)
Index(
    "ix_wf_conversation_api_key_id_external_user_last_message_at",
    WfConversation.api_key_id,
    WfConversation.external_user,
    WfConversation.last_message_at.desc(),
)
