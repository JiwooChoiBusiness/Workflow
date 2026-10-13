import uuid
from typing import Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.types import JSONValue


class SkillVersion(TimestampMixin, Base):
    __tablename__ = "skill_version"
    __table_args__ = (UniqueConstraint("skill_id", "version_number"),)

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skill.id"))
    version_number: Mapped[int]
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONValue)
    storage_key: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    # SKILL.md 본문 캐시. 원본은 storage_key의 zip이다.
    skill_md: Mapped[str] = mapped_column(Text)
