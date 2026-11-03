import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import KbCreatedBy
from app.db.types import JSONValue, enum_check


class KbChildChunk(TimestampMixin, Base):
    # Parent-child 모드에서 부모 청크를 다시 나눈 검색 유닛(3단계 설계 4.1).
    __tablename__ = "kb_child_chunk"
    # document_id·kb_id 인덱스는 문서 삭제 작업(1,000행씩)과 KB 단위 정리·재임베딩이 쓴다.
    __table_args__ = (
        Index(None, "segment_id", "position"),
        UniqueConstraint("unit_no"),
        Index(None, "content_hash"),
        Index(None, "document_id"),
        Index(None, "kb_id"),
        enum_check("created_by", KbCreatedBy),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_base.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_document.id"))
    segment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_segment.id"))
    position: Mapped[int]
    unit_no: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    tokens: Mapped[int] = mapped_column(default=0)
    content_hash: Mapped[str] = mapped_column(String(64))
    # kb_segment.index_state와 같은 모양.
    index_state: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    created_by: Mapped[str] = mapped_column(String, default=KbCreatedBy.AUTOMATIC)
