import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, id_column, workspace_id_column
from app.db.types import JSONValue


def _default_scopes() -> list[str]:
    # 에이전트 키의 기본값. 워크플로우·지식 베이스 키는 서비스가 대상에 맞는 값을 늘 넣는다
    # (app/services/api_key_target.py).
    return ["chat", "tasks"]


# 키는 에이전트, 워크플로우, 지식 베이스 중 정확히 하나에 묶인다(2단계 4.1, 3단계 8.5). SQLite는
# 비교 결과가 0·1이라 더해서 센다.
TARGET_CHECK = (
    "(agent_id IS NOT NULL) + (workflow_id IS NOT NULL) + (knowledge_base_id IS NOT NULL) = 1"
)


class ApiKey(TimestampMixin, Base):
    __tablename__ = "api_key"
    __table_args__ = (CheckConstraint(TARGET_CHECK, name="target"),)

    id: Mapped[uuid.UUID] = id_column()
    workspace_id: Mapped[uuid.UUID] = workspace_id_column()
    agent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent.id"), index=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow.id"), index=True)
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_base.id"), index=True
    )
    name: Mapped[str] = mapped_column(String)
    # 목록 화면에서 키를 알아보게 하는 앞부분. 원문은 생성 응답에 한 번만 나간다.
    key_prefix: Mapped[str] = mapped_column(String)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSONValue, default=_default_scopes)
    last_used_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
