import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import AgentVersionStatus, ApprovalMode
from app.db.types import JSONValue, enum_check


class AgentVersion(TimestampMixin, Base):
    __tablename__ = "agent_version"
    __table_args__ = (
        UniqueConstraint("agent_id", "version_number"),
        # version_number가 NULL인 행이 초안이다. SQLite 유니크는 NULL끼리 겹쳐도 통과하므로
        # 에이전트당 초안 1행은 부분 인덱스로 따로 막는다.
        Index(
            "uq_agent_version_agent_id_draft",
            "agent_id",
            unique=True,
            sqlite_where=text("version_number IS NULL"),
        ),
        enum_check("status", AgentVersionStatus),
        enum_check("approval_mode", ApprovalMode),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent.id"))
    version_number: Mapped[int | None]
    status: Mapped[str] = mapped_column(String)
    instructions: Mapped[str] = mapped_column(Text, default="")
    # model, tools, limits, sandbox, env_vars의 구조는 app/schemas에 둔다(M5).
    model: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    tools: Mapped[list[str]] = mapped_column(JSONValue, default=list)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    sandbox: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    approval_mode: Mapped[str] = mapped_column(String)
    # 각 항목의 value_encrypted는 app/security/crypto.py로만 읽고 쓴다.
    env_vars: Mapped[list[dict[str, Any]]] = mapped_column(JSONValue, default=list)
    skill_version_ids: Mapped[list[str]] = mapped_column(JSONValue, default=list)
    file_ids: Mapped[list[str]] = mapped_column(JSONValue, default=list)
    # SubAgentRef 목록(오케스트레이션 2.2). 0004가 기존 행에 []를 채우며 더했다.
    sub_agents: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONValue, default=list, server_default=text("'[]'")
    )
    # 스냅샷 쪽이 agent_version_id로 이 행을 가리키므로 외래 키를 두지 않는다.
    snapshot_id: Mapped[uuid.UUID | None]
    published_at: Mapped[datetime | None]
    change_note: Mapped[str | None] = mapped_column(Text)
