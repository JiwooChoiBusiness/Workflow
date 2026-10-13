import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import URL, Connection, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import AsyncAdaptedQueuePool

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-8000",
)
_WRITE_OPTION = "platform_write"


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
    # sqlite3가 DML 앞에 몰래 넣는 BEGIN을 끄고 트랜잭션 시작은 _on_begin이 직접 보낸다.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        for pragma in PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def _on_begin(conn: Connection) -> None:
    # 쓰기 트랜잭션은 시작부터 쓰기 잠금을 잡아 읽기→쓰기 승급 중의 'database is locked'를 없앤다.
    # 읽기는 DEFERRED로 두어 WAL에서 다른 읽기·쓰기와 서로 막지 않게 한다.
    if conn.get_execution_options().get(_WRITE_OPTION):
        conn.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        conn.exec_driver_sql("BEGIN")


def _checkpoint(conn: Connection) -> None:
    # SQLAlchemy 트랜잭션 밖에서 실행해야 하므로 DBAPI 커서를 직접 쓴다.
    cursor = conn.connection.cursor()
    try:
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        cursor.close()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        # aiosqlite는 연결마다 스레드를 하나 쓰므로 풀을 작게 묶는다.
        self.engine = create_async_engine(
            URL.create("sqlite+aiosqlite", database=str(path)),
            poolclass=AsyncAdaptedQueuePool,
            pool_size=3,
            max_overflow=0,
        )
        event.listen(self.engine.sync_engine, "connect", _on_connect)
        event.listen(self.engine.sync_engine, "begin", _on_begin)
        self._write_engine = self.engine.execution_options(**{_WRITE_OPTION: True})
        self._read_sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._write_sessions = async_sessionmaker(self._write_engine, expire_on_commit=False)
        # 프로세스가 하나이므로 쓰기는 이 락 하나로 직렬화한다.
        # busy_timeout은 백업 CLI 같은 외부 접근에 대한 보험이다.
        self._write_lock = asyncio.Lock()
        self._closed = False

    async def connect(self) -> None:
        async with self.read() as session:
            await session.execute(text("SELECT 1"))

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncSession]:
        self._ensure_open()
        async with self._read_sessions() as session:
            yield session

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncSession]:
        self._ensure_open()
        async with self._write_lock, self._write_sessions() as session, session.begin():
            yield session

    @asynccontextmanager
    async def write_connection(self) -> AsyncIterator[AsyncConnection]:
        # 세션 없이 연결을 직접 다뤄야 하는 쓰기(마이그레이션)용. 트랜잭션은 부르는 쪽이 열고,
        # write()와 같은 락을 잡으며 BEGIN IMMEDIATE로 시작한다.
        self._ensure_open()
        async with self._write_lock, self._write_engine.connect() as conn:
            yield conn

    async def ping(self) -> bool:
        if self._closed:
            return False
        try:
            await self.connect()
        except (SQLAlchemyError, OSError):
            return False
        return True

    def _ensure_open(self) -> None:
        # 닫은 뒤 새 연결이 생기면 aiosqlite의 non-daemon 스레드 때문에 프로세스가 끝나지 않는다.
        if self._closed:
            raise RuntimeError("database is closed")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            async with self.engine.connect() as conn:
                await conn.run_sync(_checkpoint)
        finally:
            # 마지막 연결이 닫힐 때 SQLite가 -wal, -shm 파일을 지운다.
            await self.engine.dispose()
