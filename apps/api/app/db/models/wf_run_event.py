import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, workspace_id_column
from app.db.types import JSONValue, utcnow


class WfRunEvent(Base):
    # run_event와 같은 모양이고 외래 키만 wf_run을 가리킨다. 쓰고 나면 바꾸지 않는다.
    __tablename__ = "wf_run_event"
    __table_args__ = (
        UniqueConstraint("wf_run_id", "seq"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    wf_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wf_run.id"))
    seq: Mapped[int]
    type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONValue)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
