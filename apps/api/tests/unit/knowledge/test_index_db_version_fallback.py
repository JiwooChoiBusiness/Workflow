"""SQLite 3.43 미만이면 contentless 대신 본문을 저장하는 FTS5로 만든다(설계 5.3).

CI의 ubuntu 러너처럼 오래된 SQLite는 이 PC에 없으므로 sqlite3가 알려 주는 버전을 바꿔 폴백 모드를
켜고 같은 시험을 돌린다. 전문 색인 시험(test_fts_bigram)은 픽스처가 두 모드를 모두 돈다.
"""

import sqlite3
import uuid
from collections.abc import Awaitable, Callable

import pytest

from app.knowledge.fts_index import SqliteFtsIndex
from app.knowledge.index_db import IndexDb, contentless_supported, get_meta
from app.knowledge.units import FtsRow, VecRow, fts_table_name
from app.knowledge.vector_index import SqliteVecIndex
from app.llm.embeddings import EmbeddingConfig, pack_vector

MakeIndexDb = Callable[..., Awaitable[IndexDb]]
WORKSPACE = uuid.UUID("01890000-0000-7000-8000-000000000002")


async def create_sql(db: IndexDb, table: str) -> str:
    async with db.read() as conn:
        rows = await conn.execute_fetchall(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        )
    return str(next(iter(rows))[0])


def test_contentless_needs_sqlite_3_43() -> None:
    assert contentless_supported((3, 43, 0))
    assert contentless_supported((3, 45, 3))
    assert not contentless_supported((3, 42, 9))
    assert not contentless_supported((3, 37, 2))


async def test_mode_follows_the_running_sqlite(index_db: IndexDb) -> None:
    # CI 로그에서 러너의 SQLite 버전과 고른 모드를 볼 수 있게 남긴다(-rA 또는 -s).
    print(f"SQLite {index_db.sqlite_version}: fts_contentless={index_db.fts_contentless}")

    assert index_db.fts_contentless is contentless_supported()


async def test_old_sqlite_builds_stored_fts_and_the_same_operations_work(
    make_index_db: MakeIndexDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 37, 2))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.37.2")
    db = await make_index_db()
    kb_id = uuid.uuid4()
    fts = SqliteFtsIndex(db)
    vectors = SqliteVecIndex(db)

    await fts.ensure(kb_id, workspace_id=WORKSPACE)
    await fts.upsert(
        kb_id, [FtsRow(1, "주간 회의록"), FtsRow(2, "학회 발표"), FtsRow(3, "결제 오류")]
    )
    await fts.delete(kb_id, [3])
    await vectors.ensure(kb_id, EmbeddingConfig(dimensions=2), workspace_id=WORKSPACE)
    await vectors.upsert(kb_id, [VecRow(1, pack_vector([1.0, 0.0]), "doc")])

    assert db.sqlite_version == "3.37.2"
    assert not db.fts_contentless
    assert await get_meta(db, "fts_contentless") == "0"
    assert "contentless" not in await create_sql(db, fts_table_name(kb_id))
    assert [hit.unit_no for hit in await fts.search(kb_id, "회의록", 5)] == [1]
    assert await fts.search(kb_id, "결제", 5) == []
    assert await fts.count(kb_id) == 2
    assert [hit.unit_no for hit in await vectors.search(kb_id, pack_vector([1.0, 0.0]), 1)] == [1]


async def test_contentless_cannot_be_forced_on_old_sqlite(
    make_index_db: MakeIndexDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 42, 0))

    db = await make_index_db(fts_contentless=True)

    assert not db.fts_contentless


async def test_existing_tables_keep_their_mode_when_the_file_is_reopened_in_fallback(
    make_index_db: MakeIndexDb,
) -> None:
    if not contentless_supported():
        pytest.skip(f"SQLite {sqlite3.sqlite_version}에는 contentless 테이블이 없다")
    old_kb, new_kb = uuid.uuid4(), uuid.uuid4()
    first = await make_index_db()
    await SqliteFtsIndex(first).ensure(old_kb, workspace_id=WORKSPACE)
    await SqliteFtsIndex(first).upsert(old_kb, [FtsRow(1, "회의록")])
    await first.close()

    db = await make_index_db(fts_contentless=False)
    fts = SqliteFtsIndex(db)
    await fts.ensure(new_kb, workspace_id=WORKSPACE)

    # 모드는 meta의 값이 아니라 테이블마다 만들 때 정해진다. 질의 SQL이 같아 둘 다 읽힌다.
    assert await get_meta(db, "fts_contentless") == "0"
    assert "contentless_delete" in await create_sql(db, fts_table_name(old_kb))
    assert "contentless" not in await create_sql(db, fts_table_name(new_kb))
    assert [hit.unit_no for hit in await fts.search(old_kb, "회의록", 5)] == [1]
