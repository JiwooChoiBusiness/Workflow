import asyncio
import signal
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


async def test_queue_positions_and_running_state() -> None:
    gate = asyncio.Event()

    async def run(run_id: str, cancel: CancelSignal) -> None:
        await gate.wait()

    supervisor = Supervisor(run, max_concurrent=1)
    supervisor.start()
    for run_id in ("a", "b", "c"):
        supervisor.enqueue(run_id)
    await asyncio.sleep(0.01)

    assert supervisor.is_running("a")
    assert [supervisor.position(run_id) for run_id in ("a", "b", "c")] == [None, 1, 2]
    assert supervisor.stats()["queued"] == 2
    gate.set()
    await asyncio.sleep(0.01)
    assert supervisor.position("c") is None
    await supervisor.shutdown()


async def test_run_requeued_before_returning_keeps_its_new_signal() -> None:
    # waiting_input으로 끝나는 런에 답이 아주 빨리 와서, 실행 함수가 돌아오기 전에 같은 런이 다시
    # 큐에 들어가는 경우. 새 신호가 지워지면 재개된 런을 중지할 수 없다.
    seen: list[CancelSignal] = []
    finished = asyncio.Event()

    async def run(run_id: str, cancel: CancelSignal) -> None:
        seen.append(cancel)
        if len(seen) == 1:
            supervisor.enqueue(run_id)
        else:
            await cancel.wait()
            finished.set()

    supervisor = Supervisor(run, max_concurrent=1)
    supervisor.start()
    supervisor.enqueue("run-1")
    while len(seen) < 2:
        await asyncio.sleep(0.01)

    assert supervisor.cancel("run-1")
    await asyncio.wait_for(finished.wait(), 1)
    assert seen[0] is not seen[1]
    await supervisor.shutdown()


async def test_exit_signal_chain_starts_shutdown_and_calls_the_previous_handler() -> None:
    calls: list[str] = []

    def previous(signum: int, frame: object) -> None:
        calls.append("previous")

    original = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, previous)
    supervisor = Supervisor(never_called)
    supervisor.start()
    uninstall = winconsole.chain_exit_signals(asyncio.get_running_loop(), supervisor)
    try:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler) and handler is not previous
        handler(signal.SIGTERM, None)
        await asyncio.sleep(0.05)

        assert calls == ["previous"]
        assert supervisor.shutting_down
    finally:
        uninstall()
        assert signal.getsignal(signal.SIGTERM) is previous
        signal.signal(signal.SIGTERM, original)
        await supervisor.shutdown()
