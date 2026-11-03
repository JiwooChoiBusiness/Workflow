import asyncio
import re
import unicodedata
from collections.abc import AsyncIterator, Collection, Sequence
from typing import Protocol
from uuid import UUID

from app.knowledge.index_db import (
    IndexDb,
    delete_rowids,
    iter_rowids,
    set_stop_bigrams,
    table_exists,
    upsert_kb_row,
)
from app.knowledge.tokens import HAN_KANA_RANGES, HANGUL_RANGES
from app.knowledge.units import FtsHit, FtsRow, fts_table_name

# 질의에서 그 KB의 문서 빈도가 5%를 넘는 bigram을 뺀다. KorQuAD 1만 문단에서 MRR@10이 0.907 → 0.900,
# 지연은 4분의 1이었다(설계 5.3).
STOP_DF_RATIO = 0.05
# 작은 KB에서 5%는 문서 몇 개라 주제어까지 빠진다. 가지치기의 이득은 속도뿐이라 유닛이 이보다
# 적으면 하지 않는다.
STOP_MIN_UNITS = 1000
# 긴 질의(노드가 넘긴 글)가 OR 수백 개가 되지 않게 앞에서부터 이만큼만 쓴다.
MAX_QUERY_TERMS = 128

_RUN = re.compile(
    f"(?P<hangul>[{HANGUL_RANGES}]+)"
    f"|(?P<han_kana>[{HAN_KANA_RANGES}]+)"
    rf"|(?P<word>[^\s{HANGUL_RANGES}{HAN_KANA_RANGES}]+)"
)
# 단어 구간의 첫 글자·숫자부터 마지막 글자·숫자까지. 앞뒤 괄호·문장 부호는 뗀다.
_WORD_CORE = re.compile(r"[^\W_](?:.*[^\W_])?", re.DOTALL)


def bigram_tokens(text: str) -> list[str]:
    """한글·한자·가나 구간은 두 글자씩 겹쳐 자르고(한 글자 구간은 그대로) 영문·숫자 단어는
    통째로 둔다(설계 5.3).

    "ERR-4417"처럼 기호가 든 단어는 그대로 두고 unicode61이 err, 4417로 나눈다. 질의에서는 따옴표로
    감싸므로 두 낱말이 붙어 있는 구절로 찾는다. 단어 앞뒤의 괄호·문장 부호는 떼고, 글자·숫자가
    없는 기호 덩어리는 버린다.
    """
    text = unicodedata.normalize("NFC", text).lower()
    tokens: list[str] = []
    for match in _RUN.finditer(text):
        run = match.group()
        if match.lastgroup == "word":
            core = _WORD_CORE.search(run)
            if core is not None:
                tokens.append(core.group())
        elif len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def bigram_text(text: str) -> str:
    return " ".join(bigram_tokens(text))


def query_terms(query: str, stop: Collection[str] = ()) -> list[str]:
    terms = list(dict.fromkeys(bigram_tokens(query)))
    # 전부 가지치기되면 가지치기 없이 보낸다.
    kept = [term for term in terms if term not in stop] or terms
    return kept[:MAX_QUERY_TERMS]


def match_expression(terms: Sequence[str]) -> str:
    # 낱말마다 따옴표로 감싸 사용자 입력의 AND·NOT·*가 연산자로 해석되지 않게 한다.
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


class FtsIndex(Protocol):
    async def ensure(self, kb_id: UUID, *, workspace_id: UUID) -> None: ...
    async def drop(self, kb_id: UUID) -> None: ...
    async def upsert(self, kb_id: UUID, rows: Sequence[FtsRow]) -> None: ...
    async def delete(self, kb_id: UUID, unit_nos: Collection[int]) -> None: ...
    async def search(self, kb_id: UUID, query: str, k: int) -> list[FtsHit]: ...
    async def count(self, kb_id: UUID) -> int: ...
    def list_unit_nos(self, kb_id: UUID) -> AsyncIterator[int]: ...
    async def refresh_stop_bigrams(self, kb_id: UUID) -> tuple[str, ...]: ...


class SqliteFtsIndex:
    """KB마다 FTS5 테이블 fts_<kb hex> 하나. rowid가 unit_no이고 열은 bigram으로 바꾼 body다.

    SQLite 3.43 이상이면 원문을 저장하지 않는 contentless_delete 테이블, 그 아래면 본문을 저장하는
    테이블이다. 두 모드의 질의·삭제 SQL은 같다.
    """

    def __init__(
        self,
        db: IndexDb,
        *,
        stop_ratio: float = STOP_DF_RATIO,
        stop_min_units: int = STOP_MIN_UNITS,
    ) -> None:
        self._db = db
        self._stop_ratio = stop_ratio
        self._stop_min_units = stop_min_units
        self._stop_cache: dict[UUID, frozenset[str]] = {}

    async def ensure(self, kb_id: UUID, *, workspace_id: UUID) -> None:
        table = fts_table_name(kb_id)
        options = "content='', contentless_delete=1, " if self._db.fts_contentless else ""
        async with self._db.write() as conn:
            await conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} "
                f"USING fts5(body, {options}tokenize='unicode61')"
            )
            await upsert_kb_row(conn, kb_id, workspace_id, self._db.now(), fts_table=table)

    async def drop(self, kb_id: UUID) -> None:
        async with self._db.write() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {fts_table_name(kb_id)}")
            await conn.execute(
                "UPDATE kb_index SET fts_table = NULL, stop_bigrams = '[]', unit_count = 0 "
                "WHERE kb_id = ?",
                (str(kb_id),),
            )
        self._stop_cache.pop(kb_id, None)

    async def upsert(self, kb_id: UUID, rows: Sequence[FtsRow]) -> None:
        if not rows:
            return
        table = fts_table_name(kb_id)
        # 세그먼트 200행이면 글이 수백 KB라 변환을 이벤트 루프 밖에서 한다.
        bodies = await asyncio.to_thread(lambda: [bigram_text(row.text) for row in rows])
        params = [(row.unit_no, body) for row, body in zip(rows, bodies, strict=True)]
        async with self._db.write() as conn:
            if not await table_exists(conn, table):
                raise LookupError(f"전문 색인 테이블이 없다(ensure 먼저): {kb_id}")
            await delete_rowids(conn, table, [row.unit_no for row in rows])
            await conn.executemany(
                f"INSERT INTO {table} (rowid, body) VALUES (?, ?)",  # noqa: S608
                params,
            )

    async def delete(self, kb_id: UUID, unit_nos: Collection[int]) -> None:
        if not unit_nos:
            return
        table = fts_table_name(kb_id)
        async with self._db.write() as conn:
            if await table_exists(conn, table):
                await delete_rowids(conn, table, list(unit_nos))

    async def search(self, kb_id: UUID, query: str, k: int) -> list[FtsHit]:
        if k < 1:
            raise ValueError("k는 1 이상이다")
        terms = query_terms(query, await self._stop_terms(kb_id))
        if not terms:
            return []
        table = fts_table_name(kb_id)
        async with self._db.read() as conn:
            if not await table_exists(conn, table):
                return []
            rows = await conn.execute_fetchall(
                f"SELECT rowid, bm25({table}) FROM {table} "  # noqa: S608
                f"WHERE {table} MATCH ? ORDER BY bm25({table}) LIMIT ?",
                (match_expression(terms), k),
            )
        return [FtsHit(unit_no=int(row[0]), score=-float(row[1])) for row in rows]

    async def count(self, kb_id: UUID) -> int:
        table = fts_table_name(kb_id)
        async with self._db.read() as conn:
            if not await table_exists(conn, table):
                return 0
            rows = await conn.execute_fetchall(f"SELECT count(*) FROM {table}")  # noqa: S608
        return int(next(iter(rows))[0])

    async def list_unit_nos(self, kb_id: UUID) -> AsyncIterator[int]:
        async for unit_no in iter_rowids(self._db, fts_table_name(kb_id)):
            yield unit_no

    async def refresh_stop_bigrams(self, kb_id: UUID) -> tuple[str, ...]:
        # 인제스트 작업이 끝날 때마다 다시 센다. fts5vocab은 contentless 테이블에서도 색인만 읽는다.
        table = fts_table_name(kb_id)
        vocab = f"{table}_vocab"
        terms: tuple[str, ...] = ()
        async with self._db.write() as conn:
            if not await table_exists(conn, table):
                return ()
            rows = await conn.execute_fetchall(f"SELECT count(*) FROM {table}")  # noqa: S608
            total = int(next(iter(rows))[0])
            if total >= self._stop_min_units:
                await conn.execute(
                    f"CREATE VIRTUAL TABLE temp.{vocab} USING fts5vocab(main, {table}, row)"
                )
                try:
                    found = await conn.execute_fetchall(
                        f"SELECT term FROM temp.{vocab} WHERE doc > ? "  # noqa: S608
                        "ORDER BY doc DESC, term",
                        (total * self._stop_ratio,),
                    )
                finally:
                    await conn.execute(f"DROP TABLE temp.{vocab}")
                terms = tuple(str(row[0]) for row in found)
            await set_stop_bigrams(conn, kb_id, terms, self._db.now())
        self._stop_cache[kb_id] = frozenset(terms)
        return terms

    async def _stop_terms(self, kb_id: UUID) -> frozenset[str]:
        cached = self._stop_cache.get(kb_id)
        if cached is None:
            meta = await self._db.describe(kb_id)
            cached = frozenset(meta.stop_bigrams) if meta is not None else frozenset[str]()
            self._stop_cache[kb_id] = cached
        return cached
