import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import KbDataSourceType, KbIndexingStatus
from app.db.types import JSONValue, enum_check


class KbDocument(TimestampMixin, Base):
    # 지식 베이스의 문서 하나(3단계 설계 4.1). 원본은 늘 저장소(kb-files/)에 있고 붙여 넣은 글도
    # .txt로 둔다. 화면의 display_status는 저장하지 않고 코드로 만든다(4.4).
    __tablename__ = "kb_document"
    __table_args__ = (
        Index(None, "kb_id", "position"),
        Index(None, "workspace_id", "indexing_status"),
        Index(None, "kb_id", "batch"),
        Index(None, "kb_id", "deleted_at"),
        enum_check("data_source_type", KbDataSourceType),
        enum_check("indexing_status", KbIndexingStatus),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column(index=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_base.id"))
    position: Mapped[int]
    name: Mapped[str] = mapped_column(String)
    data_source_type: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String)
    mime: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String)
    # 이 문서에 쓴 정리·청크 규칙의 스냅샷.
    process_rule: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # {mode, pages, chars, truncated}. 파싱하기 전에는 null이다.
    extract: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    indexing_status: Mapped[str] = mapped_column(String, default=KbIndexingStatus.WAITING)
    enabled: Mapped[bool] = mapped_column(default=True)
    archived: Mapped[bool] = mapped_column(default=False)
    # {code, message, retryable, retry_after}.
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    total_segments: Mapped[int] = mapped_column(default=0)
    completed_segments: Mapped[int] = mapped_column(default=0)
    word_count: Mapped[int] = mapped_column(default=0)
    # 로컬 추정 토큰(6.4).
    tokens: Mapped[int] = mapped_column(default=0)
    hit_count: Mapped[int] = mapped_column(default=0)
    # 같은 요청으로 올린 문서 묶음. indexing-status API가 이 값으로 묶어 보여 준다.
    batch: Mapped[str] = mapped_column(String)
    processing_started_at: Mapped[datetime | None]
    parsing_completed_at: Mapped[datetime | None]
    cleaning_completed_at: Mapped[datetime | None]
    splitting_completed_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]
    paused_at: Mapped[datetime | None]
    stopped_at: Mapped[datetime | None]
    # 지운 문서는 이 값만 찍고 지식 레인의 delete 작업이 행을 지운다(4.4, 6.5).
    deleted_at: Mapped[datetime | None]
