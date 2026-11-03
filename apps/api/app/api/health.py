import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import sysinfo
from app.runner.shutdown import ShutdownCoordinator

router = APIRouter(tags=["health"])


def _lane(stats: dict[str, int | bool]) -> dict[str, int | bool]:
    return {"active": stats["active"], "queued": stats["queued"]}


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    state = request.app.state
    db_ok = await state.db.ping()
    rss = sysinfo.rss_bytes()
    coordinator: ShutdownCoordinator = state.shutdown
    return {
        "status": "ok" if db_ok else "error",
        "rss_mb": None if rss is None else round(rss / 2**20, 1),
        "uptime_sec": int(time.monotonic() - state.started_at),
        # 레인 셋(2단계 3.6, 3단계 6.5). 에이전트 레인은 채팅·과업·agent 노드의 자식 런을,
        # 워크플로우 레인은 워크플로우 런을, 지식 레인은 지식 베이스 문서의 색인·삭제 작업을 돌린다.
        "runner": {
            "agent": _lane(state.supervisor.stats()),
            "workflow": _lane(state.workflow_supervisor.stats()),
            "knowledge": _lane(state.knowledge_supervisor.stats()),
            "shutting_down": coordinator.shutting_down,
        },
        "db": "ok" if db_ok else "error",
    }


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    db_ok = await state.db.ping()
    coordinator: ShutdownCoordinator = state.shutdown
    workers_ok = coordinator.running
    ready = db_ok and workers_ok
    return JSONResponse(
        {
            "status": "ok" if ready else "unavailable",
            "db": "ok" if db_ok else "error",
            "runner": "ok" if workers_ok else "stopped",
        },
        status_code=200 if ready else 503,
    )
