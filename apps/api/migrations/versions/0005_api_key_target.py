"""api key target: API 키를 에이전트 또는 워크플로우에 묶는다(2단계 설계 4.1, 4.4)

api_key.agent_id를 nullable로 바꾸고 workflow_id(외래 키·인덱스)를 더한 뒤, 둘 중 정확히 하나만
채워지도록 CHECK를 건다. SQLite는 열의 NOT NULL과 제약을 ALTER로 바꾸지 못해 batch가 표를 새로
만들어 옮긴다. 기존 행은 모두 에이전트 키라 그대로 CHECK를 통과한다.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 앱의 UUIDStr 대신 저장 형태(0001과 같은 이유).
UUID = sa.String(length=36)
TARGET_CHECK = "ck_api_key_target"
WORKFLOW_FK = "fk_api_key_workflow_id_workflow"
WORKFLOW_INDEX = "ix_api_key_workflow_id"


def upgrade() -> None:
    with op.batch_alter_table("api_key", recreate="always") as batch_op:
        batch_op.alter_column("agent_id", existing_type=UUID, nullable=True)
        batch_op.add_column(sa.Column("workflow_id", UUID, nullable=True))
        batch_op.create_foreign_key(op.f(WORKFLOW_FK), "workflow", ["workflow_id"], ["id"])
        batch_op.create_index(op.f(WORKFLOW_INDEX), ["workflow_id"])
        batch_op.create_check_constraint(
            op.f(TARGET_CHECK), "(agent_id IS NULL) <> (workflow_id IS NULL)"
        )


def downgrade() -> None:
    # 워크플로우 키는 옛 스키마에 자리가 없어 지운다. 그 키로 만든 런은 키 없이 남긴다.
    op.execute(
        "UPDATE wf_run SET api_key_id = NULL WHERE api_key_id IN "
        "(SELECT id FROM api_key WHERE workflow_id IS NOT NULL)"
    )
    op.execute("DELETE FROM api_key WHERE workflow_id IS NOT NULL")
    with op.batch_alter_table("api_key", recreate="always") as batch_op:
        batch_op.drop_constraint(op.f(TARGET_CHECK), type_="check")
        batch_op.drop_index(op.f(WORKFLOW_INDEX))
        batch_op.drop_constraint(op.f(WORKFLOW_FK), type_="foreignkey")
        batch_op.drop_column("workflow_id")
        batch_op.alter_column("agent_id", existing_type=UUID, nullable=False)
