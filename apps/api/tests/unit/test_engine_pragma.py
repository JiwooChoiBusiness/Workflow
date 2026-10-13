import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.engine import Database


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "app.db")
    await database.connect()
    yield database
    await database.close()


def can_take_write_lock(path: Path) -> bool:
    # timeout=0이면 다른 연결이 쓰기 잠금을 쥐고 있을 때 기다리지 않고 바로 실패한다.
    with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as probe:
        try:
            probe.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False
        probe.execute("ROLLBACK")
        return True


async def test_connection_pragmas(db: Database) -> None:
    names = ("journal_mode", "synchronous", "foreign_keys", "busy_timeout", "temp_store")
    async with db.read() as session:
        values = {
            name: (await session.execute(text(f"PRAGMA {name}"))).scalar_one() for name in names
        }
        cache_size = (await session.execute(text("PRAGMA cache_size"))).scalar_one()

    assert values == {
        "journal_mode": "wal",
        "synchronous": 1,  # NORMAL
        "foreign_keys": 1,
        "busy_timeout": 5000,
        "temp_store": 2,  # MEMORY
    }
    assert cache_size == -8000


async def test_write_session_takes_write_lock_at_begin(db: Database) -> None:
    async with db.write() as session:
        await session.execute(text("SELECT 1"))

        assert not can_take_write_lock(db.path)

    assert can_take_write_lock(db.path)


async def test_read_session_does_not_hold_write_lock(db: Database) -> None:
    async with db.read() as session:
        await session.execute(text("SELECT 1"))

        assert can_take_write_lock(db.path)


async def test_write_sessions_are_serialized(db: Database) -> None:
    order: list[str] = []
    first_inside = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with db.write():
            order.append("first-in")
            first_inside.set()
            await release.wait()
            order.append("first-out")

    async def second() -> None:
        await first_inside.wait()
        async with db.write():
            order.append("second-in")

    tasks = [asyncio.create_task(first()), asyncio.create_task(second())]
    await first_inside.wait()
    await asyncio.sleep(0.05)
    assert order == ["first-in"]

    release.set()
    await asyncio.gather(*tasks)
    assert order == ["first-in", "first-out", "second-in"]


async def test_write_connection_begins_immediate_and_shares_the_write_lock(db: Database) -> None:
    order: list[str] = []
    inside = asyncio.Event()
    release = asyncio.Event()

    async def migrate_like() -> None:
        async with db.write_connection() as conn, conn.begin():
            await conn.execute(text("SELECT 1"))
            order.append("connection-in")
            assert not can_take_write_lock(db.path)
            inside.set()
            await release.wait()
            order.append("connection-out")

    async def writer() -> None:
        await inside.wait()
        async with db.write():
            order.append("session-in")

    tasks = [asyncio.create_task(migrate_like()), asyncio.create_task(writer())]
    await inside.wait()
    await asyncio.sleep(0.05)
    assert order == ["connection-in"]

    release.set()
    await asyncio.gather(*tasks)
    assert order == ["connection-in", "connection-out", "session-in"]
    assert can_take_write_lock(db.path)


async def test_write_commits_and_rolls_back(db: Database) -> None:
    async with db.write() as session:
        await session.execute(text("CREATE TABLE t (x INTEGER)"))
        await session.execute(text("INSERT INTO t VALUES (1)"))
    with pytest.raises(RuntimeError):
        async with db.write() as session:
            await session.execute(text("INSERT INTO t VALUES (2)"))
            raise RuntimeError("boom")

    async with db.read() as session:
        rows = (await session.execute(text("SELECT x FROM t"))).scalars().all()

    assert rows == [1]


async def test_close_checkpoints_and_removes_wal(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    database = Database(path)
    async with database.write() as session:
        await session.execute(text("CREATE TABLE t (x INTEGER)"))
        await session.execute(text("INSERT INTO t VALUES (1)"))
    assert path.with_name("app.db-wal").exists()

    await database.close()

    assert not path.with_name("app.db-wal").exists()
    assert not path.with_name("app.db-shm").exists()
    assert not await database.ping()
    with pytest.raises(RuntimeError):
        async with database.read():
            pass
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]
