import uuid
from typing import Any

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column
from app.db.types import JSONValue


class Workspace(TimestampMixin, Base):
    __tablename__ = "workspace"

    id: Mapped[uuid.UUID] = id_column()
    name: Mapped[str] = mapped_column(String)
    slug: Mapped[str] = mapped_column(String, unique=True)
    # 구조는 app/schemas/workspace.py의 WorkspaceSettings.
    settings: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
