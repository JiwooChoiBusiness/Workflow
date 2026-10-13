import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import ApprovalMode, RunStatus, RunTrigger
from app.db.types import JSONValue, enum_check, utcnow


class Run(TimestampMixin, Base):
    __tablename__ = "run"
    __table_args__ = (
        Index(None, "session_id", "queued_at"),
        Index(None, "workspace_id", "status"),
        enum_check("trigger", RunTrigger),
        enum_check("status", RunStatus),
        enum_check("approval_mode", ApprovalMode),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column(index=False)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("session.id"))
    agent_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_version.id"))
    trigger: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=RunStatus.QUEUED)
    user_message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("message.id"))
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("message.id"))
    model: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # 런을 만들 때 적용한 값. 이후 에이전트 설정이 바뀌어도 이 런은 이 값으로 돈다.
    limits: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    approval_mode: Mapped[str] = mapped_column(String)
    # 과업 입력. correlation(워크플로우 런·노드 id)도 여기에 둔다.
    input: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    pending_interaction: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    resume_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    cancel_requested_at: Mapped[datetime | None]
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    steps: Mapped[int] = mapped_column(default=0)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    queued_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]


Index("ix_run_workspace_id_queued_at", Run.workspace_id, Run.queued_at.desc())
