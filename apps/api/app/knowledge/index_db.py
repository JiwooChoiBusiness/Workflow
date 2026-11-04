import asyncio
import itertools
import json
import sqlite3
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args
from uuid import UUID

import aiosqlite
import structlog

from app.knowledge.units import IndexMeta, fts_table_name, vec_table_name

log = structlog.get_logger("app.knowledge")

SCHEMA_VERSION = 1
# vec0 KNN이 4KB 페이지보다 2.2배 빠르다(설계 5.1). 파일 속성이라 WAL로 바꾸기 전에 정해야 한다.
PAGE_SIZE = 65536
DEFAULT_CACHE_KB = 8000
DEFAULT_READERS = 2
BUSY_TIMEOUT_MS = 5000
# contentless_delete=1이 3.43부터 된다. 그 아래는 본문을 저장하는 FTS5로 만든다(5.3).
CONTENTLESS_MIN_SQLITE = (3, 43, 0)
# vec0의 메타데이터 열 IN(doc_id IN (...))은 3.38부터 된다[실측]. 그 아래는 rowid로 찾는다.
DOC_FILTER_IN_MIN_SQLITE = (3, 38, 0)
# app.db(app/db/types.py)와 같은 꼴. 고정 폭이라 문자열 정렬이 시간 정렬과 같다.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
DELETE_BATCH = 500
LIST_PAGE = 10_000

ERR_INDEX_UNAVAILABLE = "index_unavailable"
CheckpointMode = Literal["PASSIVE", "FULL", "RESTART", "TRUNCATE"]

_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """
        CREATE TABLE kb_index (
            kb_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            vec_table TEXT,
            fts_table TEXT,
            dims INTEGER,
            config_key TEXT,
            embedding_version INTEGER NOT NULL DEFAULT 0,
            unit_count INTEGER NOT NULL DEFAULT 0,
            stop_bigrams TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE embed_cache (
            config_key TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            vector BLOB NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (config_key, content_hash)
        )
        """,
        "CREATE INDEX ix_embed_cache_created_at ON embed_cache (created_at)",
    ),
)
_KB_COLUMNS = (
    "kb_id, workspace_id, vec_table, fts_table, dims, config_key, embedding_version, "
    "unit_count, stop_bigrams, updated_at"
)


class IndexUnavailable(RuntimeError):
    # 지식 API가 503 index_unavailable로 바꾼다. 나머지 기능은 영향이 없다(5.2).
    code = ERR_INDEX_UNAVAILABLE


def contentless_supported(version: Sequence[int] | None = None) -> bool:
    return tuple(version or sqlite3.sqlite_version_info) >= CONTENTLESS_MIN_SQLITE


def doc_filter_in_supported(version: Sequence[int] | None = None) -> bool:
    return tuple(version or sqlite3.sqlite_version_info) >= DOC_FILTER_IN_MIN_SQLITE


def default_extension_path() -> str:
    import sqlite_vec

    return sqlite_vec.loadable_path()


def format_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class IndexDb:
    """data/knowledge/index.db. app.db와 따로 두는 파생 데이터 파일이다(설계 5장).

    연결은 writer 1개(이 객체의 락으로 직렬화)와 reader 여러 개다. aiosqlite가 연결마다 스레드를
    쓰므로 KNN 스캔(100~400ms)이 이벤트 루프를 막지 않는다.
    """

    def __init__(
        self,
        path: Path,
        *,
        cache_kb: int = DEFAULT_CACHE_KB,
        readers: int = DEFAULT_READERS,
        fts_contentless: bool | None = None,
        doc_filter_in: bool | None = None,
        extension_path: Callable[[], str] = default_extension_path,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        if readers < 1:
            raise ValueError("reader 연결은 하나 이상이어야 한다")
        self.path = path
        self._cache_kb = cache_kb
        self._reader_count = readers
        self._contentless_override = fts_contentless
        self._extension_path = extension_path
        self.now = now
        self._writer: aiosqlite.Connection | None = None
        # None은 close()가 기다리던 reader를 깨우려고 넣는 표시다.
        self._readers: asyncio.Queue[aiosqlite.Connection | None] = asyncio.Queue()
        self._read_waiters = 0
        self._connections: list[aiosqlite.Connection] = []
        self._write_lock = asyncio.Lock()
        self._opened = False
        self._closed = False
        self.available = False
        self.unavailable_reason: str | None = None
        self.fts_contentless = False
        # False면 문서 필터 검색이 doc_id IN 대신 rowid로 찾는다(SQLite 3.38 미만).
        self.doc_filter_in = doc_filter_in_supported() if doc_filter_in is None else doc_filter_in
        self.page_size = 0
        self.schema_version = 0

    @property
    def sqlite_version(self) -> str:
        return sqlite3.sqlite_version

    async def open(self) -> bool:
        """연다. 확장 로드나 스키마 문제는 예외 대신 available=False와 경고로 알린다."""
        if self._opened:
            return self.available
        self._opened = True
        try:
            extension = self._extension_path()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._writer = await self._connect(extension, query_only=False, setup=True)
            problem = await self._migrate(self._writer)
            if problem is None:
                for _ in range(self._reader_count):
                    self._readers.put_nowait(await self._connect(extension, query_only=True))
        except (ImportError, OSError, AttributeError, sqlite3.Error) as exc:
            # 휠이 없거나 정책으로 DLL 로드가 막힌 경우 등. 지식 기능만 쓸 수 없다.
            problem = f"{type(exc).__name__}: {exc}"
        if problem is not None:
            await self._close_connections()
            self._mark_unavailable(problem)
            return False
        self.available = True
        log.info(
            "knowledge.index_opened",
            path=str(self.path),
            sqlite_version=self.sqlite_version,
            schema_version=self.schema_version,
            page_size=self.page_size,
            fts_contentless=self.fts_contentless,
        )
        return True

    def ensure_available(self) -> None:
        if self._closed:
            raise IndexUnavailable("index.db가 닫혔다")
        if not self.available:
            raise IndexUnavailable(self.unavailable_reason or "index.db를 열지 않았다")

    @asynccontextmanager
    async def write(self) -> AsyncIterator[aiosqlite.Connection]:
        # 처음부터 쓰기 잠금을 잡는다(BEGIN IMMEDIATE). 바깥 프로세스(백업 CLI)와는
        # busy_timeout으로 만난다.
        self.ensure_available()
        async with self._write_lock:
            # 잠금을 기다리는 사이에 close()가 시작됐을 수 있다.
            self.ensure_available()
            conn = self._require_writer()
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                await conn.execute("COMMIT")
            except BaseException:
                await _rollback(conn)
                raise

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        self.ensure_available()
        self._read_waiters += 1
        try:
            conn = await self._readers.get()
        finally:
            self._read_waiters -= 1
        if conn is None:
            # 닫히는 중이다. 연결이 돌아오기를 기다리지 않는다.
            raise IndexUnavailable("index.db가 닫히는 중이다")
        try:
            yield conn
        finally:
            self._readers.put_nowait(conn)

    async def checkpoint(self, mode: CheckpointMode = "PASSIVE") -> tuple[int, int, int]:
        # PRAGMA에는 바인드 변수를 쓸 수 없어 정해진 값만 문자열에 넣는다.
        if mode not in get_args(CheckpointMode):
            raise ValueError(f"알 수 없는 체크포인트 모드: {mode!r}")
        self.ensure_available()
        async with self._write_lock:
            self.ensure_available()
            row = await _fetchone(self._require_writer(), f"PRAGMA wal_checkpoint({mode})")
        if row is None:
            raise RuntimeError("PRAGMA wal_checkpoint가 결과 행을 돌려주지 않았다")
        return int(row[0]), int(row[1]), int(row[2])

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # reader 연결을 기다리던 검색이 영영 깨어나지 않는 일이 없게 기다리는 수만큼 깨운다.
        for _ in range(self._read_waiters):
            self._readers.put_nowait(None)
        try:
            if self.available and self._writer is not None:
                async with self._write_lock:
                    await self._writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            log.warning("knowledge.index_checkpoint_failed", error=str(exc))
        finally:
            self.available = False
            await self._close_connections()

    def disk_bytes(self) -> int | None:
        """index.db와 WAL 파일 크기의 합(KB 통계). 모든 KB가 한 파일을 나눠 쓴다. 없으면 None."""
        sizes: list[int] = []
        for path in (self.path, self.path.with_name(self.path.name + "-wal")):
            try:
                sizes.append(path.stat().st_size)
            except OSError:
                continue
        return sum(sizes) if sizes else None

    async def describe(self, kb_id: UUID) -> IndexMeta | None:
        async with self.read() as conn:
            return await read_kb_row(conn, kb_id)

    async def list_kbs(self) -> list[IndexMeta]:
        async with self.read() as conn:
            rows = await conn.execute_fetchall(
                f"SELECT {_KB_COLUMNS} FROM kb_index ORDER BY kb_id"  # noqa: S608
            )
        return [_meta_from_row(row) for row in rows]

    async def drop_kb(self, kb_id: UUID) -> None:
        # KB 삭제. 벡터·전문 색인 테이블과 kb_index 행을 함께 지운다.
        async with self.write() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {vec_table_name(kb_id)}")
            await conn.execute(f"DROP TABLE IF EXISTS {fts_table_name(kb_id)}")
            await conn.execute("DELETE FROM kb_index WHERE kb_id = ?", (str(kb_id),))

    async def refresh_unit_count(self, kb_id: UUID) -> int:
        # 모든 유닛에 전문 색인 행이 있으므로(Economical 포함) FTS 행 수를 센다.
        async with self.write() as conn:
            meta = await read_kb_row(conn, kb_id)
            if meta is None:
                return 0
            count = 0
            if meta.fts_table and await table_exists(conn, meta.fts_table):
                row = await _fetchone(conn, f"SELECT count(*) FROM {meta.fts_table}")  # noqa: S608
                count = int(row[0]) if row else 0
            await conn.execute(
                "UPDATE kb_index SET unit_count = ?, updated_at = ? WHERE kb_id = ?",
                (count, format_time(self.now()), str(kb_id)),
            )
        return count

    def _require_writer(self) -> aiosqlite.Connection:
        if self._writer is None:
            raise IndexUnavailable("index.db를 열지 않았다")
        return self._writer

    def _mark_unavailable(self, reason: str) -> None:
        self.available = False
        self.unavailable_reason = reason
        log.warning("knowledge.index_unavailable", path=str(self.path), reason=reason)

    def _pragmas(self, *, query_only: bool) -> list[str]:
        pragmas = [
            "PRAGMA synchronous=NORMAL",
            # mmap을 켜면 Windows 작업 집합(RSS)이 파일 크기만큼 는다(10만 벡터에서 347~390MB).
            "PRAGMA mmap_size=0",
            f"PRAGMA cache_size=-{int(self._cache_kb)}",
            f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
            "PRAGMA temp_store=MEMORY",
        ]
        if query_only:
            pragmas.append("PRAGMA query_only=1")
        return pragmas

    async def _connect(
        self, extension: str, *, query_only: bool, setup: bool = False
    ) -> aiosqlite.Connection:
        # isolation_level=None: sqlite3가 DML 앞에 몰래 BEGIN을 넣지 않게 한다. 트랜잭션은
        # write()가 연다.
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._connections.append(conn)
        await conn.enable_load_extension(True)
        try:
            await conn.load_extension(extension)
        finally:
            await conn.enable_load_extension(False)
        if setup:
            row = await _fetchone(conn, "PRAGMA page_count")
            if row is not None and int(row[0]) == 0:
                await conn.execute(f"PRAGMA page_size={PAGE_SIZE}")
            await conn.execute("PRAGMA journal_mode=WAL")
        for pragma in self._pragmas(query_only=query_only):
            await conn.execute(pragma)
        return conn

    async def _migrate(self, conn: aiosqlite.Connection) -> str | None:
        # 문제가 있으면 그 사유를 돌려준다. 스키마 버전은 Alembic 밖에서 이 파일이 관리한다(5.5).
        await conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        stored = await _get_meta(conn, "schema_version")
        version = int(stored) if stored else 0
        if version > SCHEMA_VERSION:
            # 더 새 코드가 만든 파일이다. 지우지 않고 지식 기능만 끈다.
            return f"index.db schema_version {version}이 코드의 {SCHEMA_VERSION}보다 높다"
        for target in range(version + 1, SCHEMA_VERSION + 1):
            await conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _MIGRATIONS[target - 1]:
                    await conn.execute(statement)
                await _set_meta(conn, "schema_version", str(target))
                await conn.execute("COMMIT")
            except BaseException:
                await _rollback(conn)
                raise
        self.schema_version = SCHEMA_VERSION
        row = await _fetchone(conn, "PRAGMA page_size")
        self.page_size = int(row[0]) if row else 0
        contentless = contentless_supported()
        if self._contentless_override is not None:
            contentless = self._contentless_override and contentless
        previous = await _get_meta(conn, "fts_contentless")
        if not contentless:
            log.warning(
                "knowledge.fts_contentless_unavailable",
                sqlite_version=self.sqlite_version,
                existing_contentless_tables=previous == "1",
            )
        self.fts_contentless = contentless
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if await _get_meta(conn, "created_at") is None:
                await _set_meta(conn, "created_at", format_time(self.now()))
            await _set_meta(conn, "page_size", str(self.page_size))
            # 이 값은 앞으로 만들 전문 색인 테이블의 모드다. 질의 코드는 두 모드가 같다.
            await _set_meta(conn, "fts_contentless", "1" if contentless else "0")
            await conn.execute("COMMIT")
        except BaseException:
            await _rollback(conn)
            raise
        return None

    async def _close_connections(self) -> None:
        connections, self._connections = self._connections, []
        self._writer = None
        while not self._readers.empty():
            self._readers.get_nowait()
        for conn in connections:
            try:
                await conn.close()
            except (sqlite3.Error, ValueError) as exc:
                log.warning("knowledge.index_close_failed", error=str(exc))


async def _fetchone(
    conn: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()
) -> sqlite3.Row | tuple[Any, ...] | None:
    async with conn.execute(sql, params) as cursor:
        return await cursor.fetchone()


async def _rollback(conn: aiosqlite.Connection) -> None:
    try:
        await conn.execute("ROLLBACK")
    except sqlite3.Error as exc:
        # BEGIN 자체가 실패했으면 열린 트랜잭션이 없다.
        log.debug("knowledge.index_rollback_skipped", error=str(exc))


async def _get_meta(conn: aiosqlite.Connection, key: str) -> str | None:
    row = await _fetchone(conn, "SELECT value FROM meta WHERE key = ?", (key,))
    return None if row is None else str(row[0])


async def _set_meta(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


async def get_meta(db: IndexDb, key: str) -> str | None:
    async with db.read() as conn:
        return await _get_meta(conn, key)


async def set_meta(db: IndexDb, key: str, value: str) -> None:
    async with db.write() as conn:
        await _set_meta(conn, key, value)


async def max_rowid(conn: aiosqlite.Connection, table: str) -> int:
    row = await _fetchone(conn, f"SELECT max(rowid) FROM {table}")  # noqa: S608
    return int(row[0]) if row is not None and row[0] is not None else 0


async def table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    row = await _fetchone(
        conn, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    )
    return row is not None


async def delete_rowids(conn: aiosqlite.Connection, table: str, rowids: Sequence[int]) -> int:
    deleted = 0
    for batch in itertools.batched(rowids, DELETE_BATCH):
        marks = ", ".join("?" for _ in batch)
        async with conn.execute(
            f"DELETE FROM {table} WHERE rowid IN ({marks})",  # noqa: S608
            batch,
        ) as cursor:
            deleted += max(cursor.rowcount, 0)
    return deleted


async def iter_rowids(db: IndexDb, table: str) -> AsyncIterator[int]:
    # 고아 정리용. 한 번에 다 들고 있지 않고, 쪽 사이에는 reader 연결을 돌려준다.
    last = -(2**63)
    while True:
        async with db.read() as conn:
            if not await table_exists(conn, table):
                return
            rows = list(
                await conn.execute_fetchall(
                    f"SELECT rowid FROM {table} WHERE rowid > ? "  # noqa: S608
                    "ORDER BY rowid LIMIT ?",
                    (last, LIST_PAGE),
                )
            )
        for row in rows:
            yield int(row[0])
        if len(rows) < LIST_PAGE:
            return
        last = int(rows[-1][0])


async def read_kb_row(conn: aiosqlite.Connection, kb_id: UUID) -> IndexMeta | None:
    row = await _fetchone(
        conn,
        f"SELECT {_KB_COLUMNS} FROM kb_index WHERE kb_id = ?",  # noqa: S608
        (str(kb_id),),
    )
    return None if row is None else _meta_from_row(row)


async def upsert_kb_row(
    conn: aiosqlite.Connection,
    kb_id: UUID,
    workspace_id: UUID,
    now: datetime,
    **fields: object,
) -> None:
    # 호출하는 쪽의 write() 트랜잭션 안에서 부른다. 다른 워크스페이스의 행을 덮어쓰지 않는다.
    existing = await read_kb_row(conn, kb_id)
    if existing is not None and existing.workspace_id != workspace_id:
        raise ValueError(f"kb_index {kb_id}는 다른 워크스페이스의 행이다")
    allowed = {"vec_table", "fts_table", "dims", "config_key", "embedding_version"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"kb_index에 없는 열: {sorted(unknown)}")
    stamp = format_time(now)
    if existing is None:
        columns = ["kb_id", "workspace_id", "updated_at", *fields]
        values = [str(kb_id), str(workspace_id), stamp, *fields.values()]
        marks = ", ".join("?" for _ in columns)
        await conn.execute(
            f"INSERT INTO kb_index ({', '.join(columns)}) VALUES ({marks})",  # noqa: S608
            values,
        )
        return
    assignments = ", ".join(f"{name} = ?" for name in [*fields, "updated_at"])
    await conn.execute(
        f"UPDATE kb_index SET {assignments} WHERE kb_id = ?",  # noqa: S608
        [*fields.values(), stamp, str(kb_id)],
    )


async def set_stop_bigrams(
    conn: aiosqlite.Connection, kb_id: UUID, terms: Sequence[str], now: datetime
) -> None:
    await conn.execute(
        "UPDATE kb_index SET stop_bigrams = ?, updated_at = ? WHERE kb_id = ?",
        (json.dumps(list(terms), ensure_ascii=False), format_time(now), str(kb_id)),
    )


def _meta_from_row(row: sqlite3.Row | tuple[Any, ...]) -> IndexMeta:
    stop: object = json.loads(row[8]) if row[8] else []
    return IndexMeta(
        kb_id=UUID(row[0]),
        workspace_id=UUID(row[1]),
        vec_table=row[2],
        fts_table=row[3],
        dims=row[4],
        config_key=row[5],
        embedding_version=int(row[6]),
        unit_count=int(row[7]),
        stop_bigrams=tuple(str(term) for term in stop) if isinstance(stop, list) else (),
        updated_at=parse_time(row[9]),
    )
