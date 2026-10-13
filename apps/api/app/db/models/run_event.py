import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, workspace_id_column
from app.db.types import JSONValue, utcnow


class RunEvent(Base):
    # 쓰고 나면 바꾸지 않는 기록이라 updated_at이 없다.
    __tablename__ = "run_event"
    __table_args__ = (
        UniqueConstraint("run_id", "seq"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run.id"))
    seq: Mapped[int]
    type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONValue)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
