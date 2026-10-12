import asyncio
import contextlib
import os
import signal
import socket
import tempfile
from pathlib import Path

import structlog

from app.config import get_settings
from app.logging import configure_logging

HEARTBEAT_INTERVAL_SEC = 5
# compose healthcheck가 이 파일의 수정 시각으로 runner 생존을 판단한다.
HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "runner-heartbeat"

log = structlog.get_logger("app.runner")


async def heartbeat_loop(stop: asyncio.Event) -> None:
    runner_id = f"{socket.gethostname()}:{os.getpid()}"
    while not stop.is_set():
        HEARTBEAT_FILE.touch()
        log.info("runner.heartbeat", runner_id=runner_id)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SEC)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows 이벤트 루프에는 add_signal_handler가 없다. 로컬에서는 Ctrl+C로 끝낸다.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("runner.started", max_concurrent_runs=settings.runner_max_concurrent_runs)
    await heartbeat_loop(stop)
    log.info("runner.stopped")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
