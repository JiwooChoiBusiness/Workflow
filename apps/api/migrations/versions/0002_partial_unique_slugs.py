"""partial unique slugs: 지운 agent·skill의 slug를 다시 쓸 수 있게 한다(설계 12.2 M2 이후 확정)

agent의 UNIQUE(workspace_id, slug) 제약과 skill의 유니크 인덱스를 deleted_at IS NULL 조건의
부분 유니크 인덱스로 바꾼다.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AGENT_SLUG = "uq_agent_workspace_id_slug"
SKILL_SLUG = "uq_skill_workspace_id_scope_agent_id_slug"
ALIVE = sa.text("deleted_at IS NULL")
SKILL_COLUMNS = ["workspace_id", "scope", sa.text("coalesce(agent_id, '')"), "slug"]


def upgrade() -> None:
    # SQLite는 테이블 제약을 ALTER로 지우지 못해 batch가 테이블을 새로 만들어 옮긴다.
    with op.batch_alter_table("agent") as batch_op:
        batch_op.drop_constraint(AGENT_SLUG, type_="unique")
    op.create_index(AGENT_SLUG, "agent", ["workspace_id", "slug"], unique=True, sqlite_where=ALIVE)

    # 인덱스는 SQLite에서도 바로 지우고 만들 수 있다. 표현식 인덱스라 autogenerate가 비교하지
    # 못하므로 손으로 적는다.
    op.drop_index(SKILL_SLUG, table_name="skill")
    op.create_index(SKILL_SLUG, "skill", SKILL_COLUMNS, unique=True, sqlite_where=ALIVE)


def downgrade() -> None:
    # 지운 행과 살아 있는 행이 같은 slug를 쓰고 있으면 되돌릴 수 없다(유니크 위반으로 실패한다).
    op.drop_index(SKILL_SLUG, table_name="skill")
    op.create_index(SKILL_SLUG, "skill", SKILL_COLUMNS, unique=True)

    op.drop_index(AGENT_SLUG, table_name="agent")
    with op.batch_alter_table("agent") as batch_op:
        batch_op.create_unique_constraint(AGENT_SLUG, ["workspace_id", "slug"])
