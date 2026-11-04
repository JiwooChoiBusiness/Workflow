import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, id_column, workspace_id_column
from app.db.models.enums import SqlQueryOrigin, SqlQueryStatus
from app.db.types import enum_check, utcnow


class SqlQueryLog(Base):
    # 검증·실행 기록(3단계 설계 4.1, 10.4). 거부도 사유 코드와 함께 남긴다. KB_QUERY_LOG_DAYS가
    # 지나면 지우고 0이면 남기지 않는다.
    __tablename__ = "sql_query_log"
    __table_args__ = (
        Index(None, "source_id", "created_at"),
        enum_check("origin", SqlQueryOrigin),
        enum_check("status", SqlQueryStatus),
    )

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sql_source.id"))
    origin: Mapped[str] = mapped_column(String)
    # 받은 SQL(코드 펜스를 벗긴 것). 8KB까지만 남긴다.
    sql: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String)
    reject_code: Mapped[str | None] = mapped_column(String)
    row_count: Mapped[int | None]
    duration_ms: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
