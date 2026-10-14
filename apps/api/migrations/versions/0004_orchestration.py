"""orchestration: 하위 에이전트 호출(오케스트레이션 설계 7장)

run.parent_run_id(자식 런 → 부모 런, 외래 키 없음)와 그 인덱스, agent_version.sub_agents(SubAgentRef
목록)를 더한다. 열만 더하므로 테이블을 다시 만들지 않는다. 기존 행은 NULL과 []다.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARENT_INDEX = "ix_run_parent_run_id"


def upgrade() -> None:
    # 앱의 UUIDStr 대신 저장 형태(0001과 같은 이유).
    op.add_column("run", sa.Column("parent_run_id", sa.String(length=36), nullable=True))
    op.create_index(op.f(PARENT_INDEX), "run", ["parent_run_id"])
    # NOT NULL 열을 ALTER로 더하려면 SQLite가 기본값을 요구한다.
    op.add_column(
        "agent_version",
        sa.Column("sub_agents", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    # SQLite 3.35부터 DROP COLUMN이 된다. 인덱스가 걸린 열은 지울 수 없어 인덱스를 먼저 지운다.
    op.drop_index(op.f(PARENT_INDEX), table_name="run")
    op.drop_column("run", "parent_run_id")
    op.drop_column("agent_version", "sub_agents")
