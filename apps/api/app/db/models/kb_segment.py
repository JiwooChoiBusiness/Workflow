import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import KbCreatedBy, KbSegmentStatus
from app.db.types import JSONValue, enum_check


class KbSegment(TimestampMixin, Base):
    # 문서를 나눈 청크(3단계 설계 4.1). General·Q&A에서는 검색 유닛이고 Parent-child에서는 돌려주는
    # 부모다. 유닛이면 unit_no가 index.db의 rowid이고 부모면 null이다.
    __tablename__ = "kb_segment"
    __table_args__ = (
        UniqueConstraint("document_id", "position"),
        UniqueConstraint("unit_no"),
        Index(None, "kb_id", "enabled"),
        Index(None, "content_hash"),
        enum_check("status", KbSegmentStatus),
        enum_check("created_by", KbCreatedBy),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_base.id"))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_document.id"))
    position: Mapped[int]
    unit_no: Mapped[int | None]
    content: Mapped[str] = mapped_column(Text)
    # Q&A 모드의 답. 임베딩은 질문(content)만 한다.
    answer: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(default=0)
    tokens: Mapped[int] = mapped_column(default=0)
    # 임베딩 입력 원문(content)의 sha256. 임베딩 캐시의 키다(6.6).
    content_hash: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String, default=KbSegmentStatus.WAITING)
    # {fts, vector, embedding_version, embedded_at}. app.db가 진실이고 index.db에 쓰기 전에
    # pending, 쓴 뒤에 ok를 적는다(5.4). 유닛이 아닌 부모는 둘 다 skip이다.
    index_state: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    created_by: Mapped[str] = mapped_column(String, default=KbCreatedBy.AUTOMATIC)
    edited: Mapped[bool] = mapped_column(default=False)
    hit_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
