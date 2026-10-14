"""workflow core: 2단계 워크플로우 정의·실행 테이블(2단계 설계 4.1, 4.4)

workflow, wf_version, wf_run, wf_node_run, wf_run_event, wf_file. 챗플로우 대화 테이블과
api_key 변경은 뒤 리비전이다. wf_run·wf_file의 conversation_id는 그 테이블이 없어 외래 키가 없다.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 앱의 UUIDStr·UTCDateTime 대신 저장 형태로 적는다(0001과 같은 이유).
UUID = sa.String(length=36)
TIMESTAMP = sa.Text()
RUN_STATUSES = ("queued", "running", "waiting_input", "succeeded", "failed", "cancelled")
KINDS = ("workflow", "chatflow")


def _id() -> sa.Column:
    return sa.Column("id", UUID, nullable=False)


def _uuid(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, UUID, nullable=nullable)


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, TIMESTAMP, nullable=nullable)


def _json(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.JSON(), nullable=nullable)


def _str(name: str, *, nullable: bool = False, length: int | None = None) -> sa.Column:
    return sa.Column(name, sa.String(length=length), nullable=nullable)


def _text(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.Text(), nullable=nullable)


def _int(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=nullable)


def _bool(name: str) -> sa.Column:
    return sa.Column(name, sa.Boolean(), nullable=False)


def _timestamps() -> list[sa.Column]:
    return [_ts("created_at"), _ts("updated_at")]


def _check(table: str, column: str, values: Sequence[str]) -> sa.CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f'"{column}" IN ({allowed})', name=op.f(f"ck_{table}_{column}"))


def _fk(table: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.id"], name=op.f(f"fk_{table}_{column}_{target}")
    )


def _pk(table: str) -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}"))


def _unique(table: str, *columns: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint(*columns, name=op.f(f"uq_{table}_{'_'.join(columns)}"))


def _index(table: str, *columns: str) -> None:
    op.create_index(op.f(f"ix_{table}_{'_'.join(columns)}"), table, list(columns))


def upgrade() -> None:
    op.create_table(
        "workflow",
        _id(),
        _uuid("workspace_id"),
        _str("slug"),
        _str("name"),
        _str("icon", nullable=True),
        _text("description"),
        _str("kind"),
        _uuid("draft_version_id", nullable=True),
        _uuid("published_version_id", nullable=True),
        _bool("webapp_enabled"),
        _ts("deleted_at", nullable=True),
        *_timestamps(),
        _check("workflow", "kind", KINDS),
        _fk("workflow", "workspace_id", "workspace"),
        _pk("workflow"),
    )
    _index("workflow", "workspace_id", "deleted_at")
    op.create_index(
        "uq_workflow_workspace_id_slug",
        "workflow",
        ["workspace_id", "slug"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "wf_version",
        _id(),
        _uuid("workspace_id"),
        _uuid("workflow_id"),
        _int("version_number", nullable=True),
        _str("status"),
        _json("graph"),
        _json("env_vars"),
        _json("conversation_vars"),
        _json("settings"),
        _str("graph_hash", length=64),
        _ts("published_at", nullable=True),
        _text("change_note", nullable=True),
        *_timestamps(),
        _check("wf_version", "status", ("draft", "published")),
        _fk("wf_version", "workspace_id", "workspace"),
        _fk("wf_version", "workflow_id", "workflow"),
        _pk("wf_version"),
        _unique("wf_version", "workflow_id", "version_number"),
    )
    _index("wf_version", "workspace_id")
    op.create_index(
        "uq_wf_version_workflow_id_draft",
        "wf_version",
        ["workflow_id"],
        unique=True,
        sqlite_where=sa.text("version_number IS NULL"),
    )

    op.create_table(
        "wf_run",
        _id(),
        _uuid("workspace_id"),
        _uuid("workflow_id"),
        _uuid("wf_version_id"),
        _str("kind"),
        _str("trigger"),
        _str("status"),
        _uuid("api_key_id", nullable=True),
        _str("external_user", nullable=True),
        _uuid("conversation_id", nullable=True),
        _uuid("message_id", nullable=True),
        _json("inputs"),
        _json("outputs", nullable=True),
        _json("state", nullable=True),
        _json("pending_interaction", nullable=True),
        _json("resume_payload", nullable=True),
        _ts("cancel_requested_at", nullable=True),
        _json("error", nullable=True),
        _json("usage"),
        _int("node_run_count"),
        _json("limits"),
        _str("graph_hash", length=64),
        _ts("queued_at"),
        _ts("started_at", nullable=True),
        _ts("ended_at", nullable=True),
        *_timestamps(),
        _check("wf_run", "kind", KINDS),
        _check("wf_run", "trigger", ("preview", "webapp", "api", "single_node", "resume")),
        _check("wf_run", "status", RUN_STATUSES),
        _fk("wf_run", "workspace_id", "workspace"),
        _fk("wf_run", "workflow_id", "workflow"),
        _fk("wf_run", "wf_version_id", "wf_version"),
        _fk("wf_run", "api_key_id", "api_key"),
        _pk("wf_run"),
    )
    _index("wf_run", "workspace_id", "status")
    _index("wf_run", "conversation_id", "queued_at")
    op.create_index(
        "ix_wf_run_workspace_id_workflow_id_queued_at",
        "wf_run",
        ["workspace_id", "workflow_id", sa.literal_column("queued_at DESC")],
    )

    op.create_table(
        "wf_node_run",
        _id(),
        _uuid("workspace_id"),
        _uuid("wf_run_id"),
        _str("node_id"),
        _str("node_type"),
        _str("title"),
        _int("index"),
        _str("iteration_node_id", nullable=True),
        _int("iteration_index", nullable=True),
        _int("attempt"),
        _str("status"),
        _json("inputs", nullable=True),
        _json("outputs", nullable=True),
        _bool("truncated"),
        _json("process"),
        _json("error", nullable=True),
        _json("usage", nullable=True),
        _json("warnings"),
        _uuid("child_run_id", nullable=True),
        _str("branch", nullable=True),
        _ts("started_at"),
        _ts("ended_at", nullable=True),
        _int("duration_ms", nullable=True),
        *_timestamps(),
        _check(
            "wf_node_run",
            "status",
            ("running", "succeeded", "failed", "skipped", "cancelled", "waiting_input"),
        ),
        _fk("wf_node_run", "workspace_id", "workspace"),
        _fk("wf_node_run", "wf_run_id", "wf_run"),
        _fk("wf_node_run", "child_run_id", "run"),
        _pk("wf_node_run"),
    )
    _index("wf_node_run", "workspace_id")
    _index("wf_node_run", "wf_run_id", "index")
    _index("wf_node_run", "child_run_id")

    op.create_table(
        "wf_run_event",
        sa.Column("id", sa.Integer(), nullable=False),
        _uuid("workspace_id"),
        _uuid("wf_run_id"),
        _int("seq"),
        _str("type"),
        _json("payload"),
        _ts("created_at"),
        _fk("wf_run_event", "workspace_id", "workspace"),
        _fk("wf_run_event", "wf_run_id", "wf_run"),
        _pk("wf_run_event"),
        _unique("wf_run_event", "wf_run_id", "seq"),
        sqlite_autoincrement=True,
    )
    _index("wf_run_event", "workspace_id")

    op.create_table(
        "wf_file",
        _id(),
        _uuid("workspace_id"),
        _str("kind"),
        _uuid("workflow_id", nullable=True),
        _uuid("wf_run_id", nullable=True),
        _uuid("conversation_id", nullable=True),
        _str("filename"),
        _str("mime"),
        _int("size_bytes"),
        _str("sha256", length=64),
        _str("storage_key"),
        _ts("created_at"),
        _check("wf_file", "kind", ("upload", "output")),
        _fk("wf_file", "workspace_id", "workspace"),
        _fk("wf_file", "workflow_id", "workflow"),
        _fk("wf_file", "wf_run_id", "wf_run"),
        _pk("wf_file"),
    )
    _index("wf_file", "workspace_id")
    _index("wf_file", "wf_run_id")
    _index("wf_file", "conversation_id")


def downgrade() -> None:
    # 참조하는 쪽부터 지운다. 인덱스는 테이블과 함께 사라진다.
    for table in ("wf_file", "wf_run_event", "wf_node_run", "wf_run", "wf_version", "workflow"):
        op.drop_table(table)
