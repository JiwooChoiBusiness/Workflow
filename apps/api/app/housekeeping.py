import asyncio

import structlog

from app import sysinfo
from app.auth.session_store import SessionStore
from app.db.engine import Database
from app.sandbox.base import SandboxBackend

log = structlog.get_logger("app.housekeeping")

MEMORY_CHECK_INTERVAL_SEC = 60.0
HOURLY_INTERVAL_SEC = 3600.0


class Housekeeping:
    def __init__(
        self,
        *,
        db: Database,
        backend: SandboxBackend,
        memory_budget_mb: int,
        interval_sec: float = MEMORY_CHECK_INTERVAL_SEC,
        hourly_interval_sec: float = HOURLY_INTERVAL_SEC,
    ) -> None:
        self._sessions = SessionStore(db)
        self._backend = backend
        self._budget_mb = memory_budget_mb
        self._interval_sec = interval_sec
        self._hourly_interval_sec = hourly_interval_sec
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._memory_loop(), name="housekeeping-memory"),
                asyncio.create_task(self._hourly_loop(), name="housekeeping-hourly"),
            ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    def check_memory(self) -> float | None:
        rss = sysinfo.rss_bytes()
        if rss is None:
            return None
        rss_mb = rss / 2**20
        # 예산을 넘어도 재시작하지 않는다. 로그로 알리기만 한다.
        if rss_mb > self._budget_mb:
            log.warning("memory.over_budget", rss_mb=round(rss_mb, 1), budget_mb=self._budget_mb)
        return rss_mb

    async def run_hourly(self) -> None:
        # 한 작업이 실패해도 나머지는 돈다. 실패한 작업은 다음 시간에 다시 한다.
        expired: int | None = None
        purged: int | None = None
        try:
            expired = await self._sessions.purge_expired()
        except Exception:
            log.exception("housekeeping.login_sessions_failed")
        try:
            purged = await self._backend.purge_deleted()
        except Exception:
            log.exception("housekeeping.workspaces_failed")
        log.info("housekeeping.hourly", expired_login_sessions=expired, purged_workspaces=purged)

    async def _memory_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_sec)
            self.check_memory()

    async def _hourly_loop(self) -> None:
        # 켜고 끄기를 자주 하는 개인 PC라 한 시간을 다 기다리지 않고 기동 직후에도 한 번 돈다.
        while True:
            await self.run_hourly()
            await asyncio.sleep(self._hourly_interval_sec)
