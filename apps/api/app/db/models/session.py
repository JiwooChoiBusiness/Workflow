import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import ApprovalMode, SessionSource, SessionStatus
from app.db.types import JSONValue, enum_check


class ChatSession(TimestampMixin, Base):
    # sqlalchemy.orm.Session과 헷갈리지 않게 클래스 이름만 다르게 둔다. 테이블 이름은 session이다.
    __tablename__ = "session"
    __table_args__ = (
        enum_check("source", SessionSource),
        enum_check("status", SessionStatus),
        # NULL이면 에이전트 버전의 승인 모드를 따른다. 덮어쓰기 값은 off 하나뿐이다.
        enum_check("approval_override", [ApprovalMode.OFF]),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column(index=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent.id"))
    agent_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_version.id"))
    source: Mapped[str] = mapped_column(String)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_key.id"))
    external_user: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=SessionStatus.ACTIVE)
    # workspace_instance.session_id가 이 행을 가리키므로 외래 키를 두지 않는다.
    workspace_instance_id: Mapped[uuid.UUID | None]
    approval_override: Mapped[str | None] = mapped_column(String)
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    # metadata는 Declarative가 쓰는 이름이라 속성 이름만 바꾼다.
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONValue, default=dict)
    last_message_at: Mapped[datetime | None]
    message_count: Mapped[int] = mapped_column(default=0)
    deleted_at: Mapped[datetime | None]


Index(
    "ix_session_workspace_id_agent_id_last_message_at",
    ChatSession.workspace_id,
    ChatSession.agent_id,
    ChatSession.last_message_at.desc(),
)
Index(
    "ix_session_api_key_id_external_user_last_message_at",
    ChatSession.api_key_id,
    ChatSession.external_user,
    ChatSession.last_message_at.desc(),
)
