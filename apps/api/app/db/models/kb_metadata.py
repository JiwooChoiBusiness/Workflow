import uuid
from datetime import datetime

from sqlalchemy import Float, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, id_column, workspace_id_column
from app.db.models.enums import KbMetadataType
from app.db.types import enum_check, utcnow


class KbMetadataField(Base):
    # 지식 베이스의 메타데이터 필드(3단계 설계 4.1, 7.5). 타입은 바꿀 수 없고 이름만 바꾼다.
    __tablename__ = "kb_metadata_field"
    __table_args__ = (
        UniqueConstraint("kb_id", "name"),
        enum_check("type", KbMetadataType),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_base.id"))
    name: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String)
    # 기본 필드(document_name 등)는 시스템이 채우고 사용자는 고치지 못한다.
    built_in: Mapped[bool] = mapped_column(default=False)
    position: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KbDocumentMetadata(Base):
    # 문서마다 필드 값 하나. 검색 필터가 SQL 조건으로 바뀌므로 JSON이 아니라 행으로 둔다(7.5).
    # 값은 필드 타입에 맞는 열 하나에만 있다.
    __tablename__ = "kb_document_metadata"
    __table_args__ = (
        UniqueConstraint("document_id", "field_id"),
        Index(None, "kb_id", "field_id", "value_string"),
        Index(None, "kb_id", "field_id", "value_number"),
        Index(None, "kb_id", "field_id", "value_time"),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_base.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_document.id"))
    field_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_metadata_field.id"))
    value_string: Mapped[str | None] = mapped_column(Text)
    value_number: Mapped[float | None] = mapped_column(Float)
    value_time: Mapped[datetime | None]
