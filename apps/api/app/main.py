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
from app.housekeeping import Housekeeping, Retention, WorkflowRetention
from app.llm import make_adapter
from app.llm.base import ModelAdapter
from app.logsetup import configure_logging
from app.runner import recovery, winconsole
from app.runner.shutdown import ShutdownCoordinator
from app.runner.supervisor import Supervisor
from app.sandbox import allowlist, make_backend
from app.sandbox.allowlist import Allowlist
from app.sandbox.http_client import HttpClient
from app.security.rate_limit import new_api_limiter
from app.services.child_runs import ChildRunService
from app.services.llm_status import LlmStatusService
from app.services.run_executor import RunExecutor
from app.services.runs import RunMaintenance, RunService
from app.services.tasks import TaskService
from app.services.workflow_executor import WF_WORKSPACES, WorkflowRunExecutor
from app.services.workflow_runs import WorkflowParents
from app.storage import make_storage
from app.streaming.event_bus import EventBus
from app.streaming.stores import sqlite_stores
from app.web_static import mount_web
from app.workflow import render as workflow_render

log = structlog.get_logger("app.main")

DATA_SUBDIRS = ("storage", "workspaces", WF_WORKSPACES, "cache/pip", "logs")

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
        # 과업 지시와 워크플로우 노드가 함께 쓰는 Jinja2 환경을 한 번만 만든다(2단계 설계 3.3).
        workflow_render.setup()

        # SANDBOX_BACKEND=e2b면 여기서 기동이 실패한다(설계 3.1).
        # 허용 목록과 HTTP 클라이언트는 하나를 만들어 두 백엔드와 워크플로우 노드가 나눠 쓴다
        # (2단계 9.2). 두 번째 백엔드는 워크플로우 런 작업 폴더(DATA_DIR/wf-workspaces)다(9.1).
        http = HttpClient(
            Allowlist(settings.allowlist_file), max_body_bytes=settings.http_max_body_bytes
        )
        try:
            backend = make_backend(settings, http_client=http)
            wf_backend = make_backend(
                settings, http_client=http, workspaces_dir=settings.data_dir / WF_WORKSPACES
            )
        except BaseException:
            await http.aclose()
            raise
        llm: ModelAdapter = app.state.llm
        db = Database(settings.data_dir / "app.db")
        storage = make_storage(settings)
        bus = EventBus(sqlite_stores(db))
        executor = RunExecutor(
            db=db, settings=settings, llm=llm, backend=backend, storage=storage, bus=bus
        )
        supervisor = Supervisor(
            executor,
            max_concurrent=settings.runner_max_concurrent_runs,
            grace_sec=settings.shutdown_grace_sec,
            name="agent",
        )
        # 실행기가 하위 에이전트 런과 재개하는 부모를 같은 레인에 넣는다(오케스트레이션 3.3).
        executor.supervisor = supervisor
        # 워크플로우 런의 agent 노드는 자식 런을 ChildRunService로 만들어 에이전트 레인에 넣고 이
        # 과업 서비스로 기다린다(오케스트레이션 9장).
        tasks = TaskService(
            db,
            settings,
            runs=RunService(
                db, settings, bus=bus, supervisor=supervisor, storage=storage, backend=backend
            ),
            bus=bus,
        )
        children = ChildRunService(
            db, settings, bus=bus, supervisor=supervisor, storage=storage, backend=backend
        )
        workflow_supervisor = Supervisor(
            WorkflowRunExecutor(
                db=db,
                settings=settings,
                bus=bus,
                llm=llm,
                sandbox=wf_backend,
                http=http,
                tasks=tasks,
                children=children,
                storage=storage,
            ),
            max_concurrent=settings.runner_max_concurrent_workflow_runs,
            grace_sec=settings.shutdown_grace_sec,
            name="workflow",
        )
        parents = WorkflowParents(
            db, bus, workflow_supervisor, waiting_ttl_hours=settings.waiting_input_ttl_hours
        )
        maintenance = RunMaintenance(
            db, settings, bus=bus, storage=storage, parents=parents, supervisor=supervisor
        )
        housekeeping = Housekeeping(
            db=db,
            backend=backend,
            memory_budget_mb=settings.memory_budget_mb,
            runs=maintenance,
            retention=Retention(db, storage=storage, backend=backend),
            workflows=WorkflowRetention(
                db,
                storage=storage,
                backend=wf_backend,
                retention_days=settings.run_event_retention_days,
                forms=parents,
            ),
        )
        # 종료 훅은 두 레인이 모두 끝난 뒤 등록한 순서로 돈다(2단계 3.6). 콘솔 창 닫기처럼
        # lifespan을 거치지 않는 종료도 같은 순서로 정리되게 여기에 모은다. 런이 다 멈춘 뒤
        # 하우스키핑을 세우고, 이벤트 버스가 남은 이벤트를 쓰고 SSE 구독을 닫은 다음 DB가 마지막에
        # 닫힌다.
        shutdown = ShutdownCoordinator([supervisor, workflow_supervisor])
        for hook in (
            housekeeping.stop,
            backend.aclose,
            wf_backend.aclose,
            llm.aclose,
            bus.close,
            db.close,
        ):
            shutdown.add_hook(hook)
        loop = asyncio.get_running_loop()
        uninstall_console_handler = winconsole.install(loop, shutdown)
        uninstall_signal_chain = winconsole.chain_exit_signals(loop, shutdown)
        # 기동 도중 실패해도 finally가 엔진을 닫아야 한다.
        # 열린 aiosqlite 스레드가 남으면 프로세스가 끝나지 않는다.
        try:
            await db.connect()
            # 혼자 쓰는 로컬 앱이라 기동할 때마다 최신 스키마로 올린다(설계 2.6).
            # 복구가 run 테이블을 읽으므로 그보다 먼저 한다.
            await upgrade_head(db)
            # 복구가 run.error·run.queued를 발행하므로 writer를 먼저 띄운다.
            bus.start()
            # 워크플로우 런을 먼저 본다. 끝난 워크플로우 런의 queued 자식을 1단계 복구가 다시 넣기
            # 전에 고아로 닫아야 한다(2단계 3.7의 5번).
            await recovery.recover_workflows(db, workflow_supervisor, bus)
            await recovery.recover(db, supervisor, bus)
            # 하위 에이전트를 기다리던 부모 중 자식이 이미 끝난 것(지난 종료 직전에 놓친 훅)을
            # 잇는다.
            await maintenance.reconcile_children()
            supervisor.start()
            workflow_supervisor.start()
            housekeeping.start()

            app.state.db = db
            app.state.supervisor = supervisor
            app.state.workflow_supervisor = workflow_supervisor
            app.state.shutdown = shutdown
            app.state.wf_parents = parents
            app.state.sandbox = backend
            app.state.wf_sandbox = wf_backend
            app.state.http = http
            app.state.storage = storage
            app.state.bus = bus
            app.state.started_at = started_at
            rss = sysinfo.rss_bytes()
            log.info(
                "server.ready",
                port=settings.server_port,
                rss_mb=None if rss is None else round(rss / 2**20, 1),
                workers=settings.runner_max_concurrent_runs,
                workflow_workers=settings.runner_max_concurrent_workflow_runs,
                loop=type(asyncio.get_running_loop()).__name__,
                subprocess_ok=subprocess_check.ok,
            )
            yield
        finally:
            await shutdown.shutdown()
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
