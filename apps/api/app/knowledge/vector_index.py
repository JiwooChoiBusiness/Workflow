import itertools
from collections.abc import AsyncIterator, Collection, Sequence
from typing import Any, Protocol
from uuid import UUID

import aiosqlite

from app.knowledge.index_db import (
    IndexDb,
    delete_rowids,
    iter_rowids,
    read_kb_row,
    table_exists,
    upsert_kb_row,
)
from app.knowledge.units import Hit, IndexMeta, VecRow, vec_table_name
from app.llm.embeddings import EmbeddingConfig

# vec0의 k 상한. 4097부터 확장이 오류를 낸다[실측].
MAX_K = 4096
# 문서 필터 IN 목록 한 번의 크기. 넘으면 나눠 KNN을 돌리고 거리순으로 합친다.
DOC_FILTER_BATCH = 500
GET_BATCH = 500


class VectorIndex(Protocol):
    """벡터 색인 경계(설계 5.2). sqlite-vec(pre-1.0)의 유지 보수 위험 때문에 구현을 바꿔 끼울 수
    있게 한다."""

    async def ensure(self, kb_id: UUID, config: EmbeddingConfig, *, workspace_id: UUID) -> bool: ...
    async def drop(self, kb_id: UUID) -> None: ...
    async def upsert(self, kb_id: UUID, rows: Sequence[VecRow]) -> None: ...
    async def delete(self, kb_id: UUID, unit_nos: Collection[int]) -> None: ...
    async def delete_docs(self, kb_id: UUID, doc_ids: Collection[str]) -> None: ...
    async def get(self, kb_id: UUID, unit_no: int) -> bytes | None: ...
    async def get_many(self, kb_id: UUID, unit_nos: Collection[int]) -> dict[int, bytes]: ...
    async def search(
        self, kb_id: UUID, query: bytes, k: int, *, doc_ids: Collection[str] | None = None
    ) -> list[Hit]: ...
    async def count(self, kb_id: UUID) -> int: ...
    def list_unit_nos(self, kb_id: UUID) -> AsyncIterator[int]: ...
    async def describe(self, kb_id: UUID) -> IndexMeta | None: ...


class SqliteVecIndex:
    """KB마다 vec0 테이블 vec_<kb hex> 하나. rowid가 unit_no다.

    열은 embedding float[dims](코사인 거리), 메타데이터 열 doc_id(문서 필터 IN), 보조 열
    unit_kind다.
    """

    def __init__(self, db: IndexDb) -> None:
        self._db = db

    async def ensure(self, kb_id: UUID, config: EmbeddingConfig, *, workspace_id: UUID) -> bool:
        """테이블을 만들고 설정을 kb_index에 적는다. 새로 만들었으면(벡터가 비었으면) True.

        설정(config_key·차원·version)이 바뀌었으면 테이블을 지우고 다시 만든다. 같은 차원에 모델만
        다르면 vec0가 오류 없이 엉뚱한 결과를 내므로 행 존재가 아니라 설정으로 가른다(5.4).
        """
        table = vec_table_name(kb_id)
        async with self._db.write() as conn:
            meta = await read_kb_row(conn, kb_id)
            if meta is not None and meta.workspace_id != workspace_id:
                raise ValueError(f"kb_index {kb_id}는 다른 워크스페이스의 행이다")
            if (
                meta is not None
                and meta.vec_table == table
                and meta.config_key == config.key()
                and meta.dims == config.dimensions
                and meta.embedding_version == config.version
                and await table_exists(conn, table)
            ):
                return False
            await conn.execute(f"DROP TABLE IF EXISTS {table}")
            await conn.execute(
                f"CREATE VIRTUAL TABLE {table} USING vec0("
                f"embedding float[{int(config.dimensions)}] distance_metric=cosine, "
                "doc_id text, +unit_kind text)"
            )
            await upsert_kb_row(
                conn,
                kb_id,
                workspace_id,
                self._db.now(),
                vec_table=table,
                dims=config.dimensions,
                config_key=config.key(),
                embedding_version=config.version,
            )
        return True

    async def drop(self, kb_id: UUID) -> None:
        async with self._db.write() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {vec_table_name(kb_id)}")
            await conn.execute(
                "UPDATE kb_index SET vec_table = NULL, dims = NULL, config_key = NULL "
                "WHERE kb_id = ?",
                (str(kb_id),),
            )

    async def upsert(self, kb_id: UUID, rows: Sequence[VecRow]) -> None:
        # vec0는 INSERT OR REPLACE를 UNIQUE 위반으로 거부한다[실측]. 한 트랜잭션에서 지우고 넣는다.
        if not rows:
            return
        table = vec_table_name(kb_id)
        async with self._db.write() as conn:
            meta = await read_kb_row(conn, kb_id)
            if meta is None or meta.vec_table != table or meta.dims is None:
                raise LookupError(f"벡터 테이블이 없다(ensure 먼저): {kb_id}")
            width = meta.dims * 4
            for row in rows:
                if len(row.vector) != width:
                    dims = len(row.vector) // 4
                    raise ValueError(f"유닛 {row.unit_no}의 벡터가 {dims}차원이다(KB {meta.dims})")
            await delete_rowids(conn, table, [row.unit_no for row in rows])
            await conn.executemany(
                f"INSERT INTO {table} (rowid, embedding, doc_id, unit_kind) "  # noqa: S608
                "VALUES (?, ?, ?, ?)",
                [(row.unit_no, row.vector, row.doc_id, row.unit_kind) for row in rows],
            )

    async def delete(self, kb_id: UUID, unit_nos: Collection[int]) -> None:
        if not unit_nos:
            return
        table = vec_table_name(kb_id)
        async with self._db.write() as conn:
            if await table_exists(conn, table):
                await delete_rowids(conn, table, sorted(set(unit_nos)))

    async def delete_docs(self, kb_id: UUID, doc_ids: Collection[str]) -> None:
        if not doc_ids:
            return
        table = vec_table_name(kb_id)
        async with self._db.write() as conn:
            if not await table_exists(conn, table):
                return
            for batch in itertools.batched(sorted(set(doc_ids)), DOC_FILTER_BATCH):
                marks = ", ".join("?" for _ in batch)
                await conn.execute(
                    f"DELETE FROM {table} WHERE doc_id IN ({marks})",  # noqa: S608
                    batch,
                )

    async def get(self, kb_id: UUID, unit_no: int) -> bytes | None:
        return (await self.get_many(kb_id, [unit_no])).get(unit_no)

    async def get_many(self, kb_id: UUID, unit_nos: Collection[int]) -> dict[int, bytes]:
        # 캐시 복사용(살아 있는 유닛의 벡터를 다른 KB로, 지우는 유닛의 벡터를 embed_cache로).
        if not unit_nos:
            return {}
        table = vec_table_name(kb_id)
        found: dict[int, bytes] = {}
        async with self._db.read() as conn:
            if not await table_exists(conn, table):
                return {}
            for batch in itertools.batched(sorted(set(unit_nos)), GET_BATCH):
                marks = ", ".join("?" for _ in batch)
                rows = await conn.execute_fetchall(
                    f"SELECT rowid, embedding FROM {table} WHERE rowid IN ({marks})",  # noqa: S608
                    batch,
                )
                found.update((int(row[0]), bytes(row[1])) for row in rows)
        return found

    async def search(
        self, kb_id: UUID, query: bytes, k: int, *, doc_ids: Collection[str] | None = None
    ) -> list[Hit]:
        """코사인 유사도(1 - 거리) 상위 k. doc_ids가 있으면 그 문서의 유닛만 본다."""
        if not 1 <= k <= MAX_K:
            raise ValueError(f"k는 1~{MAX_K}이다: {k}")
        if doc_ids is not None and not doc_ids:
            return []
        table = vec_table_name(kb_id)
        knn = (
            f"SELECT rowid, distance FROM {table} "  # noqa: S608
            "WHERE embedding MATCH ? AND k = ?"
        )
        async with self._db.read() as conn:
            meta = await read_kb_row(conn, kb_id)
            if meta is None or meta.vec_table != table or meta.dims is None:
                return []
            if len(query) != meta.dims * 4:
                raise ValueError(f"질의 벡터가 {len(query) // 4}차원이다(KB {meta.dims})")
            if not await table_exists(conn, table):
                return []
            rows: list[Any] = []
            if doc_ids is None:
                rows.extend(await conn.execute_fetchall(knn + " ORDER BY distance", (query, k)))
            elif self._db.doc_filter_in:
                for batch in itertools.batched(sorted(set(doc_ids)), DOC_FILTER_BATCH):
                    marks = ", ".join("?" for _ in batch)
                    rows.extend(
                        await conn.execute_fetchall(
                            f"{knn} AND doc_id IN ({marks}) ORDER BY distance", (query, k, *batch)
                        )
                    )
            else:
                rows.extend(await _filtered_by_rowid(conn, table, query, doc_ids))
        ranked = sorted(rows, key=lambda row: (float(row[1]), int(row[0])))[:k]
        return [Hit(unit_no=int(row[0]), cosine=1.0 - float(row[1])) for row in ranked]

    async def count(self, kb_id: UUID) -> int:
        table = vec_table_name(kb_id)
        async with self._db.read() as conn:
            if not await table_exists(conn, table):
                return 0
            rows = await conn.execute_fetchall(f"SELECT count(*) FROM {table}")  # noqa: S608
        return int(next(iter(rows))[0])

    async def list_unit_nos(self, kb_id: UUID) -> AsyncIterator[int]:
        async for unit_no in iter_rowids(self._db, vec_table_name(kb_id)):
            yield unit_no

    async def describe(self, kb_id: UUID) -> IndexMeta | None:
        return await self._db.describe(kb_id)


async def _filtered_by_rowid(
    conn: aiosqlite.Connection, table: str, query: bytes, doc_ids: Collection[str]
) -> list[Any]:
    """SQLite 3.38 미만의 문서 필터 검색. 그 문서들의 rowid를 doc_id 등호로 모은 뒤 rowid 점
    조회로 거리를 잰다. KNN 인덱스를 쓰지 않아 유닛이 많으면 느리지만 결과는 같다.

    vec0의 IN 처리(doc_id IN, KNN과 함께 쓰는 rowid IN)는 sqlite3_vtab_in이 있어야 해서 3.38부터
    된다. 등호와 rowid 점 조회는 그 전에도 된다.
    """
    unit_nos: list[int] = []
    for doc_id in sorted(set(doc_ids)):
        found = await conn.execute_fetchall(
            f"SELECT rowid FROM {table} WHERE doc_id = ?",  # noqa: S608
            (doc_id,),
        )
        unit_nos.extend(int(row[0]) for row in found)
    rows: list[Any] = []
    for batch in itertools.batched(sorted(unit_nos), GET_BATCH):
        marks = ", ".join("?" for _ in batch)
        rows.extend(
            await conn.execute_fetchall(
                f"SELECT rowid, vec_distance_cosine(embedding, ?) FROM {table} "  # noqa: S608
                f"WHERE rowid IN ({marks})",
                (query, *batch),
            )
        )
    return rows
