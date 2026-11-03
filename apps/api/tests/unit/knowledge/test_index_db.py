import asyncio
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from app.knowledge.fts_index import SqliteFtsIndex
from app.knowledge.index_db import (
    ERR_INDEX_UNAVAILABLE,
    PAGE_SIZE,
    SCHEMA_VERSION,
    IndexDb,
    IndexUnavailable,
    get_meta,
)
from app.knowledge.units import FtsRow, fts_table_name, vec_table_name
from app.knowledge.vector_index import SqliteVecIndex
from app.llm.embeddings import EmbeddingConfig

MakeIndexDb = Callable[..., Awaitable[IndexDb]]
WORKSPACE = uuid.UUID("01890000-0000-7000-8000-000000000001")


async def pragma(db: IndexDb, name: str, *, writer: bool = False) -> object:
    if writer:
        async with db.write() as conn:
            rows = await conn.execute_fetchall(f"PRAGMA {name}")
    else:
        async with db.read() as conn:
            rows = await conn.execute_fetchall(f"PRAGMA {name}")
    return next(iter(rows))[0]


async def table_names(db: IndexDb) -> set[str]:
    async with db.read() as conn:
        rows = await conn.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(row[0]) for row in rows}


def test_wal_first_would_lock_the_page_size(tmp_path: Path) -> None:
    # 순서가 왜 중요한지의 대조 실험: WAL로 먼저 바꾸면 그 뒤의 page_size는 무시된다.
    conn = sqlite3.connect(tmp_path / "wrong-order.db")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA page_size={PAGE_SIZE}")
        conn.execute("CREATE TABLE t (x)")
        assert conn.execute("PRAGMA page_size").fetchone()[0] != PAGE_SIZE
    finally:
        conn.close()


async def test_new_file_gets_64k_pages_before_wal(index_db: IndexDb) -> None:
    assert await pragma(index_db, "page_size") == PAGE_SIZE
    assert await pragma(index_db, "journal_mode") == "wal"
    assert index_db.page_size == PAGE_SIZE
    assert index_db.schema_version == SCHEMA_VERSION
    assert await get_meta(index_db, "page_size") == str(PAGE_SIZE)
    assert await get_meta(index_db, "schema_version") == str(SCHEMA_VERSION)
    assert await get_meta(index_db, "created_at")
    assert await get_meta(index_db, "fts_contentless") == ("1" if index_db.fts_contentless else "0")
    assert {"meta", "kb_index", "embed_cache"} <= await table_names(index_db)


async def test_every_connection_turns_mmap_off(make_index_db: MakeIndexDb) -> None:
    db = await make_index_db(cache_kb=4000)

    for writer in (False, True):
        assert await pragma(db, "mmap_size", writer=writer) == 0
        assert await pragma(db, "cache_size", writer=writer) == -4000
        assert await pragma(db, "busy_timeout", writer=writer) == 5000
        assert await pragma(db, "synchronous", writer=writer) == 1
        assert await pragma(db, "temp_store", writer=writer) == 2
    assert await pragma(db, "query_only") == 1
    assert await pragma(db, "query_only", writer=True) == 0


async def test_vec0_is_loaded_on_readers_and_the_writer(index_db: IndexDb) -> None:
    async with index_db.read() as conn:
        rows = await conn.execute_fetchall("SELECT vec_version()")
    assert str(next(iter(rows))[0]).startswith("v0.1.")


async def test_reopening_keeps_data_and_does_not_migrate_again(make_index_db: MakeIndexDb) -> None:
    kb_id = uuid.uuid4()
    first = await make_index_db()
    await SqliteFtsIndex(first).ensure(kb_id, workspace_id=WORKSPACE)
    created_at = await get_meta(first, "created_at")
    await first.close()

    again = await make_index_db()

    meta = await again.describe(kb_id)
    assert meta is not None and meta.workspace_id == WORKSPACE
    assert meta.fts_table == fts_table_name(kb_id)
    assert await get_meta(again, "created_at") == created_at
    assert await get_meta(again, "schema_version") == str(SCHEMA_VERSION)


async def test_extension_that_cannot_load_disables_only_the_index(
    make_index_db: MakeIndexDb, tmp_path: Path
) -> None:
    db = await make_index_db(
        expect_open=False, extension_path=lambda: str(tmp_path / "no-such-extension")
    )

    assert not db.available
    assert db.unavailable_reason
    with pytest.raises(IndexUnavailable) as raised:
        db.ensure_available()
    assert raised.value.code == ERR_INDEX_UNAVAILABLE
    with pytest.raises(IndexUnavailable):
        async with db.read():
            pass


async def test_missing_sqlite_vec_package_disables_the_index(make_index_db: MakeIndexDb) -> None:
    def missing() -> str:
        raise ImportError("No module named 'sqlite_vec'")

    db = await make_index_db(expect_open=False, extension_path=missing)

    assert not db.available
    assert db.unavailable_reason is not None and "sqlite_vec" in db.unavailable_reason


async def test_file_written_by_a_newer_version_is_left_alone(
    make_index_db: MakeIndexDb, tmp_path: Path
) -> None:
    await (await make_index_db()).close()
    raw = sqlite3.connect(tmp_path / "index.db")
    with raw:
        raw.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    raw.close()

    db = await make_index_db(expect_open=False)

    assert not db.available
    assert db.unavailable_reason is not None and "99" in db.unavailable_reason
    raw = sqlite3.connect(tmp_path / "index.db")
    try:
        assert raw.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == (
            "99",
        )
    finally:
        raw.close()


async def test_file_that_is_not_a_database_is_unavailable(
    make_index_db: MakeIndexDb, tmp_path: Path
) -> None:
    (tmp_path / "index.db").write_bytes(b"not a database" * 100)

    db = await make_index_db(expect_open=False)

    assert not db.available


async def test_failed_write_rolls_back_and_the_writer_stays_usable(index_db: IndexDb) -> None:
    kb_id = uuid.uuid4()
    with pytest.raises(RuntimeError):
        async with index_db.write() as conn:
            await conn.execute(
                "INSERT INTO kb_index (kb_id, workspace_id, updated_at) VALUES (?, ?, ?)",
                (str(kb_id), str(WORKSPACE), "2026-11-02T00:00:00.000000Z"),
            )
            raise RuntimeError("중간에 실패")

    assert await index_db.describe(kb_id) is None
    await SqliteFtsIndex(index_db).ensure(kb_id, workspace_id=WORKSPACE)
    assert await index_db.describe(kb_id) is not None


async def test_concurrent_writes_are_serialized(index_db: IndexDb) -> None:
    fts = SqliteFtsIndex(index_db)
    kb_ids = [uuid.uuid4() for _ in range(5)]

    await asyncio.gather(*(fts.ensure(kb_id, workspace_id=WORKSPACE) for kb_id in kb_ids))

    assert {meta.kb_id for meta in await index_db.list_kbs()} == set(kb_ids)


async def test_kb_row_belongs_to_one_workspace(index_db: IndexDb) -> None:
    kb_id = uuid.uuid4()
    fts = SqliteFtsIndex(index_db)
    await fts.ensure(kb_id, workspace_id=WORKSPACE)

    with pytest.raises(ValueError):
        await fts.ensure(kb_id, workspace_id=uuid.uuid4())
    with pytest.raises(ValueError):
        await SqliteVecIndex(index_db).ensure(kb_id, EmbeddingConfig(), workspace_id=uuid.uuid4())


async def test_drop_kb_removes_both_tables_and_the_row(index_db: IndexDb) -> None:
    kb_id = uuid.uuid4()
    await SqliteFtsIndex(index_db).ensure(kb_id, workspace_id=WORKSPACE)
    await SqliteVecIndex(index_db).ensure(
        kb_id, EmbeddingConfig(dimensions=4), workspace_id=WORKSPACE
    )
    assert {vec_table_name(kb_id), fts_table_name(kb_id)} <= await table_names(index_db)

    await index_db.drop_kb(kb_id)

    assert await index_db.describe(kb_id) is None
    tables = await table_names(index_db)
    assert not any(kb_id.hex in name for name in tables)


async def test_unit_count_is_recounted_from_the_fulltext_rows(index_db: IndexDb) -> None:
    kb_id = uuid.uuid4()
    fts = SqliteFtsIndex(index_db)
    await fts.ensure(kb_id, workspace_id=WORKSPACE)
    await fts.upsert(kb_id, [FtsRow(unit, f"문단 {unit}") for unit in range(1, 8)])

    assert await index_db.refresh_unit_count(kb_id) == 7
    meta = await index_db.describe(kb_id)
    assert meta is not None and meta.unit_count == 7
    assert await index_db.refresh_unit_count(uuid.uuid4()) == 0


async def test_checkpoint_and_close_leave_no_wal_behind(
    make_index_db: MakeIndexDb, tmp_path: Path
) -> None:
    db = await make_index_db()
    kb_id = uuid.uuid4()
    fts = SqliteFtsIndex(db)
    await fts.ensure(kb_id, workspace_id=WORKSPACE)
    await fts.upsert(kb_id, [FtsRow(1, "회의록")])

    busy, _, _ = await db.checkpoint("PASSIVE")
    with pytest.raises(ValueError):
        await db.checkpoint("NOW")  # type: ignore[arg-type]
    await db.close()

    assert busy == 0
    wal = tmp_path / "index.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    with pytest.raises(IndexUnavailable):
        db.ensure_available()


async def test_close_wakes_readers_that_wait_for_a_connection(make_index_db: MakeIndexDb) -> None:
    # 검색이 reader를 기다리는 중에 닫히면 기다리지 않고 IndexUnavailable이다(M21 검토 2-8).
    db = await make_index_db(readers=1)
    held = asyncio.Event()
    release = asyncio.Event()

    async def hold_the_reader() -> None:
        async with db.read():
            held.set()
            await release.wait()

    async def wait_for_a_reader() -> None:
        async with db.read():
            pass

    holder = asyncio.create_task(hold_the_reader())
    await held.wait()
    waiter = asyncio.create_task(wait_for_a_reader())
    await asyncio.sleep(0)

    closing = asyncio.create_task(db.close())
    with pytest.raises(IndexUnavailable):
        await asyncio.wait_for(waiter, 5)
    release.set()
    await holder
    await closing
    with pytest.raises(IndexUnavailable):
        async with db.write():
            pass
