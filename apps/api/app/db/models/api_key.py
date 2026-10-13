import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.types import JSONValue


def _default_scopes() -> list[str]:
    return ["chat", "tasks"]


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_key"

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    # 목록 화면에서 키를 알아보게 하는 앞부분. 원문은 생성 응답에 한 번만 나간다.
    key_prefix: Mapped[str] = mapped_column(String)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSONValue, default=_default_scopes)
    last_used_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
