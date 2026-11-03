import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, id_column, workspace_id_column
from app.db.models.enums import KbQuerySource
from app.db.types import JSONValue, enum_check, utcnow


class KbQueryLog(Base):
    # 검색 질의 기록(3단계 설계 4.1, 7.7). KB_QUERY_LOG_DAYS가 지나면 지우고 0이면 남기지 않는다.
    __tablename__ = "kb_query_log"
    __table_args__ = (
        Index(None, "kb_id", "created_at"),
        enum_check("source", KbQuerySource),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_base.id"))
    source: Mapped[str] = mapped_column(String)
    query: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # 상위 20개 [{segment_id, score}].
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSONValue, default=list)
    result_count: Mapped[int] = mapped_column(default=0)
    latency_ms: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
