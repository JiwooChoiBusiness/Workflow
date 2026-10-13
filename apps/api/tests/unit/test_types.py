import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, insert, select, text
from sqlalchemy.dialects import sqlite

from app.db.engine import Database
from app.db.types import TIMESTAMP_FORMAT, UTCDateTime, UUIDStr, enum_check, new_id

DIALECT = sqlite.dialect()
KST = timezone(timedelta(hours=9))

metadata = MetaData()
sample = Table(
    "sample",
    metadata,
    Column("pk", Integer, primary_key=True),
    Column("ref", UUIDStr()),
    Column("at", UTCDateTime()),
)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "types.db")
    async with database.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield database
    await database.close()


def test_uuid_is_stored_as_lowercase_hyphenated_text() -> None:
    value = uuid.UUID("0192F0C8-7A3B-7C2D-8E4F-0123456789AB")
    bind = UUIDStr().process_bind_param

    assert bind(value, DIALECT) == "0192f0c8-7a3b-7c2d-8e4f-0123456789ab"
    assert bind("0192F0C8-7A3B-7C2D-8E4F-0123456789AB", DIALECT) == str(value)
    assert bind(None, DIALECT) is None
    with pytest.raises(ValueError):
        bind("not-a-uuid", DIALECT)


def test_timestamp_is_fixed_width_utc() -> None:
    bind = UTCDateTime().process_bind_param

    assert (
        bind(datetime(2026, 10, 13, 18, 30, tzinfo=KST), DIALECT) == "2026-10-13T09:30:00.000000Z"
    )
    assert bind(datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC), DIALECT) == (
        "2026-01-02T03:04:05.000006Z"
    )
    with pytest.raises(ValueError):
        bind(datetime(2026, 10, 13, 9, 30), DIALECT)


def test_timestamp_text_order_matches_time_order() -> None:
    bind = UTCDateTime().process_bind_param
    base = datetime(2026, 10, 13, tzinfo=UTC)
    moments = [base + timedelta(microseconds=n) for n in (0, 1, 999_999, 10**6, 86_400 * 10**6)]
    moments += [base - timedelta(days=400), datetime(2026, 10, 13, 8, 59, tzinfo=KST)]

    texts = [bind(moment, DIALECT) for moment in moments]

    assert len({len(t) for t in texts if t}) == 1
    assert sorted(texts, key=str) == [bind(m, DIALECT) for m in sorted(moments)]


async def test_round_trip_through_sqlite(db: Database) -> None:
    ref = new_id()
    at = datetime(2026, 10, 13, 18, 30, 0, 123456, tzinfo=KST)
    async with db.write() as session:
        await session.execute(insert(sample).values(pk=1, ref=ref, at=at))

    async with db.read() as session:
        row = (await session.execute(select(sample.c.ref, sample.c.at))).one()
        raw = (await session.execute(text("SELECT ref, at FROM sample"))).one()

    assert row.ref == ref
    assert row.at == at
    assert row.at.tzinfo == UTC
    assert raw.ref == str(ref)
    assert raw.at == at.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


async def test_range_query_on_timestamps(db: Database) -> None:
    base = datetime(2026, 10, 13, tzinfo=UTC)
    async with db.write() as session:
        for pk in range(5):
            await session.execute(insert(sample).values(pk=pk, at=base + timedelta(hours=pk)))

    async with db.read() as session:
        newer = await session.scalars(
            select(sample.c.pk).where(sample.c.at > base + timedelta(minutes=90))
        )

    assert sorted(newer) == [2, 3, 4]


def test_new_id_is_uuid7_in_time_order() -> None:
    first = new_id()
    time.sleep(0.002)
    second = new_id()

    assert first.version == second.version == 7
    assert str(first) < str(second)


def test_enum_check_quotes_column_and_values() -> None:
    check = enum_check("trigger", ["chat", "task"])

    assert str(check.sqltext) == "\"trigger\" IN ('chat', 'task')"
    assert check.name == "trigger"
