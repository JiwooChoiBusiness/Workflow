import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import AgentVersionStatus
from app.db.types import JSONValue, enum_check


class WfVersion(TimestampMixin, Base):
    __tablename__ = "wf_version"
    __table_args__ = (
        UniqueConstraint("workflow_id", "version_number"),
        # version_number가 NULL인 행이 초안이다. 워크플로우당 초안 1행은 부분 인덱스로 막는다.
        Index(
            "uq_wf_version_workflow_id_draft",
            "workflow_id",
            unique=True,
            sqlite_where=text("version_number IS NULL"),
        ),
        enum_check("status", AgentVersionStatus),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow.id"))
    version_number: Mapped[int | None]
    status: Mapped[str] = mapped_column(String)
    # app/schemas/workflow.py의 Graph. 초안은 검증 오류가 있어도 저장된다.
    graph: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # [{name, value?, value_encrypted?, secret}]. 비밀값은 value_encrypted에만 있고
    # app/security/crypto.py로만 읽고 쓴다.
    env_vars: Mapped[list[dict[str, Any]]] = mapped_column(JSONValue, default=list)
    conversation_vars: Mapped[list[dict[str, Any]]] = mapped_column(JSONValue, default=list)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # 그래프 JSON(키 정렬)의 sha256. 대기 중인 초안 런을 이을 때 그래프가 바뀌었는지 본다.
    graph_hash: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime | None]
    change_note: Mapped[str | None] = mapped_column(Text)
