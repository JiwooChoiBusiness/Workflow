"""wf conversation: 챗플로우 대화와 메시지(2단계 설계 4.1, 4.4)

wf_conversation(대화, 대화 변수 현재값)과 wf_message(턴마다 user·assistant 두 행)를 만든다.
wf_run·wf_file의 conversation_id와 wf_run.message_id는 앞 리비전에서 만든 열이라 외래 키 없이 둔다.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 앱의 UUIDStr·UTCDateTime 대신 저장 형태로 적는다(0001과 같은 이유).
UUID = sa.String(length=36)
TIMESTAMP = sa.Text()
RUN_STATUSES = ("queued", "running", "waiting_input", "succeeded", "failed", "cancelled")


def _uuid(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, UUID, nullable=nullable)


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, TIMESTAMP, nullable=nullable)


def _str(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.String(), nullable=nullable)


def _check(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f'"{column}" IN ({allowed})', name=op.f(f"ck_{table}_{column}"))


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.id"], name=op.f(f"fk_{table}_{column}_{target}")
    )


def upgrade() -> None:
    op.create_table(
        "wf_conversation",
        _uuid("id"),
        _uuid("workspace_id"),
        _uuid("workflow_id"),
        _uuid("wf_version_id"),
        _str("source"),
        _uuid("api_key_id", nullable=True),
        _str("external_user", nullable=True),
        _str("title", nullable=True),
        _str("status"),
        sa.Column("conversation_vars", sa.JSON(), nullable=False),
        _ts("last_message_at", nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False),
        _ts("deleted_at", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _check("wf_conversation", "source", ("preview", "webapp", "api")),
        _check("wf_conversation", "status", ("active", "archived")),
        _fk("wf_conversation", "workspace_id", "workspace"),
        _fk("wf_conversation", "workflow_id", "workflow"),
        _fk("wf_conversation", "wf_version_id", "wf_version"),
        _fk("wf_conversation", "api_key_id", "api_key"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wf_conversation")),
    )
    op.create_index(
        "ix_wf_conversation_workspace_id_workflow_id_last_message_at",
        "wf_conversation",
        ["workspace_id", "workflow_id", sa.literal_column("last_message_at DESC")],
    )
    op.create_index(
        "ix_wf_conversation_api_key_id_external_user_last_message_at",
        "wf_conversation",
        ["api_key_id", "external_user", sa.literal_column("last_message_at DESC")],
    )

    op.create_table(
        "wf_message",
        _uuid("id"),
        _uuid("workspace_id"),
        _uuid("conversation_id"),
        _uuid("wf_run_id", nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        _str("role"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=True),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=True),
        _str("status", nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        _check("wf_message", "role", ("user", "assistant")),
        _check("wf_message", "status", RUN_STATUSES),
        _fk("wf_message", "workspace_id", "workspace"),
        _fk("wf_message", "conversation_id", "wf_conversation"),
        _fk("wf_message", "wf_run_id", "wf_run"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wf_message")),
        sa.UniqueConstraint(
            "conversation_id", "seq", name=op.f("uq_wf_message_conversation_id_seq")
        ),
    )
    op.create_index(op.f("ix_wf_message_workspace_id"), "wf_message", ["workspace_id"])


def downgrade() -> None:
    # 참조하는 쪽부터 지운다. 인덱스는 표와 함께 사라진다.
    op.drop_table("wf_message")
    op.drop_table("wf_conversation")
