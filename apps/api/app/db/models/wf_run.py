import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import RunStatus, WfRunTrigger, WorkflowKind
from app.db.types import JSONValue, enum_check, utcnow


class WfRun(TimestampMixin, Base):
    __tablename__ = "wf_run"
    __table_args__ = (
        Index(None, "workspace_id", "status"),
        Index(None, "conversation_id", "queued_at"),
        enum_check("kind", WorkflowKind),
        enum_check("trigger", WfRunTrigger),
        enum_check("status", RunStatus),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column(index=False)
    workflow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workflow.id"))
    wf_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wf_version.id"))
    kind: Mapped[str] = mapped_column(String)
    trigger: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=RunStatus.QUEUED)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_key.id"))
    external_user: Mapped[str | None] = mapped_column(String)
    # 챗플로우 대화·메시지(M17의 wf_conversation·wf_message). 그 테이블이 생기기 전이라 외래 키가
    # 없다.
    conversation_id: Mapped[uuid.UUID | None]
    message_id: Mapped[uuid.UUID | None]
    # 비밀 env 값을 가린 런 입력.
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # end 출력을 합친 전문. 이벤트에는 4KB 미리보기만 싣는다.
    outputs: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    # 체크포인트(app/workflow/frames.py의 Checkpoint). 대기 전환 직전과 실패 때만 쓴다.
    state: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    # {node_id, iteration_index, child_run_id, interaction}. 병렬 대기가 있어도 하나뿐이다.
    pending_interaction: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    resume_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    cancel_requested_at: Mapped[datetime | None]
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    node_run_count: Mapped[int] = mapped_column(default=0)
    # 런을 만들 때 적용한 한도. 이후 버전 설정이 바뀌어도 이 런은 이 값으로 돈다.
    limits: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # 런을 만들 때의 wf_version.graph_hash. 초안이 바뀐 뒤에는 대기 중이던 런을 잇지 않는다.
    graph_hash: Mapped[str] = mapped_column(String(64))
    queued_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]


Index(
    "ix_wf_run_workspace_id_workflow_id_queued_at",
    WfRun.workspace_id,
    WfRun.workflow_id,
    WfRun.queued_at.desc(),
)
