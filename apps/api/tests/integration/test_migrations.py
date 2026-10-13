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

import app.db.models  # noqa: F401
from app import cli, sysinfo
from app.config import Settings, get_settings
from app.db.base import Base
from app.db.engine import Database
from app.db.migrate import current_revision, upgrade_head
from app.logging import configure_logging
from app.main import create_app

# 설계 5.1의 테이블 전부.
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
    assert await current_revision(migrated) == "0001"
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
    assert await current_revision(migrated) == "0001"


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
    assert "리비전 0001" in capsys.readouterr().out
    assert table_names(tmp_path / "cli-data" / "app.db") == TABLES | {"alembic_version"}
