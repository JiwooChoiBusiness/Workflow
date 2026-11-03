import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import KbDocForm, KbIndexingTechnique
from app.db.types import JSONValue, enum_check


class KnowledgeBase(TimestampMixin, Base):
    # 지식 베이스(3단계 설계 4.1). JSON 열의 모양은 app/schemas/knowledge.py에 둔다.
    __tablename__ = "knowledge_base"
    __table_args__ = (
        Index(
            "uq_knowledge_base_workspace_id_slug",
            "workspace_id",
            "slug",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index(None, "workspace_id", "deleted_at"),
        enum_check("doc_form", KbDocForm),
        enum_check("indexing_technique", KbIndexingTechnique),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column(index=False)
    slug: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str | None] = mapped_column(String)
    # 만든 뒤 바꾸지 않는다.
    doc_form: Mapped[str] = mapped_column(String)
    # economy → high_quality로만 바꿀 수 있다.
    indexing_technique: Mapped[str] = mapped_column(String)
    # {model, dimensions, prefix, task_type, version}. economy면 null. 바꿀 때마다 version이 오르고
    # index.db의 kb_index.embedding_version과 맞춰 본다(5.4).
    embedding: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    # 새 문서의 기본 정리·청크 규칙. 문서는 만들 때의 값을 따로 가진다.
    process_rule: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    retrieval_model: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    search_while_indexing: Mapped[bool] = mapped_column(default=True)
    built_in_fields_enabled: Mapped[bool] = mapped_column(default=False)
    # {key_tier, acknowledged_at}.
    data_policy: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # 지우지 않은 문서와 그 청크 수. 목록 화면이 세지 않고 읽는다.
    document_count: Mapped[int] = mapped_column(default=0)
    segment_count: Mapped[int] = mapped_column(default=0)
    # 임베딩 응답에 usage가 없어 보낸 글의 추정 토큰을 더한다(3.5).
    embedded_tokens_estimated: Mapped[int] = mapped_column(default=0)
    deleted_at: Mapped[datetime | None]
