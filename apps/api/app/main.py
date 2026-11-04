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
from app.housekeeping import (
    Housekeeping,
    KnowledgeRetention,
    Retention,
    SqlRetention,
    WorkflowRetention,
)
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
from app.services.knowledge_index_executor import (
    KB_WORK,
    KnowledgeIndexExecutor,
    KnowledgeLane,
    KnowledgeMaintenance,
)
from app.services.knowledge_runtime import INDEX_DIR, build_knowledge_runtime
from app.services.llm_status import LlmStatusService
from app.services.run_executor import RunExecutor
from app.services.runs import RunMaintenance, RunService
from app.services.sql_runtime import SqlRuntime
from app.services.sql_sources import SqlMaintenance
from app.services.tasks import TaskService
from app.services.workflow_executor import WF_WORKSPACES, WorkflowRunExecutor
from app.services.workflow_runs import WorkflowParents
from app.storage import make_storage
from app.streaming.event_bus import EventBus
from app.streaming.stores import sqlite_stores
from app.web_static import mount_web
from app.workflow import render as workflow_render

log = structlog.get_logger("app.main")

DATA_SUBDIRS = ("storage", "workspaces", WF_WORKSPACES, KB_WORK, INDEX_DIR, "cache/pip", "logs")
PAID_KEY_IN_FREE_POOL = (
    "GEMINI_PAID_API_KEY가 GEMINI_API_KEYS에도 들어 있다. 유료 키는 결제를 연결한"
    " 새 프로젝트의 키여야 하고 무료 목록에서는 빼야 한다"
)

winconsole.ensure_proactor_loop_policy()


class StartupCheckError(RuntimeError):
    pass


def check_paid_key(settings: Settings) -> None:
    # 결제한 키가 무료 순환에 섞이면 무료로 여긴 호출까지 과금되고 무료 등급 약관 구분도 깨진다
    # (3단계 설계 Z1, 14.4). 다른 점검과 달리 경고로 넘기지 않고 기동을 멈춘다.
    if settings.paid_key_in_free_pool():
        log.error(
            "startup.check_failed", check="paid_key_in_free_pool", detail=PAID_KEY_IN_FREE_POOL
        )
        raise StartupCheckError(PAID_KEY_IN_FREE_POOL)


def _lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        check_paid_key(settings)
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
        # 허용 목록과 HTTP 클라이언트는 하나를 만들어 세 백엔드와 워크플로우 노드가 나눠 쓴다
        # (2단계 9.2). 두 번째 백엔드는 워크플로우 런 작업 폴더(DATA_DIR/wf-workspaces)(9.1),
        # 세 번째는 지식 베이스 파싱 작업 폴더(DATA_DIR/kb-work)다(3단계 6.2).
        http = HttpClient(
            Allowlist(settings.allowlist_file), max_body_bytes=settings.http_max_body_bytes
        )
        try:
            backend = make_backend(settings, http_client=http)
            wf_backend = make_backend(
                settings, http_client=http, workspaces_dir=settings.data_dir / WF_WORKSPACES
            )
            kb_backend = make_backend(
                settings, http_client=http, workspaces_dir=settings.data_dir / KB_WORK
            )
        except BaseException:
            await http.aclose()
            raise
        llm: ModelAdapter = app.state.llm
        db = Database(settings.data_dir / "app.db")
        # index.db를 열지 못해도(확장 로드 실패 등) 기동은 계속하고 지식 API만 503이다(3단계 5.2).
        knowledge = build_knowledge_runtime(settings, llm, db=db)
        storage = make_storage(settings)
        bus = EventBus(sqlite_stores(db))
        # Text2SQL 준비·질의는 지식 작업 폴더(kb-work)의 하위 프로세스에서 한 번에 하나씩 돈다
        # (3단계 10.5). 콘솔 시험·SQL 노드·query_sql 도구가 같은 객체를 쓴다.
        sql_runtime = SqlRuntime(settings, kb_backend)
        # search_knowledge가 지식 실행 객체로 검색한다(3단계 8.4).
        executor = RunExecutor(
            db=db,
            settings=settings,
            llm=llm,
            backend=backend,
            storage=storage,
            bus=bus,
            knowledge=knowledge,
            sql_runtime=sql_runtime,
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
        workflow_executor = WorkflowRunExecutor(
            db=db,
            settings=settings,
            bus=bus,
            llm=llm,
            sandbox=wf_backend,
            http=http,
            tasks=tasks,
            children=children,
            storage=storage,
            knowledge=knowledge,
            sql_runtime=sql_runtime,
        )
        workflow_supervisor = Supervisor(
            workflow_executor,
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
        # 지식 레인(세 번째 Supervisor, 동시 1). 몇 시간짜리 인제스트가 워크플로우 레인을 쥐지 않게
        # 따로 두고, 취소·종료·/healthz·복구는 Supervisor의 것을 그대로 쓴다(3단계 6.5).
        index_executor = KnowledgeIndexExecutor(
            db=db,
            settings=settings,
            runtime=knowledge,
            storage=storage,
            sandbox=kb_backend,
            llm=llm,
        )
        knowledge_supervisor = Supervisor(
            index_executor.run,
            max_concurrent=settings.runner_max_concurrent_index_jobs,
            grace_sec=settings.shutdown_grace_sec,
            name="knowledge",
        )
        knowledge_lane = KnowledgeLane(knowledge_supervisor)
        index_executor.lane = knowledge_lane
        # knowledge_index 노드가 만든 문서를 지식 레인에 넣는다(3단계 8.2).
        workflow_executor.knowledge_lane = knowledge_lane
        knowledge_maintenance = KnowledgeMaintenance(
            db=db,
            runtime=knowledge,
            settings=settings,
            storage=storage,
            sandbox=kb_backend,
            lane=knowledge_lane,
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
            knowledge=KnowledgeRetention(knowledge_maintenance),
            sql=SqlRetention(SqlMaintenance(db, settings, storage, sql_runtime)),
        )
        # 종료 훅은 세 레인이 모두 끝난 뒤 등록한 순서로 돈다(2단계 3.6). 콘솔 창 닫기처럼
        # lifespan을 거치지 않는 종료도 같은 순서로 정리되게 여기에 모은다. 런이 다 멈춘 뒤
        # 하우스키핑을 세우고, index.db를 체크포인트하고 닫은 뒤, 이벤트 버스가 남은 이벤트를 쓰고
        # SSE 구독을 닫은 다음 DB가 마지막에 닫힌다.
        shutdown = ShutdownCoordinator([supervisor, workflow_supervisor, knowledge_supervisor])
        for hook in (
            housekeeping.stop,
            backend.aclose,
            wf_backend.aclose,
            sql_runtime.close,
            kb_backend.aclose,
            llm.aclose,
            knowledge.close,
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
            if await knowledge.open():
                # app.db만 옛 백업으로 되돌렸으면 유닛 번호가 index.db의 rowid로 거슬러 가므로
                # 먼저 올리고, 설정이 어긋난 KB(5.4)와 비어 있는 index.db(5.5)를 맞춘다.
                await knowledge_maintenance.raise_unit_counter()
                await knowledge_maintenance.reconcile()
            else:
                log.warning(
                    "startup.check_failed",
                    check="knowledge_index",
                    detail=knowledge.index_db.unavailable_reason,
                )
            app.state.knowledge = knowledge
            # 복구가 run.error·run.queued를 발행하므로 writer를 먼저 띄운다.
            bus.start()
            # 워크플로우 런을 먼저 본다. 끝난 워크플로우 런의 queued 자식을 1단계 복구가 다시 넣기
            # 전에 고아로 닫아야 한다(2단계 3.7의 5번).
            await recovery.recover_workflows(db, workflow_supervisor, bus)
            await recovery.recover(db, supervisor, bus)
            # 지식 레인은 런과 무관하다. 대기·진행 중 문서와 지운 문서의 작업을 다시 넣는다(6.5).
            await recovery.recover_knowledge(db, knowledge_lane)
            # 하위 에이전트를 기다리던 부모 중 자식이 이미 끝난 것(지난 종료 직전에 놓친 훅)을
            # 잇는다.
            await maintenance.reconcile_children()
            supervisor.start()
            workflow_supervisor.start()
            knowledge_supervisor.start()
            housekeeping.start()

            app.state.db = db
            app.state.supervisor = supervisor
            app.state.workflow_supervisor = workflow_supervisor
            app.state.knowledge_supervisor = knowledge_supervisor
            app.state.knowledge_lane = knowledge_lane
            app.state.knowledge_maintenance = knowledge_maintenance
            app.state.shutdown = shutdown
            app.state.wf_parents = parents
            app.state.sandbox = backend
            app.state.wf_sandbox = wf_backend
            app.state.kb_sandbox = kb_backend
            app.state.sql_runtime = sql_runtime
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
                index_workers=settings.runner_max_concurrent_index_jobs,
                knowledge_index=knowledge.available,
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
