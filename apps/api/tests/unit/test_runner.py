import asyncio
from pathlib import Path

import pytest

from app.runner import __main__ as runner


async def test_heartbeat_loop_touches_file_and_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "runner-heartbeat"
    monkeypatch.setattr(runner, "HEARTBEAT_FILE", heartbeat)

    stop = asyncio.Event()
    task = asyncio.create_task(runner.heartbeat_loop(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert heartbeat.exists()
