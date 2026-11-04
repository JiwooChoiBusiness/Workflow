"""sql_sources: Text2SQL 데이터 소스와 질의 기록(3단계 설계 4.1, 4.5, 10장)

sql_source와 sql_query_log를 만들고 agent_version에 sql_sources 열을 더한다. 열만 더하므로
agent_version은 다시 만들지 않고 기존 행은 NULL(데이터 소스 없음)이다.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 앱의 UUIDStr·UTCDateTime 대신 저장 형태로 적는다(0001과 같은 이유).
UUID = sa.String(length=36)
TIMESTAMP = sa.Text()
TABLES = ("sql_source", "sql_query_log")


def _check(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f'"{column}" IN ({allowed})', name=op.f(f"ck_{table}_{column}"))


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.id"], name=op.f(f"fk_{table}_{column}_{target}")
    )


def upgrade() -> None:
    op.create_table(
        "sql_source",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("files", sa.JSON(), nullable=False),
        sa.Column("schema", sa.JSON(), nullable=True),
        sa.Column("annotations", sa.JSON(), nullable=False),
        sa.Column("glossary", sa.Text(), nullable=False),
        sa.Column("limits", sa.JSON(), nullable=False),
        sa.Column("approval", sa.String(), nullable=False),
        sa.Column("data_policy", sa.JSON(), nullable=False),
        sa.Column("materialized", sa.JSON(), nullable=True),
        sa.Column("deleted_at", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("updated_at", TIMESTAMP, nullable=False),
        _check("sql_source", "kind", ("files", "sqlite")),
        _check("sql_source", "approval", ("none", "always")),
        _fk("sql_source", "workspace_id", "workspace"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sql_source")),
    )
    op.create_index(
        op.f("ix_sql_source_workspace_id_deleted_at"), "sql_source", ["workspace_id", "deleted_at"]
    )
    op.create_index(
        "uq_sql_source_workspace_id_slug",
        "sql_source",
        ["workspace_id", "slug"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "sql_query_log",
        sa.Column("id", UUID, nullable=False),
        sa.Column("workspace_id", UUID, nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("sql", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reject_code", sa.String(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        _check("sql_query_log", "origin", ("node", "tool", "test", "api")),
        _check("sql_query_log", "status", ("ok", "rejected", "error", "timeout")),
        _fk("sql_query_log", "workspace_id", "workspace"),
        _fk("sql_query_log", "source_id", "sql_source"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sql_query_log")),
    )
    op.create_index(op.f("ix_sql_query_log_workspace_id"), "sql_query_log", ["workspace_id"])
    op.create_index(
        op.f("ix_sql_query_log_source_id_created_at"), "sql_query_log", ["source_id", "created_at"]
    )

    op.add_column("agent_version", sa.Column("sql_sources", sa.JSON(), nullable=True))


def downgrade() -> None:
    # SQLite 3.35부터 DROP COLUMN이 된다.
    op.drop_column("agent_version", "sql_sources")
    # 참조하는 쪽부터 지운다. 인덱스는 테이블과 함께 사라진다.
    for table in reversed(TABLES):
        op.drop_table(table)
