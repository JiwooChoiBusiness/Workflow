"""0007_knowledge: 지식 베이스 테이블과 api_key 세 번째 대상(3단계 설계 4.1, 4.5).

0006의 데이터가 그대로 남고, api_key의 CHECK가 대상 셋 중 정확히 하나를 요구하며, 내리면
옛 스키마로 돌아가는지 본다.
"""

import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app.db.engine import Database
from app.db.migrate import alembic_config, current_revision, run_in_transaction

STAMP = "2026-11-02T00:00:00.000000Z"
WS = "00000000-0000-7000-8000-000000000001"
AGENT = "00000000-0000-7000-8000-000000000002"
WORKFLOW = "00000000-0000-7000-8000-000000000003"
VERSION = "00000000-0000-7000-8000-000000000004"
KB_TABLES = {
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
}


async def migrate_to(db: Database, revision: str, *, down: bool = False) -> None:
    from alembic import command

    config = alembic_config()

    def run(connection: Connection) -> None:
        config.attributes["connection"] = connection
        step = command.downgrade if down else command.upgrade
        run_in_transaction(connection, lambda: step(config, revision))

    async with db.write_connection() as conn:
        await conn.run_sync(run)


async def execute(db: Database, sql: str, **params: Any) -> None:
    async with db.write() as session:
        await session.execute(text(sql), params)


async def rows(db: Database, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async with db.read() as session:
        return [tuple(row) for row in (await session.execute(text(sql), params)).all()]


def table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        found = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {name for (name,) in found}


def columns(path: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')")}


async def insert_key(
    db: Database,
    *,
    agent: str | None = None,
    workflow: str | None = None,
    kb: str | None = None,
    scopes: str = "[]",
) -> str:
    # knowledge_base_id 열은 0007부터 있어 값이 있을 때만 넣는다.
    key_id = str(uuid.uuid4())
    statement = (
        "INSERT INTO api_key (id, workspace_id, agent_id, workflow_id, name, key_prefix,"
        " key_hash, scopes, created_at, updated_at)"
        " VALUES (:id, :ws, :agent, :workflow, 'k', 'sk-wf-x', :hash, :scopes, :ts, :ts)"
    )
    if kb is not None:
        statement = (
            "INSERT INTO api_key (id, workspace_id, agent_id, workflow_id, knowledge_base_id,"
            " name, key_prefix, key_hash, scopes, created_at, updated_at)"
            " VALUES (:id, :ws, :agent, :workflow, :kb, 'k', 'sk-wf-x', :hash, :scopes, :ts, :ts)"
        )
    await execute(
        db,
        statement,
        id=key_id,
        ws=WS,
        agent=agent,
        workflow=workflow,
        kb=kb,
        hash=uuid.uuid4().hex * 2,
        scopes=scopes,
        ts=STAMP,
    )
    return key_id


async def insert_kb(db: Database, kb_id: str, slug: str, *, deleted: bool = False) -> None:
    await execute(
        db,
        "INSERT INTO knowledge_base (id, workspace_id, slug, name, description, doc_form,"
        " indexing_technique, embedding, process_rule, retrieval_model, search_while_indexing,"
        " built_in_fields_enabled, data_policy, document_count, segment_count,"
        " embedded_tokens_estimated, deleted_at, created_at, updated_at)"
        " VALUES (:id, :ws, :slug, :slug, '', 'text_model', 'economy', NULL, '{}', '{}', 1, 0,"
        " '{}', 0, 0, 0, :deleted, :ts, :ts)",
        id=kb_id,
        ws=WS,
        slug=slug,
        deleted=STAMP if deleted else None,
        ts=STAMP,
    )


@pytest.fixture
async def at_0006(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "app.db")
    await migrate_to(db, "0006")
    await execute(
        db,
        "INSERT INTO workspace (id, name, slug, settings, created_at, updated_at)"
        " VALUES (:id, 'w', 'w', '{}', :ts, :ts)",
        id=WS,
        ts=STAMP,
    )
    await execute(
        db,
        "INSERT INTO agent (id, workspace_id, slug, name, description, webapp_enabled,"
        " created_at, updated_at) VALUES (:id, :ws, 'helper', 'helper', '', 0, :ts, :ts)",
        id=AGENT,
        ws=WS,
        ts=STAMP,
    )
    await execute(
        db,
        "INSERT INTO workflow (id, workspace_id, slug, name, description, kind, webapp_enabled,"
        " created_at, updated_at) VALUES (:id, :ws, 'flow', 'flow', '', 'chatflow', 0, :ts, :ts)",
        id=WORKFLOW,
        ws=WS,
        ts=STAMP,
    )
    await execute(
        db,
        "INSERT INTO agent_version (id, workspace_id, agent_id, version_number, status,"
        " instructions, model, tools, limits, sandbox, approval_mode, env_vars,"
        " skill_version_ids, file_ids, created_at, updated_at)"
        " VALUES (:id, :ws, :agent, NULL, 'draft', '', '{}', '[]', '{}', '{}', 'all_shell', '[]',"
        " '[]', '[]', :ts, :ts)",
        id=VERSION,
        ws=WS,
        agent=AGENT,
        ts=STAMP,
    )
    try:
        yield db
    finally:
        await db.close()


async def test_upgrade_keeps_rows_and_adds_empty_columns(at_0006: Database) -> None:
    db = at_0006
    agent_key = await insert_key(db, agent=AGENT, scopes='["chat"]')
    flow_key = await insert_key(db, workflow=WORKFLOW, scopes='["chat"]')

    await migrate_to(db, "0007")

    assert await current_revision(db) == "0007"
    assert sorted(
        await rows(db, "SELECT id, agent_id, workflow_id, knowledge_base_id FROM api_key")
    ) == sorted([(agent_key, AGENT, None, None), (flow_key, None, WORKFLOW, None)])
    assert await rows(db, "SELECT knowledge FROM agent_version") == [(None,)]
    assert await rows(db, "SELECT name, value FROM kb_counter") == [("unit_no", 0)]
    await db.close()
    assert KB_TABLES <= table_names(db.path)
    for table in ("wf_run", "wf_message", "message"):
        assert "retriever_resources" in columns(db.path, table)
    assert "knowledge" in columns(db.path, "agent_version")


async def test_api_key_needs_exactly_one_target(at_0006: Database) -> None:
    db = at_0006
    await migrate_to(db, "0007")
    kb = "00000000-0000-7000-8000-000000000010"
    await insert_kb(db, kb, "docs")

    await insert_key(db, kb=kb, scopes='["knowledge"]')

    for targets in ({}, {"agent": AGENT, "kb": kb}, {"workflow": WORKFLOW, "kb": kb}):
        with pytest.raises(IntegrityError):
            await insert_key(db, **targets)
    # 지식 베이스 외래 키도 걸린다.
    with pytest.raises(IntegrityError):
        await insert_key(db, kb="00000000-0000-7000-8000-0000000000ff")


async def test_kb_slug_is_unique_only_among_live_rows(at_0006: Database) -> None:
    db = at_0006
    await migrate_to(db, "0007")
    await insert_kb(db, "00000000-0000-7000-8000-000000000020", "manual", deleted=True)

    await insert_kb(db, "00000000-0000-7000-8000-000000000021", "manual")

    with pytest.raises(IntegrityError):
        await insert_kb(db, "00000000-0000-7000-8000-000000000022", "manual")


async def test_parent_segments_share_a_null_unit_no(at_0006: Database) -> None:
    # Parent-child의 부모는 유닛이 아니라 unit_no가 NULL이다. UNIQUE가 NULL끼리는 막지 않아야 한다.
    db = at_0006
    await migrate_to(db, "0007")
    kb, doc = "00000000-0000-7000-8000-000000000030", "00000000-0000-7000-8000-000000000031"
    await insert_kb(db, kb, "tree")
    await execute(
        db,
        "INSERT INTO kb_document (id, workspace_id, kb_id, position, name, data_source_type,"
        " filename, mime, size_bytes, sha256, storage_key, process_rule, indexing_status,"
        " enabled, archived, total_segments, completed_segments, word_count, tokens, hit_count,"
        " batch, created_at, updated_at) VALUES (:id, :ws, :kb, 1, 'a', 'paste_text', 'a.txt',"
        " 'text/plain', 1, :sha, 'kb-files/x', '{}', 'waiting', 1, 0, 0, 0, 0, 0, 0, 'b',"
        " :ts, :ts)",
        id=doc,
        ws=WS,
        kb=kb,
        sha="0" * 64,
        ts=STAMP,
    )

    async def segment(position: int, unit_no: int | None) -> None:
        await execute(
            db,
            "INSERT INTO kb_segment (id, workspace_id, kb_id, document_id, position, unit_no,"
            " content, word_count, tokens, content_hash, enabled, status, index_state,"
            " created_by, edited, hit_count, created_at, updated_at) VALUES (:id, :ws, :kb, :doc,"
            " :pos, :unit, 'x', 1, 1, :hash, 1, 'waiting', '{}', 'automatic', 0, 0, :ts, :ts)",
            id=str(uuid.uuid4()),
            ws=WS,
            kb=kb,
            doc=doc,
            pos=position,
            unit=unit_no,
            hash="0" * 64,
            ts=STAMP,
        )

    await segment(1, None)
    await segment(2, None)
    await segment(3, 7)
    with pytest.raises(IntegrityError):
        await segment(4, 7)
    with pytest.raises(IntegrityError):
        await segment(3, None)


async def test_child_chunks_are_indexed_by_document_and_kb(at_0006: Database) -> None:
    # 문서 삭제 작업(1,000행씩)과 KB 단위 정리가 자식 행 전체를 훑지 않아야 한다(M21 검토 1-5).
    db = at_0006
    await migrate_to(db, "0007")
    await db.close()

    with closing(sqlite3.connect(db.path)) as conn:
        indexes = {
            name: [row[2] for row in conn.execute(f"PRAGMA index_info('{name}')")]
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'index' AND tbl_name = 'kb_child_chunk'"
            )
        }
        plan = " ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM kb_child_chunk WHERE document_id = ?", ("x",)
            )
        )

    assert indexes["ix_kb_child_chunk_document_id"] == ["document_id"]
    assert indexes["ix_kb_child_chunk_kb_id"] == ["kb_id"]
    assert "ix_kb_child_chunk_document_id" in plan


async def test_downgrade_restores_the_0006_schema(at_0006: Database) -> None:
    db = at_0006
    agent_key = await insert_key(db, agent=AGENT)
    await migrate_to(db, "0007")
    kb = "00000000-0000-7000-8000-000000000040"
    await insert_kb(db, kb, "gone")
    await insert_key(db, kb=kb)

    await migrate_to(db, "0006", down=True)

    assert await current_revision(db) == "0006"
    # 지식 베이스 키는 지우고 나머지 키는 남는다. 옛 CHECK가 다시 둘 중 하나를 요구한다.
    assert await rows(db, "SELECT id FROM api_key") == [(agent_key,)]
    with pytest.raises(IntegrityError):
        await insert_key(db)
    await db.close()
    assert not KB_TABLES & table_names(db.path)
    assert "knowledge_base_id" not in columns(db.path, "api_key")
    assert "knowledge" not in columns(db.path, "agent_version")
    for table in ("wf_run", "wf_message", "message"):
        assert "retriever_resources" not in columns(db.path, table)
