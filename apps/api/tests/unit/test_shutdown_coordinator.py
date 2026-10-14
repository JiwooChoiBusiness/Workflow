"""종료 코디네이터(runner/shutdown.py, 2단계 설계 3.6).

레인 둘이 모두 끝난 뒤에만 종료 훅(이벤트 버스·DB 닫기)이 돈다. 한 레인이 늦게 끝나도 먼저 끝난
레인이 훅을 돌리지 않는다.
"""

import asyncio
import threading

import pytest

from app.runner import winconsole
from app.runner.shutdown import ShutdownCoordinator
from app.runner.supervisor import CancelSignal, Supervisor, SupervisorClosed


async def never_called(run_id: str, cancel: CancelSignal) -> None:
    raise AssertionError(f"unexpected run {run_id}")


async def test_hooks_run_in_order_after_both_lanes_finish() -> None:
    order: list[str] = []
    agent_started = asyncio.Event()
    workflow_started = asyncio.Event()

    async def agent_run(run_id: str, cancel: CancelSignal) -> None:
        agent_started.set()
        await cancel.wait()
        order.append(f"agent:{cancel.reason}")

    async def workflow_run(run_id: str, cancel: CancelSignal) -> None:
        workflow_started.set()
        await cancel.wait()
        # 워크플로우 레인이 더 늦게 끝나도 훅은 그 뒤에 돈다.
        await asyncio.sleep(0.1)
        order.append(f"workflow:{cancel.reason}")

    agent = Supervisor(agent_run, grace_sec=5)
    workflow = Supervisor(workflow_run, grace_sec=5, name="workflow")
    coordinator = ShutdownCoordinator([agent, workflow])

    async def close_bus() -> None:
        order.append("bus.close")

    async def close_db() -> None:
        order.append("db.close")

    coordinator.add_hook(close_bus)
    coordinator.add_hook(close_db)
    agent.start()
    workflow.start()
    agent.enqueue("a-1")
    workflow.enqueue("w-1")
    await asyncio.wait_for(asyncio.gather(agent_started.wait(), workflow_started.wait()), 1)
    assert coordinator.running

    await asyncio.wait_for(coordinator.shutdown(), 2)

    assert order == ["agent:shutdown", "workflow:shutdown", "bus.close", "db.close"]
    assert coordinator.finished.is_set()
    assert not coordinator.running and coordinator.shutting_down
    assert agent.finished.is_set() and workflow.finished.is_set()


async def test_both_lanes_stop_taking_runs_as_soon_as_shutdown_starts() -> None:
    agent = Supervisor(never_called)
    workflow = Supervisor(never_called, name="workflow")
    coordinator = ShutdownCoordinator([agent, workflow])
    agent.start()
    workflow.start()

    coordinator.request_shutdown()

    for lane in (agent, workflow):
        assert lane.shutting_down
        with pytest.raises(SupervisorClosed):
            lane.enqueue("late")
    await coordinator.shutdown()


async def test_grace_from_the_request_reaches_every_lane() -> None:
    cancelled: list[str] = []
    started = asyncio.Event()

    async def stubborn(run_id: str, cancel: CancelSignal) -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(run_id)
            raise

    workflow = Supervisor(stubborn, grace_sec=30, name="workflow")
    coordinator = ShutdownCoordinator([Supervisor(never_called), workflow])
    workflow.start()
    workflow.enqueue("w-1")
    await asyncio.wait_for(started.wait(), 1)

    coordinator.request_shutdown(0.05)
    await asyncio.wait_for(coordinator.shutdown(), 2)

    assert cancelled == ["w-1"]


async def test_a_failing_hook_does_not_stop_the_rest() -> None:
    calls: list[str] = []

    async def broken() -> None:
        raise RuntimeError("훅 실패")

    async def close_db() -> None:
        calls.append("db.close")

    coordinator = ShutdownCoordinator([Supervisor(never_called)])
    coordinator.add_hook(broken)
    coordinator.add_hook(close_db)

    await coordinator.shutdown()

    assert calls == ["db.close"] and coordinator.finished.is_set()


async def test_shutdown_requested_from_another_thread() -> None:
    coordinator = ShutdownCoordinator([Supervisor(never_called), Supervisor(never_called)])
    loop = asyncio.get_running_loop()

    threading.Thread(target=loop.call_soon_threadsafe, args=(coordinator.request_shutdown,)).start()

    assert await asyncio.to_thread(coordinator.finished.wait, 2)
    await coordinator.shutdown()


async def test_console_close_event_waits_for_the_coordinator() -> None:
    hooks: list[str] = []

    async def close_db() -> None:
        hooks.append("db.close")

    coordinator = ShutdownCoordinator([Supervisor(never_called), Supervisor(never_called)])
    coordinator.add_hook(close_db)
    handle = winconsole.make_handler(asyncio.get_running_loop(), coordinator)

    assert await asyncio.to_thread(handle, winconsole.CTRL_C_EVENT) is False
    assert not coordinator.shutting_down
    assert await asyncio.to_thread(handle, winconsole.CTRL_CLOSE_EVENT) is True

    assert coordinator.finished.is_set() and hooks == ["db.close"]
