import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.models.enums import SqlApproval, SqlSourceKind
from app.db.types import JSONValue, enum_check


class SqlSource(TimestampMixin, Base):
    # Text2SQL 데이터 소스(3단계 설계 4.1, 10.2). JSON 열의 모양은 app/schemas/sql_source.py에 둔다.
    __tablename__ = "sql_source"
    __table_args__ = (
        Index(
            "uq_sql_source_workspace_id_slug",
            "workspace_id",
            "slug",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index(None, "workspace_id", "deleted_at"),
        enum_check("kind", SqlSourceKind),
        enum_check("approval", SqlApproval),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column(index=False)
    slug: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    # 만든 뒤 바꾸지 않는다.
    kind: Mapped[str] = mapped_column(String)
    # [{file_id, storage_key, filename, mime, size_bytes, sha256, table, sheet, options}].
    files: Mapped[list[dict[str, Any]]] = mapped_column(JSONValue, default=list)
    # 준비가 뜬 스냅샷. 숨긴 열도 이름과 타입은 남고 샘플값만 없다. 열 이름이 schema라 속성 이름을
    # 바꿔 둔다.
    table_schema: Mapped[dict[str, Any] | None] = mapped_column("schema", JSONValue)
    annotations: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    glossary: Mapped[str] = mapped_column(Text, default="")
    # {max_rows, timeout_sec, memory_mb, llm_rows, llm_bytes}. 노드 config가 없을 때와 도구가 쓴다.
    limits: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    approval: Mapped[str] = mapped_column(String)
    # {key_tier, acknowledged_at}.
    data_policy: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    # files 소스의 질의 파일 {path, built_at, source_hash, bytes}. 준비 전이거나 sqlite면 null.
    materialized: Mapped[dict[str, Any] | None] = mapped_column(JSONValue)
    deleted_at: Mapped[datetime | None]
