"""knowledge: 지식 베이스 테이블과 인용·키 대상 열(3단계 설계 4.1, 4.5)

knowledge_base, kb_document, kb_segment, kb_child_chunk, kb_metadata_field, kb_document_metadata,
kb_eval_case, kb_eval_run, kb_query_log, kb_counter를 만든다. 벡터·전문 색인은 Alembic 밖의
index.db에 있다(5.5). agent_version.knowledge와 wf_run·wf_message·message.retriever_resources는
열만 더하고, api_key는 세 번째 대상 knowledge_base_id를 더하며 CHECK를 다시 쓰느라 batch로 다시
만든다. 기존 키는 모두 에이전트나 워크플로우 하나에 묶여 있어 새 CHECK를 그대로 통과한다.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 앱의 UUIDStr·UTCDateTime 대신 저장 형태로 적는다(0001과 같은 이유).
UUID = sa.String(length=36)
TIMESTAMP = sa.Text()
OLD_TARGET = "(agent_id IS NULL) <> (workflow_id IS NULL)"
NEW_TARGET = (
    "(agent_id IS NOT NULL) + (workflow_id IS NOT NULL) + (knowledge_base_id IS NOT NULL) = 1"
)
TARGET_CHECK = "ck_api_key_target"
KB_FK = "fk_api_key_knowledge_base_id_knowledge_base"
KB_INDEX = "ix_api_key_knowledge_base_id"
RETRIEVER_TABLES = ("wf_run", "wf_message", "message")
TABLES = (
    "knowledge_base",
    "kb_document",
    "kb_segment",
    "kb_child_chunk",
    "kb_metadata_field",
    "kb_document_metadata",
    "kb_eval_case",
    "kb_eval_run",
    "kb_query_log",
    "kb_counter",
)


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


def _kb_columns(table: str) -> list[sa.Column | sa.ForeignKeyConstraint]:
    return [
        _uuid("workspace_id"),
        _uuid("kb_id"),
        _fk(table, "workspace_id", "workspace"),
        _fk(table, "kb_id", "knowledge_base"),
    ]


def upgrade() -> None:
    op.create_table(
        "knowledge_base",
        _id(),
        _uuid("workspace_id"),
        _str("slug"),
        _str("name"),
        _text("description"),
        _str("icon", nullable=True),
        _str("doc_form"),
        _str("indexing_technique"),
        _json("embedding", nullable=True),
        _json("process_rule"),
        _json("retrieval_model"),
        _bool("search_while_indexing"),
        _bool("built_in_fields_enabled"),
        _json("data_policy"),
        _int("document_count"),
        _int("segment_count"),
        _int("embedded_tokens_estimated"),
        _ts("deleted_at", nullable=True),
        *_timestamps(),
        _check("knowledge_base", "doc_form", ("text_model", "hierarchical_model", "qa_model")),
        _check("knowledge_base", "indexing_technique", ("high_quality", "economy")),
        _fk("knowledge_base", "workspace_id", "workspace"),
        _pk("knowledge_base"),
    )
    _index("knowledge_base", "workspace_id", "deleted_at")
    op.create_index(
        "uq_knowledge_base_workspace_id_slug",
        "knowledge_base",
        ["workspace_id", "slug"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "kb_document",
        _id(),
        *_kb_columns("kb_document"),
        _int("position"),
        _str("name"),
        _str("data_source_type"),
        _str("filename"),
        _str("mime"),
        _int("size_bytes"),
        _str("sha256", length=64),
        _str("storage_key"),
        _json("process_rule"),
        _json("extract", nullable=True),
        _str("indexing_status"),
        _bool("enabled"),
        _bool("archived"),
        _json("error", nullable=True),
        _int("total_segments"),
        _int("completed_segments"),
        _int("word_count"),
        _int("tokens"),
        _int("hit_count"),
        _str("batch"),
        _ts("processing_started_at", nullable=True),
        _ts("parsing_completed_at", nullable=True),
        _ts("cleaning_completed_at", nullable=True),
        _ts("splitting_completed_at", nullable=True),
        _ts("completed_at", nullable=True),
        _ts("paused_at", nullable=True),
        _ts("stopped_at", nullable=True),
        _ts("deleted_at", nullable=True),
        *_timestamps(),
        _check("kb_document", "data_source_type", ("upload_file", "paste_text", "workflow", "api")),
        _check(
            "kb_document",
            "indexing_status",
            (
                "waiting",
                "parsing",
                "cleaning",
                "splitting",
                "indexing",
                "completed",
                "error",
                "paused",
            ),
        ),
        _pk("kb_document"),
    )
    _index("kb_document", "kb_id", "position")
    _index("kb_document", "workspace_id", "indexing_status")
    _index("kb_document", "kb_id", "batch")
    _index("kb_document", "kb_id", "deleted_at")

    op.create_table(
        "kb_segment",
        _id(),
        *_kb_columns("kb_segment"),
        _uuid("document_id"),
        _int("position"),
        _int("unit_no", nullable=True),
        _text("content"),
        _text("answer", nullable=True),
        _int("word_count"),
        _int("tokens"),
        _str("content_hash", length=64),
        _bool("enabled"),
        _str("status"),
        _json("index_state"),
        _str("created_by"),
        _bool("edited"),
        _int("hit_count"),
        _json("error", nullable=True),
        *_timestamps(),
        _check("kb_segment", "status", ("waiting", "indexing", "completed", "error")),
        _check("kb_segment", "created_by", ("automatic", "customized")),
        _fk("kb_segment", "document_id", "kb_document"),
        _pk("kb_segment"),
        _unique("kb_segment", "document_id", "position"),
        _unique("kb_segment", "unit_no"),
    )
    _index("kb_segment", "workspace_id")
    _index("kb_segment", "kb_id", "enabled")
    _index("kb_segment", "content_hash")

    op.create_table(
        "kb_child_chunk",
        _id(),
        *_kb_columns("kb_child_chunk"),
        _uuid("document_id"),
        _uuid("segment_id"),
        _int("position"),
        _int("unit_no"),
        _text("content"),
        _int("tokens"),
        _str("content_hash", length=64),
        _json("index_state"),
        _str("created_by"),
        *_timestamps(),
        _check("kb_child_chunk", "created_by", ("automatic", "customized")),
        _fk("kb_child_chunk", "document_id", "kb_document"),
        _fk("kb_child_chunk", "segment_id", "kb_segment"),
        _pk("kb_child_chunk"),
        _unique("kb_child_chunk", "unit_no"),
    )
    _index("kb_child_chunk", "workspace_id")
    _index("kb_child_chunk", "segment_id", "position")
    _index("kb_child_chunk", "content_hash")
    # 문서 삭제(1,000행씩)와 KB 단위 정리가 자식 행 전체를 훑지 않게 한다.
    _index("kb_child_chunk", "document_id")
    _index("kb_child_chunk", "kb_id")

    op.create_table(
        "kb_metadata_field",
        _id(),
        *_kb_columns("kb_metadata_field"),
        _str("name"),
        _str("type"),
        _bool("built_in"),
        _int("position"),
        _ts("created_at"),
        _check("kb_metadata_field", "type", ("string", "number", "time")),
        _pk("kb_metadata_field"),
        _unique("kb_metadata_field", "kb_id", "name"),
    )
    _index("kb_metadata_field", "workspace_id")

    op.create_table(
        "kb_document_metadata",
        _id(),
        *_kb_columns("kb_document_metadata"),
        _uuid("document_id"),
        _uuid("field_id"),
        _text("value_string", nullable=True),
        sa.Column("value_number", sa.Float(), nullable=True),
        _ts("value_time", nullable=True),
        _fk("kb_document_metadata", "document_id", "kb_document"),
        _fk("kb_document_metadata", "field_id", "kb_metadata_field"),
        _pk("kb_document_metadata"),
        _unique("kb_document_metadata", "document_id", "field_id"),
    )
    _index("kb_document_metadata", "workspace_id")
    for column in ("value_string", "value_number", "value_time"):
        _index("kb_document_metadata", "kb_id", "field_id", column)

    op.create_table(
        "kb_eval_case",
        _id(),
        *_kb_columns("kb_eval_case"),
        _text("question"),
        _json("expected"),
        _text("note", nullable=True),
        _ts("created_at"),
        _pk("kb_eval_case"),
    )
    _index("kb_eval_case", "workspace_id")
    _index("kb_eval_case", "kb_id", "created_at")

    op.create_table(
        "kb_eval_run",
        _id(),
        *_kb_columns("kb_eval_run"),
        _json("settings"),
        _json("metrics"),
        _json("cases"),
        _int("duration_ms"),
        _ts("created_at"),
        _pk("kb_eval_run"),
    )
    _index("kb_eval_run", "workspace_id")
    _index("kb_eval_run", "kb_id", "created_at")

    op.create_table(
        "kb_query_log",
        _id(),
        *_kb_columns("kb_query_log"),
        _str("source"),
        _text("query"),
        _json("settings"),
        _json("results"),
        _int("result_count"),
        _int("latency_ms"),
        _ts("created_at"),
        _check("kb_query_log", "source", ("hit_test", "node", "tool", "api", "eval")),
        _pk("kb_query_log"),
    )
    _index("kb_query_log", "workspace_id")
    _index("kb_query_log", "kb_id", "created_at")

    op.create_table(
        "kb_counter",
        _str("name"),
        _int("value"),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_kb_counter")),
    )
    op.execute("INSERT INTO kb_counter (name, value) VALUES ('unit_no', 0)")

    # 열만 더하므로 테이블을 다시 만들지 않는다. 기존 행은 NULL이다.
    op.add_column("agent_version", sa.Column("knowledge", sa.JSON(), nullable=True))
    for table in RETRIEVER_TABLES:
        op.add_column(table, sa.Column("retriever_resources", sa.JSON(), nullable=True))

    with op.batch_alter_table("api_key", recreate="always") as batch_op:
        batch_op.drop_constraint(op.f(TARGET_CHECK), type_="check")
        batch_op.add_column(sa.Column("knowledge_base_id", UUID, nullable=True))
        batch_op.create_foreign_key(op.f(KB_FK), "knowledge_base", ["knowledge_base_id"], ["id"])
        batch_op.create_index(op.f(KB_INDEX), ["knowledge_base_id"])
        batch_op.create_check_constraint(op.f(TARGET_CHECK), NEW_TARGET)


def downgrade() -> None:
    # 지식 베이스 키는 옛 스키마에 자리가 없어 지운다. 그 키로 만든 런·대화는 없다.
    op.execute("DELETE FROM api_key WHERE knowledge_base_id IS NOT NULL")
    with op.batch_alter_table("api_key", recreate="always") as batch_op:
        batch_op.drop_constraint(op.f(TARGET_CHECK), type_="check")
        batch_op.drop_index(op.f(KB_INDEX))
        batch_op.drop_constraint(op.f(KB_FK), type_="foreignkey")
        batch_op.drop_column("knowledge_base_id")
        batch_op.create_check_constraint(op.f(TARGET_CHECK), OLD_TARGET)
    # SQLite 3.35부터 DROP COLUMN이 된다.
    for table in RETRIEVER_TABLES:
        op.drop_column(table, "retriever_resources")
    op.drop_column("agent_version", "knowledge")
    # 참조하는 쪽부터 지운다. 인덱스는 테이블과 함께 사라진다.
    for table in reversed(TABLES):
        op.drop_table(table)
