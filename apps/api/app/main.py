# ruff: noqa: E402
import os
import sys

if __name__ == "__main__" and sys.path[0] == os.path.dirname(os.path.abspath(__file__)):
    # 파일을 직접 실행하면(VS Code 실행 버튼) 이 폴더가 import 경로 맨 앞에 와서
    # app/logging.py가 표준 logging 모듈을 가린다. 패키지는 가상환경에 설치돼 있으니 빼도 된다.
    sys.path.pop(0)

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import structlog
from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import sysinfo
from app.api import console, health, v1, web
from app.api.console.auth import new_login_limiter
from app.api.errors import install_error_handlers
from app.config import Settings, get_settings
from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.housekeeping import Housekeeping, Retention
from app.llm import make_adapter
from app.llm.base import ModelAdapter
from app.logging import configure_logging
from app.runner import recovery, winconsole
from app.runner.supervisor import Supervisor
from app.sandbox import allowlist, make_backend
from app.security.rate_limit import new_api_limiter
from app.services.llm_status import LlmStatusService
from app.services.run_executor import RunExecutor
from app.services.runs import RunMaintenance
from app.storage import make_storage
from app.streaming.event_bus import EventBus, SqliteEventStore
from app.web_static import mount_web

log = structlog.get_logger("app.main")

DATA_SUBDIRS = ("storage", "workspaces", "cache/pip", "logs")

winconsole.ensure_proactor_loop_policy()


def _lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started_at = time.monotonic()
        for name in DATA_SUBDIRS:
            (settings.data_dir / name).mkdir(parents=True, exist_ok=True)
        if allowlist.ensure_default_file(settings.allowlist_file):
            log.info("allowlist.created", path=str(settings.allowlist_file))
        # 실패해도 기동은 계속한다. 셸이 없으면 shell 도구만 못 쓴다.
        checks = await asyncio.to_thread(sysinfo.startup_checks, settings)
        # 서브프로세스 점검은 서버가 쓰는 이 루프에서 해야 의미가 있어 스레드로 보내지 않는다.
        subprocess_check = await sysinfo.check_subprocess()
        for check in [*checks, subprocess_check]:
            if not check.ok:
                log.warning("startup.check_failed", check=check.name, detail=check.detail)

        # SANDBOX_BACKEND=e2b면 여기서 기동이 실패한다(설계 3.1).
        backend = make_backend(settings)
        llm: ModelAdapter = app.state.llm
        db = Database(settings.data_dir / "app.db")
        storage = make_storage(settings)
        bus = EventBus(SqliteEventStore(db))
        executor = RunExecutor(
            db=db, settings=settings, llm=llm, backend=backend, storage=storage, bus=bus
        )
        supervisor = Supervisor(
            executor,
            max_concurrent=settings.runner_max_concurrent_runs,
            grace_sec=settings.shutdown_grace_sec,
        )
        housekeeping = Housekeeping(
            db=db,
            backend=backend,
            memory_budget_mb=settings.memory_budget_mb,
            runs=RunMaintenance(db, settings, bus=bus, storage=storage),
            retention=Retention(db, storage=storage, backend=backend),
        )
        # 종료 훅은 등록한 순서로 돈다. 콘솔 창 닫기처럼 lifespan을 거치지 않는 종료도 같은
        # 순서로 정리되게 여기에 모은다. 런이 다 멈춘 뒤 하우스키핑을 세우고, 이벤트 버스가 남은
        # 이벤트를 쓰고 SSE 구독을 닫은 다음 DB가 마지막에 닫힌다.
        for hook in (housekeeping.stop, backend.aclose, llm.aclose, bus.close, db.close):
            supervisor.add_shutdown_hook(hook)
        loop = asyncio.get_running_loop()
        uninstall_console_handler = winconsole.install(loop, supervisor)
        uninstall_signal_chain = winconsole.chain_exit_signals(loop, supervisor)
        # 기동 도중 실패해도 finally가 엔진을 닫아야 한다.
        # 열린 aiosqlite 스레드가 남으면 프로세스가 끝나지 않는다.
        try:
            await db.connect()
            # 혼자 쓰는 로컬 앱이라 기동할 때마다 최신 스키마로 올린다(설계 2.6).
            # 복구가 run 테이블을 읽으므로 그보다 먼저 한다.
            await upgrade_head(db)
            # 복구가 run.error·run.queued를 발행하므로 writer를 먼저 띄운다.
            bus.start()
            await recovery.recover(db, supervisor, bus)
            supervisor.start()
            housekeeping.start()

            app.state.db = db
            app.state.supervisor = supervisor
            app.state.sandbox = backend
            app.state.storage = storage
            app.state.bus = bus
            app.state.started_at = started_at
            rss = sysinfo.rss_bytes()
            log.info(
                "server.ready",
                port=settings.server_port,
                rss_mb=None if rss is None else round(rss / 2**20, 1),
                workers=settings.runner_max_concurrent_runs,
                loop=type(asyncio.get_running_loop()).__name__,
                subprocess_ok=subprocess_check.ok,
            )
            yield
        finally:
            await supervisor.shutdown()
            uninstall_signal_chain()
            uninstall_console_handler()
            log.info("server.stopped")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        settings.log_level,
        settings.data_dir / "logs" / "app.log",
        secrets=settings.secret_values(),
    )

    app = FastAPI(title="platform-api", lifespan=_lifespan(settings))
    app.state.settings = settings
    app.state.login_limiter = new_login_limiter()
    app.state.api_limiter = new_api_limiter()
    # openai SDK는 첫 모델 호출 때 불러온다(설계 2.7). 여기서는 어댑터 객체만 만든다.
    app.state.llm = make_adapter(settings)
    app.state.llm_status = LlmStatusService(settings, app.state.llm)
    install_error_handlers(app)
    # Starlette는 Host 헤더에서 포트를 떼고 호스트 이름만 비교한다. 서버가 127.0.0.1:<port>에만
    # 바인드하므로 결과적으로 127.0.0.1:<port>, localhost:<port>만 통과한다.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.include_router(health.router)
    app.include_router(console.router)
    app.include_router(web.router)
    app.include_router(v1.router)
    mount_web(app, settings.web_dist_dir)
    return app


if __name__ == "__main__":
    # VS Code에서 이 파일을 실행 버튼으로 띄울 때. 동작은 `platform serve`와 같다.
    from app.cli import serve

    raise SystemExit(serve())
