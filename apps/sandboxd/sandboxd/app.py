import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

_started_at = time.monotonic()


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "uptime_sec": int(time.monotonic() - _started_at)})


app = Starlette(routes=[Route("/health", health)])
