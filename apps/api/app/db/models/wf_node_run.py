import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import WfNodeRunStatus
from app.db.types import JSONValue, enum_check


class WfNodeRun(TimestampMixin, Base):
    __tablename__ = "wf_node_run"
    __table_args__ = (
        Index(None, "wf_run_id", "index"),
        # 자식 런의 대기 만료·재기동 복구가 이 열로 워크플로우 런을 거꾸로 찾는다(2단계 3.7).
        Index(None, "child_run_id"),
        enum_check("status", WfNodeRunStatus),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    wf_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wf_run.id"))
    node_id: Mapped[str] = mapped_column(String)
    node_type: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, default="")
    # 런 안 실행 순서. 재개된 노드는 처음 받은 번호를 그대로 쓴다.
    index: Mapped[int]
    iteration_node_id: Mapped[str | None] = mapped_column(String)
    iteration_index: Mapped[int | None]
    attempt: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String)
    # 64KB(iteration 하위 노드는 8KB)까지 자른 값. 보존 기간이 지나면 iteration 하위 노드는 비운다.
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    outputs: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    truncated: Mapped[bool] = mapped_column(default=False)
    process: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    warnings: Mapped[list[str]] = mapped_column(JSONValue, default=list)
    child_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("run.id"))
    branch: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]
