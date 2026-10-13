import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import sysinfo

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    state = request.app.state
    db_ok = await state.db.ping()
    rss = sysinfo.rss_bytes()
    return {
        "status": "ok" if db_ok else "error",
        "rss_mb": None if rss is None else round(rss / 2**20, 1),
        "uptime_sec": int(time.monotonic() - state.started_at),
        "runner": state.supervisor.stats(),
        "db": "ok" if db_ok else "error",
    }


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    db_ok = await state.db.ping()
    workers_ok = state.supervisor.running
    ready = db_ok and workers_ok
    return JSONResponse(
        {
            "status": "ok" if ready else "unavailable",
            "db": "ok" if db_ok else "error",
            "runner": "ok" if workers_ok else "stopped",
        },
        status_code=200 if ready else 503,
    )
