import asyncio
import sqlite3
import warnings
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

import app.db.models  # noqa: F401
from app import cli, sysinfo
from app.config import Settings, get_settings
from app.db.base import Base
from app.db.engine import Database
from app.db.migrate import alembic_config, current_revision, run_in_transaction, upgrade_head
from app.logsetup import configure_logging
from app.main import create_app

HEAD = "0004"

# 1단계 설계 5.1과 2단계 설계 4.1(0003)의 테이블 전부.
TABLES = {
    "workspace",
    "account",
    "login_session",
    "agent",
    "agent_version",
    "skill",
    "skill_version",
    "agent_file",
    "session",
    "message",
    "run",
    "run_event",
    "session_file",
    "workspace_instance",
    "snapshot",
    "api_key",
    "workflow",
    "wf_version",
    "wf_run",
    "wf_node_run",
    "wf_run_event",
    "wf_file",
}


def table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {name for (name,) in rows if not name.startswith("sqlite_")}


@pytest.fixture
async def migrated(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "app.db")
    await upgrade_head(database)
    yield database
    await database.close()


async def test_upgrade_creates_every_table(migrated: Database) -> None:
    assert await current_revision(migrated) == HEAD
    await migrated.close()

    assert table_names(migrated.path) == TABLES | {"alembic_version"}


async def test_models_match_migrations(migrated: Database) -> None:
    def diff(connection: Connection) -> list[object]:
        context = MigrationContext.configure(connection)
        with warnings.catch_warnings():
            # SQLite는 표현식 인덱스(skill의 coalesce 유니크)를 반영해 읽지 못해 비교에서 빠진다.
            warnings.filterwarnings("ignore", message=".*expression-based index.*")
            return list(compare_metadata(context, Base.metadata))

    async with migrated.engine.connect() as conn:
        differences = await conn.run_sync(diff)

    assert differences == []


async def test_upgrade_is_idempotent_and_keeps_foreign_keys_on(migrated: Database) -> None:
    # 마이그레이션은 foreign_keys를 끄고 돈다. 풀로 돌아간 연결에서 다시 켜져 있어야 한다.
    await upgrade_head(migrated)

    # 세션 셋을 동시에 열어 풀(최대 3개)의 연결을 모두 확인한다.
    async with migrated.read() as a, migrated.read() as b, migrated.read() as c:
        values = [(await s.execute(text("PRAGMA foreign_keys"))).scalar_one() for s in (a, b, c)]
    assert values == [1, 1, 1]
    assert await current_revision(migrated) == HEAD


async def test_expression_unique_index_on_skill(migrated: Database) -> None:
    await migrated.close()
    with closing(sqlite3.connect(migrated.path)) as conn:
        indexes = {row[1]: row[2] for row in conn.execute("PRAGMA index_list('skill')")}

    assert indexes.get("uq_skill_workspace_id_scope_agent_id_slug") == 1


async def test_server_restart_reuses_schema(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])

    async def boot_and(action: str) -> int:
        application: FastAPI = create_app(settings)
        try:
            async with (
                application.router.lifespan_context(application),
                AsyncClient(
                    transport=ASGITransport(app=application), base_url="http://127.0.0.1:8500"
                ) as client,
            ):
                if action == "setup":
                    response = await client.post(
                        "/api/console/setup",
                        json={"email": "owner@example.com", "password": "correct-horse-battery"},
                    )
                else:
                    response = await client.get("/api/console/setup")
                return response.status_code
        finally:
            configure_logging("INFO")

    assert await boot_and("setup") == 201
    assert await boot_and("check") == 404


async def test_platform_migrate_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "cli-data"))
    get_settings.cache_clear()
    try:
        # CLI는 asyncio.run을 쓰므로 테스트의 이벤트 루프 밖(다른 스레드)에서 돌린다.
        first = await asyncio.to_thread(cli.main, ["migrate"])
        second = await asyncio.to_thread(cli.main, ["migrate"])
    finally:
        get_settings.cache_clear()
        configure_logging("INFO")

    assert first == second == 0
    assert f"리비전 {HEAD}" in capsys.readouterr().out
    assert table_names(tmp_path / "cli-data" / "app.db") == TABLES | {"alembic_version"}


async def migrate_to(db: Database, revision: str, *, down: bool = False) -> None:
    from alembic import command

    config = alembic_config()

    def run(connection: Connection) -> None:
        config.attributes["connection"] = connection
        step = command.downgrade if down else command.upgrade
        run_in_transaction(connection, lambda: step(config, revision))

    async with db.write_connection() as conn:
        await conn.run_sync(run)


def index_flags(path: Path, table: str) -> dict[str, tuple[int, str, int]]:
    # PRAGMA index_list: (seq, name, unique, origin, partial). origin은 c(CREATE INDEX),
    # u(UNIQUE 제약), pk(기본 키).
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(f"PRAGMA index_list('{table}')").fetchall()
    return {row[1]: (row[2], row[3], row[4]) for row in rows}


STAMP = "2026-10-13T00:00:00.000000Z"


async def insert_agent(
    db: Database, workspace_id: str, agent_id: str, slug: str, *, deleted: bool = False
) -> None:
    async with db.write() as session:
        await session.execute(
            text(
                "INSERT INTO agent (id, workspace_id, slug, name, description, webapp_enabled,"
                " deleted_at, created_at, updated_at)"
                " VALUES (:id, :ws, :slug, :slug, '', 0, :deleted, :ts, :ts)"
            ),
            {
                "id": agent_id,
                "ws": workspace_id,
                "slug": slug,
                "deleted": STAMP if deleted else None,
                "ts": STAMP,
            },
        )


async def test_slug_indexes_skip_deleted_rows(migrated: Database) -> None:
    await migrated.close()

    agent = index_flags(migrated.path, "agent")
    skill = index_flags(migrated.path, "skill")

    assert agent["uq_agent_workspace_id_slug"] == (1, "c", 1)
    assert skill["uq_skill_workspace_id_scope_agent_id_slug"] == (1, "c", 1)
    # 0001의 UNIQUE 제약은 테이블을 다시 만들면서 사라져야 한다.
    assert [flags for flags in agent.values() if flags[1] == "u"] == []


async def test_upgrade_from_0001_keeps_rows_and_allows_reusing_deleted_slugs(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "app.db")
    workspace_id = "00000000-0000-7000-8000-000000000001"
    agent_id = "00000000-0000-7000-8000-000000000002"
    try:
        await migrate_to(db, "0001")
        async with db.write() as session:
            await session.execute(
                text(
                    "INSERT INTO workspace (id, name, slug, settings, created_at, updated_at)"
                    " VALUES (:id, 'w', 'w', '{}', :ts, :ts)"
                ),
                {"id": workspace_id, "ts": STAMP},
            )
        await insert_agent(db, workspace_id, agent_id, "helper", deleted=True)
        async with db.write() as session:
            # agent를 가리키는 행이 테이블을 다시 만든 뒤에도 그대로 이어져야 한다.
            await session.execute(
                text(
                    "INSERT INTO api_key (id, workspace_id, agent_id, name, key_prefix, key_hash,"
                    " scopes, created_at, updated_at)"
                    " VALUES ('00000000-0000-7000-8000-000000000003', :ws, :agent, 'k',"
                    " 'sk-wf-abcdef', :hash, '[]', :ts, :ts)"
                ),
                {"ws": workspace_id, "agent": agent_id, "hash": "0" * 64, "ts": STAMP},
            )

        await migrate_to(db, "head")

        assert await current_revision(db) == HEAD
        async with db.read() as session:
            slugs = (await session.execute(text("SELECT slug, deleted_at FROM agent"))).all()
            keys = (await session.execute(text("SELECT agent_id FROM api_key"))).scalars().all()
        assert [tuple(row) for row in slugs] == [("helper", STAMP)]
        assert keys == [agent_id]

        await insert_agent(db, workspace_id, "00000000-0000-7000-8000-000000000004", "helper")
        with pytest.raises(IntegrityError):
            await insert_agent(db, workspace_id, "00000000-0000-7000-8000-000000000005", "helper")
    finally:
        await db.close()


async def test_downgrade_restores_the_unique_constraint(migrated: Database) -> None:
    await migrate_to(migrated, "0001", down=True)

    assert await current_revision(migrated) == "0001"
    await migrated.close()
    agent = index_flags(migrated.path, "agent")
    skill = index_flags(migrated.path, "skill")
    assert "uq_agent_workspace_id_slug" not in agent
    assert [flags for flags in agent.values() if flags[1] == "u"] == [(1, "u", 0)]
    assert skill["uq_skill_workspace_id_scope_agent_id_slug"] == (1, "c", 0)
