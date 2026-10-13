import asyncio
import sys
import threading
import time

import pytest

from app.runner import winconsole
from app.runner.supervisor import CancelSignal, Supervisor, SupervisorClosed


async def never_called(run_id: str, cancel: CancelSignal) -> None:
    raise AssertionError(f"unexpected run {run_id}")


async def test_idle_shutdown_is_immediate_and_runs_hooks_in_order() -> None:
    calls: list[str] = []

    async def first() -> None:
        calls.append("event-flush")

    async def second() -> None:
        calls.append("db-close")

    supervisor = Supervisor(never_called, max_concurrent=2)
    supervisor.add_shutdown_hook(first)
    supervisor.add_shutdown_hook(second)
    supervisor.start()
    assert supervisor.running

    started = time.monotonic()
    await supervisor.shutdown()

    assert time.monotonic() - started < 0.5
    assert calls == ["event-flush", "db-close"]
    assert supervisor.finished.is_set()
    assert not supervisor.running
    with pytest.raises(SupervisorClosed):
        supervisor.enqueue("run-late")


async def test_shutdown_cancels_active_run_with_reason() -> None:
    started = asyncio.Event()
    seen: list[tuple[str, str | None]] = []

    async def run(run_id: str, cancel: CancelSignal) -> None:
        started.set()
        await cancel.wait()
        seen.append((run_id, cancel.reason))

    supervisor = Supervisor(run, grace_sec=5)
    supervisor.start()
    supervisor.enqueue("run-1")
    await asyncio.wait_for(started.wait(), 1)
    assert supervisor.stats() == {"active": 1, "queued": 0, "shutting_down": False}

    await asyncio.wait_for(supervisor.shutdown(), 1)

    assert seen == [("run-1", "shutdown")]


async def test_run_that_ignores_cancel_is_cancelled_after_grace() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stubborn(run_id: str, cancel: CancelSignal) -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    supervisor = Supervisor(stubborn, grace_sec=0.1)
    supervisor.start()
    supervisor.enqueue("run-1")
    await asyncio.wait_for(started.wait(), 1)

    await asyncio.wait_for(supervisor.shutdown(), 1)

    assert cancelled.is_set()
    assert supervisor.finished.is_set()


async def test_queued_runs_do_not_start_during_shutdown() -> None:
    started: list[str] = []
    first_started = asyncio.Event()

    async def run(run_id: str, cancel: CancelSignal) -> None:
        started.append(run_id)
        first_started.set()
        await cancel.wait()

    supervisor = Supervisor(run, grace_sec=5)
    supervisor.start()
    supervisor.enqueue("run-1")
    supervisor.enqueue("run-2")
    await asyncio.wait_for(first_started.wait(), 1)

    await asyncio.wait_for(supervisor.shutdown(), 1)

    assert started == ["run-1"]


async def test_user_cancel_reaches_the_run() -> None:
    started = asyncio.Event()
    reasons: list[str | None] = []

    async def run(run_id: str, cancel: CancelSignal) -> None:
        started.set()
        await cancel.wait()
        reasons.append(cancel.reason)

    supervisor = Supervisor(run)
    supervisor.start()
    supervisor.enqueue("run-1")
    await asyncio.wait_for(started.wait(), 1)

    assert supervisor.cancel("run-1")
    assert not supervisor.cancel("run-unknown")
    await supervisor.shutdown()
    assert reasons == ["user"]


async def test_shutdown_requested_from_another_thread() -> None:
    supervisor = Supervisor(never_called)
    supervisor.start()
    loop = asyncio.get_running_loop()

    threading.Thread(target=loop.call_soon_threadsafe, args=(supervisor.request_shutdown,)).start()

    assert await asyncio.to_thread(supervisor.finished.wait, 2)
    await supervisor.shutdown()


async def test_console_close_event_waits_for_shutdown() -> None:
    supervisor = Supervisor(never_called)
    supervisor.start()
    handle = winconsole.make_handler(asyncio.get_running_loop(), supervisor)

    assert await asyncio.to_thread(handle, winconsole.CTRL_C_EVENT) is False
    assert await asyncio.to_thread(handle, winconsole.CTRL_BREAK_EVENT) is False
    assert not supervisor.shutting_down

    assert await asyncio.to_thread(handle, winconsole.CTRL_CLOSE_EVENT) is True
    assert supervisor.finished.is_set()


async def test_shutdown_before_start_still_runs_hooks() -> None:
    calls: list[str] = []

    async def hook() -> None:
        calls.append("db-close")

    supervisor = Supervisor(never_called, max_concurrent=2)
    supervisor.add_shutdown_hook(hook)

    await supervisor.shutdown()

    assert calls == ["db-close"]
    assert supervisor.finished.is_set()


async def test_console_handler_installs_and_uninstalls() -> None:
    supervisor = Supervisor(never_called)
    uninstall = winconsole.install(asyncio.get_running_loop(), supervisor)
    uninstall()


def test_reload_warning_only_on_windows() -> None:
    warning = winconsole.reload_warning()

    assert (warning is not None) == (sys.platform == "win32")


@pytest.mark.skipif(sys.platform != "win32", reason="이벤트 루프 정책 차이는 Windows에만 있다")
def test_ensure_proactor_loop_policy_undoes_a_selector_policy() -> None:
    assert sys.platform == "win32"
    original = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        winconsole.ensure_proactor_loop_policy()

        policy = asyncio.get_event_loop_policy()
        assert isinstance(policy, asyncio.WindowsProactorEventLoopPolicy)
    finally:
        asyncio.set_event_loop_policy(original)
