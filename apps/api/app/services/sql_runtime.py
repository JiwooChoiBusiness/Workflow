"""Text2SQL 실행 객체(3단계 설계 10.2, 10.5). lifespan이 하나 만들어 콘솔·노드·도구가 같이 쓴다.

세 번째 로컬 백엔드(DATA_DIR/kb-work)의 작업 폴더에서 준비·질의·런타임 설치 스크립트를 돌린다.
질의와 준비는 세마포어 하나로 한 번에 하나만 돈다(code 노드와 같은 방식, 지식 레인은 쓰지 않는다).
작업 폴더는 작업마다 새 uuid 폴더이고 끝나면 지운다. 매시 정리(kb-work 1시간)가 남은 것을 거둔다.

DuckDB 런타임(패키지와 excel·encodings 확장)은 "런타임 준비"가 뒤에서 받고 상태는 폴링으로 본다.
sqlite 소스는 샌드박스 파이썬의 sqlite3만 쓰므로 런타임이 없어도 돈다.
"""

import asyncio
import contextlib
import shutil
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import structlog

from app.config import Settings
from app.db.types import utcnow
from app.knowledge.sql.runner import (
    EXTENSIONS,
    EXTENSIONS_MARKER,
    RUNTIME_SPECS,
    SqlRunFailed,
    extensions_dir,
    install_runtime,
    runtime_dir,
)
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.sandbox.disk import measure
from app.schemas.sql_source import SqlRuntimeFailure, SqlRuntimeStatus
from app.workflow.extract_runner import READY_MARKER

log = structlog.get_logger("app.services.sql_runtime")

KB_SQL_CACHE = ("cache", "kb-sql")
ERR_RUNTIME_MISSING = "sql_runtime_missing"
ERR_INSTALL_TIMEOUT = "sql_runtime_install_timeout"


class SqlRuntimeMissing(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ERR_RUNTIME_MISSING
        self.message = message


def _ready(path: Path) -> bool:
    return path.is_file()


class SqlRuntime:
    def __init__(
        self,
        settings: Settings,
        sandbox: SandboxBackend | None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        # DATA_DIR/kb-work의 세 번째 로컬 백엔드. 없으면(시험) 준비·질의가 sql_failed다.
        self.sandbox = sandbox
        self.cache_dir = settings.data_dir.joinpath(*KB_SQL_CACHE)
        self._clock = clock
        self._slot: asyncio.Semaphore | None = None
        self._start_lock: asyncio.Lock | None = None
        self._install: asyncio.Task[None] | None = None
        self._failure: SqlRuntimeFailure | None = None
        self._installed_at: datetime | None = None

    @property
    def packages_dir(self) -> Path:
        return runtime_dir(self.cache_dir)

    @property
    def extensions_dir(self) -> Path:
        return extensions_dir(self.cache_dir)

    def database_path(self, source_id: uuid.UUID) -> Path:
        return self.cache_dir / f"{source_id}.duckdb"

    def copy_path(self, source_id: uuid.UUID) -> Path:
        """sqlite 소스의 숨김을 뺀 사본(10.5). 숨긴 표·열이 없으면 만들지 않는다."""
        return self.cache_dir / f"{source_id}.sqlite"

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        # 세마포어는 이 루프에서 처음 쓸 때 만든다(lifespan 밖에서 만든 객체를 시험이 다른 루프에서
        # 쓰지 않게).
        if self._slot is None:
            self._slot = asyncio.Semaphore(1)
        async with self._slot:
            yield

    def _flags(self) -> tuple[bool, bool]:
        return (
            _ready(self.packages_dir / READY_MARKER),
            _ready(self.extensions_dir / EXTENSIONS_MARKER),
        )

    async def ready_lib(self) -> Path:
        """DuckDB 패키지 폴더. 패키지나 확장이 없으면 SqlRuntimeMissing이다."""
        packages, extensions = await asyncio.to_thread(self._flags)
        if not (packages and extensions):
            raise SqlRuntimeMissing(
                "DuckDB 런타임이 없다. 데이터 소스 화면에서 런타임을 먼저 준비한다"
                "(인터넷 연결이 필요하다)"
            )
        return self.packages_dir

    @property
    def installing(self) -> bool:
        return self._install is not None and not self._install.done()

    async def status(self) -> SqlRuntimeStatus:
        packages, extensions = await asyncio.to_thread(self._flags)
        size: int | None = None
        if packages or extensions:
            measured = await asyncio.to_thread(
                lambda: (
                    measure(self.packages_dir).total_bytes
                    + measure(self.extensions_dir).total_bytes
                )
            )
            size = measured
        if self.installing:
            state = "installing"
        elif packages and extensions:
            state = "ready"
        elif self._failure is not None:
            state = "failed"
        else:
            state = "missing"
        return SqlRuntimeStatus(
            state=state,
            packages=list(RUNTIME_SPECS),
            extensions=list(EXTENSIONS),
            packages_ready=packages,
            extensions_ready=extensions,
            bytes=size,
            installed_at=self._installed_at,
            error=self._failure if state == "failed" else None,
        )

    async def start_install(self) -> SqlRuntimeStatus:
        """런타임 설치를 뒤에서 시작한다. 이미 돌고 있거나 받아 두었으면 지금 상태만 돌려준다."""
        # 표시 파일을 읽는 await 사이에 두 번째 요청이 들어와 태스크가 둘 생기지 않게 잠근다.
        # 루프에서 처음 쓸 때 만든다(slot과 같은 이유).
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if not self.installing:
                packages, extensions = await asyncio.to_thread(self._flags)
                if not (packages and extensions):
                    self._failure = None
                    self._install = asyncio.create_task(self._run_install(), name="sql-runtime")
        return await self.status()

    async def _run_install(self) -> None:
        started = self._clock()
        limit = self.settings.sql_runtime_install_timeout_sec
        # pip과 확장 받기에도 각자 시간 상한이 있지만 합치면 15분이고, 응답 없는 하위 프로세스가 그
        # 상한에 걸리지 않으면 installing이 끝나지 않는다. 넘으면 취소가 exec의 정리를 거쳐 프로세스
        # 트리를 끝내고 작업 폴더를 지운다.
        deadline = asyncio.timeout(limit)
        try:
            async with deadline, self.work() as handle:
                _, version = await install_runtime(
                    self.backend(), handle, self.cache_dir, exec_prefix="sql-runtime"
                )
        except TimeoutError as exc:
            if deadline.expired():
                self._failure = SqlRuntimeFailure(
                    code=ERR_INSTALL_TIMEOUT,
                    message=(
                        f"{limit}초 안에 끝나지 않아 설치를 멈췄다. "
                        "인터넷 연결을 확인하고 다시 받는다"
                    ),
                )
                log.warning("sql.runtime_install_timeout", seconds=limit)
            else:
                # 설치 안쪽에서 난 다른 시간 초과(파일 옮기기 등)는 상한 초과가 아니라 설치 실패다.
                self._failure = SqlRuntimeFailure(
                    code="sql_runtime_install_failed", message=f"설치 중 시간 초과: {exc}"
                )
                log.warning("sql.runtime_install_failed", code="timeout", error=str(exc))
            return
        except SqlRunFailed as exc:
            self._failure = SqlRuntimeFailure(code=exc.code, message=exc.message)
            log.warning("sql.runtime_install_failed", code=exc.code, error=exc.message)
            return
        except SandboxError as exc:
            self._failure = SqlRuntimeFailure(
                code="sql_runtime_install_failed", message=exc.message
            )
            log.warning("sql.runtime_install_failed", code=exc.code, error=exc.message)
            return
        self._installed_at = self._clock()
        log.info(
            "sql.runtime_installed",
            duckdb=version,
            seconds=round((self._installed_at - started).total_seconds(), 1),
        )

    def backend(self) -> SandboxBackend:
        if self.sandbox is None:
            raise SandboxError("sandbox_unavailable", "SQL을 실행할 샌드박스가 없다")
        return self.sandbox

    @asynccontextmanager
    async def work(self) -> AsyncIterator[WorkspaceHandle]:
        """작업 하나의 폴더(DATA_DIR/kb-work/<uuid>). 나가면 지운다."""
        sandbox = self.backend()
        handle = await sandbox.acquire(uuid.uuid4())
        try:
            yield handle
        finally:
            try:
                await sandbox.release(handle, delete=True)
            except SandboxError as exc:
                log.warning("sql.work_dir_delete_failed", error=exc.message)

    async def install_database(self, built: Path, source_id: uuid.UUID) -> int:
        """작업 폴더에서 만든 질의 파일을 cache/kb-sql/<id>.duckdb로 옮긴다. 크기를 돌려준다."""
        return await self._place(built, self.database_path(source_id))

    async def install_copy(self, built: Path, source_id: uuid.UUID) -> int:
        """작업 폴더에서 만든 sqlite 사본을 cache/kb-sql/<id>.sqlite로 옮긴다."""
        return await self._place(built, self.copy_path(source_id))

    async def _place(self, built: Path, target: Path) -> int:
        def move() -> int:
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.part")
            shutil.move(str(built), str(partial))
            partial.replace(target)
            return target.stat().st_size

        return await asyncio.to_thread(move)

    async def remove_database(self, source_id: uuid.UUID) -> None:
        """질의 파일과 사본을 지운다. 숨김을 바꾸면 옛 파일이 읽히지 않게 바로 부른다."""
        for target in (self.database_path(source_id), self.copy_path(source_id)):
            with contextlib.suppress(OSError):
                await asyncio.to_thread(target.unlink, True)

    async def close(self) -> None:
        task = self._install
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
