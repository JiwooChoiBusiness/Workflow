"""initial: 1단계 테이블 전부(설계 5.1)

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 앱의 UUIDStr·UTCDateTime 대신 저장 형태로 적는다. 앱 타입이 바뀌어도 이 리비전은 그대로여야 한다.
UUID = sa.String(length=36)
TIMESTAMP = sa.Text()
APPROVAL_MODES = ("off", "http_writes", "all_shell")


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


def _pk(table: str, column: str = "id") -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint(column, name=op.f(f"pk_{table}"))


def _unique(table: str, *columns: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint(*columns, name=op.f(f"uq_{table}_{'_'.join(columns)}"))


def _index(table: str, *columns: str) -> None:
    op.create_index(op.f(f"ix_{table}_{'_'.join(columns)}"), table, list(columns))


def upgrade() -> None:
    op.create_table(
        "workspace",
        _id(),
        _str("name"),
        _str("slug"),
        _json("settings"),
        *_timestamps(),
        _pk("workspace"),
        _unique("workspace", "slug"),
    )

    op.create_table(
        "account",
        _id(),
        _str("email"),
        _str("name"),
        _str("password_hash"),
        _ts("last_login_at", nullable=True),
        *_timestamps(),
        _pk("account"),
        _unique("account", "email"),
    )

    op.create_table(
        "login_session",
        _str("token_hash", length=64),
        _uuid("account_id"),
        _ts("created_at"),
        _ts("expires_at"),
        _ts("last_seen_at"),
        _str("user_agent", nullable=True),
        _fk("login_session", "account_id", "account"),
        _pk("login_session", "token_hash"),
    )
    _index("login_session", "account_id")
    _index("login_session", "expires_at")

    op.create_table(
        "agent",
        _id(),
        _uuid("workspace_id"),
        _str("slug"),
        _str("name"),
        _str("icon", nullable=True),
        _text("description"),
        _uuid("draft_version_id", nullable=True),
        _uuid("published_version_id", nullable=True),
        _bool("webapp_enabled"),
        _ts("deleted_at", nullable=True),
        *_timestamps(),
        _fk("agent", "workspace_id", "workspace"),
        _pk("agent"),
        _unique("agent", "workspace_id", "slug"),
    )
    _index("agent", "workspace_id", "deleted_at")

    op.create_table(
        "agent_version",
        _id(),
        _uuid("workspace_id"),
        _uuid("agent_id"),
        _int("version_number", nullable=True),
        _str("status"),
        _text("instructions"),
        _json("model"),
        _json("tools"),
        _json("limits"),
        _json("sandbox"),
        _str("approval_mode"),
        _json("env_vars"),
        _json("skill_version_ids"),
        _json("file_ids"),
        _uuid("snapshot_id", nullable=True),
        _ts("published_at", nullable=True),
        _text("change_note", nullable=True),
        *_timestamps(),
        _check("agent_version", "status", ("draft", "published")),
        _check("agent_version", "approval_mode", APPROVAL_MODES),
        _fk("agent_version", "workspace_id", "workspace"),
        _fk("agent_version", "agent_id", "agent"),
        _pk("agent_version"),
        _unique("agent_version", "agent_id", "version_number"),
    )
    _index("agent_version", "workspace_id")
    # 초안(version_number IS NULL)은 에이전트당 1행.
    op.create_index(
        "uq_agent_version_agent_id_draft",
        "agent_version",
        ["agent_id"],
        unique=True,
        sqlite_where=sa.text("version_number IS NULL"),
    )

    op.create_table(
        "agent_file",
        _id(),
        _uuid("workspace_id"),
        _uuid("agent_id"),
        _str("filename"),
        _str("mime"),
        _int("size_bytes"),
        _str("sha256", length=64),
        _str("storage_key"),
        *_timestamps(),
        _fk("agent_file", "workspace_id", "workspace"),
        _fk("agent_file", "agent_id", "agent"),
        _pk("agent_file"),
    )
    _index("agent_file", "workspace_id")
    _index("agent_file", "agent_id")

    op.create_table(
        "api_key",
        _id(),
        _uuid("workspace_id"),
        _uuid("agent_id"),
        _str("name"),
        _str("key_prefix"),
        _str("key_hash", length=64),
        _json("scopes"),
        _ts("last_used_at", nullable=True),
        _ts("expires_at", nullable=True),
        _ts("revoked_at", nullable=True),
        *_timestamps(),
        _fk("api_key", "workspace_id", "workspace"),
        _fk("api_key", "agent_id", "agent"),
        _pk("api_key"),
        _unique("api_key", "key_hash"),
    )
    _index("api_key", "workspace_id")
    _index("api_key", "agent_id")

    op.create_table(
        "skill",
        _id(),
        _uuid("workspace_id"),
        _str("scope"),
        _uuid("agent_id", nullable=True),
        _str("slug"),
        _str("name"),
        _text("description"),
        _uuid("current_version_id", nullable=True),
        _ts("deleted_at", nullable=True),
        *_timestamps(),
        _check("skill", "scope", ("library", "embedded")),
        _fk("skill", "workspace_id", "workspace"),
        _fk("skill", "agent_id", "agent"),
        _pk("skill"),
    )
    # 라이브러리 스킬은 agent_id가 NULL이라 coalesce로 묶어야 유니크가 걸린다.
    # 표현식 인덱스는 autogenerate가 비교하지 못하므로 바꿀 때는 리비전에 손으로 적는다.
    op.create_index(
        "uq_skill_workspace_id_scope_agent_id_slug",
        "skill",
        ["workspace_id", "scope", sa.text("coalesce(agent_id, '')"), "slug"],
        unique=True,
    )

    op.create_table(
        "skill_version",
        _id(),
        _uuid("workspace_id"),
        _uuid("skill_id"),
        _int("version_number"),
        _json("manifest"),
        _str("storage_key"),
        _int("size_bytes"),
        _str("sha256", length=64),
        _text("skill_md"),
        *_timestamps(),
        _fk("skill_version", "workspace_id", "workspace"),
        _fk("skill_version", "skill_id", "skill"),
        _pk("skill_version"),
        _unique("skill_version", "skill_id", "version_number"),
    )
    _index("skill_version", "workspace_id")

    op.create_table(
        "session",
        _id(),
        _uuid("workspace_id"),
        _uuid("agent_id"),
        _uuid("agent_version_id"),
        _str("source"),
        _uuid("api_key_id", nullable=True),
        _str("external_user", nullable=True),
        _str("title", nullable=True),
        _str("status"),
        _uuid("workspace_instance_id", nullable=True),
        _str("approval_override", nullable=True),
        _str("prompt_hash", nullable=True, length=64),
        _json("metadata"),
        _ts("last_message_at", nullable=True),
        _int("message_count"),
        _ts("deleted_at", nullable=True),
        *_timestamps(),
        _check("session", "source", ("preview", "webapp", "api", "task")),
        _check("session", "status", ("active", "archived")),
        _check("session", "approval_override", ("off",)),
        _fk("session", "workspace_id", "workspace"),
        _fk("session", "agent_id", "agent"),
        _fk("session", "agent_version_id", "agent_version"),
        _fk("session", "api_key_id", "api_key"),
        _pk("session"),
    )
    op.create_index(
        "ix_session_workspace_id_agent_id_last_message_at",
        "session",
        ["workspace_id", "agent_id", sa.literal_column("last_message_at DESC")],
    )
    op.create_index(
        "ix_session_api_key_id_external_user_last_message_at",
        "session",
        ["api_key_id", "external_user", sa.literal_column("last_message_at DESC")],
    )

    op.create_table(
        "message",
        _id(),
        _uuid("workspace_id"),
        _uuid("session_id"),
        _uuid("run_id", nullable=True),
        _int("seq"),
        _str("role"),
        _json("blocks"),
        _text("text"),
        _str("stop_reason", nullable=True),
        _json("files"),
        _json("usage", nullable=True),
        *_timestamps(),
        _check("message", "role", ("user", "assistant", "system_summary")),
        _fk("message", "workspace_id", "workspace"),
        _fk("message", "session_id", "session"),
        _pk("message"),
        _unique("message", "session_id", "seq"),
    )
    _index("message", "workspace_id")

    op.create_table(
        "snapshot",
        _id(),
        _uuid("workspace_id"),
        _str("kind"),
        _uuid("agent_version_id", nullable=True),
        _uuid("session_id", nullable=True),
        _str("storage_key"),
        _int("size_bytes"),
        _int("raw_bytes"),
        _int("file_count"),
        _str("sha256", length=64),
        *_timestamps(),
        _check("snapshot", "kind", ("published",)),
        _fk("snapshot", "workspace_id", "workspace"),
        _fk("snapshot", "agent_version_id", "agent_version"),
        _fk("snapshot", "session_id", "session"),
        _pk("snapshot"),
    )
    _index("snapshot", "workspace_id")
    _index("snapshot", "agent_version_id")

    op.create_table(
        "run",
        _id(),
        _uuid("workspace_id"),
        _uuid("session_id"),
        _uuid("agent_version_id"),
        _str("trigger"),
        _str("status"),
        _uuid("user_message_id"),
        _uuid("assistant_message_id", nullable=True),
        _json("model"),
        _json("limits"),
        _str("approval_mode"),
        _json("input", nullable=True),
        _json("output", nullable=True),
        _json("pending_interaction", nullable=True),
        _json("resume_payload", nullable=True),
        _ts("cancel_requested_at", nullable=True),
        _json("error", nullable=True),
        _int("steps"),
        _json("usage"),
        _ts("queued_at"),
        _ts("started_at", nullable=True),
        _ts("ended_at", nullable=True),
        *_timestamps(),
        _check("run", "trigger", ("chat", "task", "resume")),
        _check(
            "run",
            "status",
            ("queued", "running", "waiting_input", "succeeded", "failed", "cancelled"),
        ),
        _check("run", "approval_mode", APPROVAL_MODES),
        _fk("run", "workspace_id", "workspace"),
        _fk("run", "session_id", "session"),
        _fk("run", "agent_version_id", "agent_version"),
        _fk("run", "user_message_id", "message"),
        _fk("run", "assistant_message_id", "message"),
        _pk("run"),
    )
    _index("run", "session_id", "queued_at")
    _index("run", "workspace_id", "status")
    op.create_index(
        "ix_run_workspace_id_queued_at",
        "run",
        ["workspace_id", sa.literal_column("queued_at DESC")],
    )

    op.create_table(
        "run_event",
        sa.Column("id", sa.Integer(), nullable=False),
        _uuid("workspace_id"),
        _uuid("run_id"),
        _int("seq"),
        _str("type"),
        _json("payload"),
        _ts("created_at"),
        _fk("run_event", "workspace_id", "workspace"),
        _fk("run_event", "run_id", "run"),
        _pk("run_event"),
        _unique("run_event", "run_id", "seq"),
        sqlite_autoincrement=True,
    )
    _index("run_event", "workspace_id")

    op.create_table(
        "session_file",
        _id(),
        _uuid("workspace_id"),
        _uuid("session_id"),
        _uuid("run_id", nullable=True),
        _uuid("message_id", nullable=True),
        _str("kind"),
        _str("path"),
        _str("filename"),
        _str("mime"),
        _int("size_bytes"),
        _str("sha256", length=64),
        _str("storage_key", nullable=True),
        _str("reason", nullable=True),
        *_timestamps(),
        _check("session_file", "kind", ("upload", "output")),
        _fk("session_file", "workspace_id", "workspace"),
        _fk("session_file", "session_id", "session"),
        _fk("session_file", "run_id", "run"),
        _fk("session_file", "message_id", "message"),
        _pk("session_file"),
    )
    _index("session_file", "workspace_id")
    _index("session_file", "session_id", "created_at")

    op.create_table(
        "workspace_instance",
        _id(),
        _uuid("workspace_id"),
        _uuid("session_id"),
        _str("backend"),
        _str("root_path"),
        _str("status"),
        _uuid("restored_from_snapshot_id", nullable=True),
        _int("disk_bytes"),
        _bool("disk_estimated"),
        _ts("disk_measured_at", nullable=True),
        _ts("last_activity_at"),
        *_timestamps(),
        _check("workspace_instance", "backend", ("local", "e2b")),
        _check("workspace_instance", "status", ("ready", "busy", "archived")),
        _fk("workspace_instance", "workspace_id", "workspace"),
        _fk("workspace_instance", "session_id", "session"),
        _fk("workspace_instance", "restored_from_snapshot_id", "snapshot"),
        _pk("workspace_instance"),
        _unique("workspace_instance", "session_id"),
    )
    _index("workspace_instance", "workspace_id")


def downgrade() -> None:
    # 참조하는 쪽부터 지운다. 인덱스는 테이블과 함께 사라진다.
    for table in (
        "workspace_instance",
        "session_file",
        "run_event",
        "run",
        "snapshot",
        "message",
        "session",
        "skill_version",
        "skill",
        "api_key",
        "agent_file",
        "agent_version",
        "agent",
        "login_session",
        "account",
        "workspace",
    ):
        op.drop_table(table)
