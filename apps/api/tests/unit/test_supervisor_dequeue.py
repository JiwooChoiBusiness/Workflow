"""Supervisor.dequeue(3단계 설계 6.5). 큐에서 뺀 런은 _work가 꺼내도 돌지 않는다(지식 레인의
pause)."""

import asyncio

from app.runner.supervisor import CancelSignal, Supervisor


class Recorder:
    """첫 런은 release가 켜질 때까지 붙잡아 둔다. 그동안 뒤의 런은 큐에 머문다."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, run_id: str, cancel: CancelSignal) -> None:
        self.ran.append(run_id)
        if len(self.ran) == 1:
            self.started.set()
            await self.release.wait()


async def drain(supervisor: Supervisor) -> None:
    for _ in range(50):
        await asyncio.sleep(0)
        if not supervisor.stats()["active"] and not supervisor.stats()["queued"]:
            return


async def test_a_dequeued_run_is_skipped_when_the_worker_takes_it() -> None:
    record = Recorder()
    supervisor = Supervisor(record)
    supervisor.start()
    supervisor.enqueue("first")
    await asyncio.wait_for(record.started.wait(), 1)
    supervisor.enqueue("paused")
    supervisor.enqueue("last")

    assert supervisor.dequeue("paused")
    assert supervisor.position("paused") is None
    assert supervisor.position("last") == 1
    assert supervisor.stats()["queued"] == 1
    record.release.set()
    await drain(supervisor)

    assert record.ran == ["first", "last"]
    await supervisor.shutdown()


async def test_a_run_put_back_after_dequeue_runs_once() -> None:
    record = Recorder()
    supervisor = Supervisor(record)
    supervisor.start()
    supervisor.enqueue("first")
    await asyncio.wait_for(record.started.wait(), 1)
    supervisor.enqueue("doc")

    assert supervisor.dequeue("doc")
    supervisor.enqueue("other")
    supervisor.enqueue("doc")
    assert supervisor.position("doc") == 2
    record.release.set()
    await drain(supervisor)

    # 옛 항목은 버리고 다시 넣은 항목만 그 자리에서 돈다.
    assert record.ran == ["first", "other", "doc"]
    await supervisor.shutdown()


async def test_running_or_unknown_runs_are_not_dequeued() -> None:
    record = Recorder()
    supervisor = Supervisor(record)
    supervisor.start()
    supervisor.enqueue("first")
    await asyncio.wait_for(record.started.wait(), 1)

    assert not supervisor.dequeue("first")
    assert not supervisor.dequeue("missing")
    assert supervisor.is_running("first")
    assert supervisor.running_keys() == ["first"]
    record.release.set()
    await drain(supervisor)
    await supervisor.shutdown()
