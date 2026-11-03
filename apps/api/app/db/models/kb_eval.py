import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, id_column, workspace_id_column
from app.db.types import JSONValue, utcnow


class KbEvalCase(Base):
    # 평가셋의 질문 하나와 정답(3단계 설계 4.1, 7.7). 정답은 문서나 청크 단위다.
    __tablename__ = "kb_eval_case"
    __table_args__ = (Index(None, "kb_id", "created_at"),)

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_base.id"))
    question: Mapped[str] = mapped_column(Text)
    # {document_ids[], segment_ids[]}.
    expected: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KbEvalRun(Base):
    # 평가 실행 결과(확장 범위). KB마다 최근 30개만 남긴다(4.5).
    __tablename__ = "kb_eval_run"
    __table_args__ = (Index(None, "kb_id", "created_at"),)

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    kb_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_base.id"))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # {r1, r3, r5, mrr10, cases}.
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # [{case_id, rank, top[]}].
    cases: Mapped[list[dict[str, Any]]] = mapped_column(JSONValue, default=list)
    duration_ms: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
