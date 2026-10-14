"""EventBus.open_channel(오케스트레이션 6.1). 재기동 뒤 하위 에이전트를 기다리는 부모 런의 스트림이
기록만 주고 닫히지 않고 live로 이어지는지 본다."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest

from app.streaming.event_bus import BusEvent, EventBus, RunRef
from app.streaming.sse import sse_stream


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[BusEvent] = []

    async def write(self, events: Sequence[BusEvent]) -> None:
        self.rows.extend(events)

    async def load(
        self, ref: RunRef, *, after: int, before: int | None = None, limit: int | None = None
    ) -> list[BusEvent]:
        rows = sorted(
            (
                row
                for row in self.rows
                if row.run_id == ref.run_id
                and row.seq > after
                and (before is None or row.seq < before)
            ),
            key=lambda row: row.seq,
        )
        return rows[:limit] if limit is not None else rows

    async def last_seq(self, ref: RunRef) -> int:
        return max((row.seq for row in self.rows if row.run_id == ref.run_id), default=0)


def make_ref() -> RunRef:
    return RunRef(run_id=uuid.uuid4(), session_id=uuid.uuid4(), workspace_id=uuid.uuid4())


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
async def bus(store: MemoryStore) -> AsyncIterator[EventBus]:
    instance = EventBus(store, batch_window_sec=0.01, drop_delay_sec=0.05)
    instance.start()
    yield instance
    await instance.close()


async def persisted_history(store: MemoryStore, ref: RunRef) -> None:
    # 이전 프로세스가 남긴 기록. 지금 프로세스에는 채널이 없다(재기동 직후).
    other = EventBus(store)
    await other.publish(ref, "run.queued", {"position": 1})
    await other.publish(
        ref,
        "child_run.started",
        {
            "tool_call_id": "c",
            "child_run_id": str(uuid.uuid4()),
            "child_session_id": str(uuid.uuid4()),
            "agent_id": str(uuid.uuid4()),
            "agent_slug": "researcher",
            "agent_name": "조사",
            "version": "published",
            "depth": 1,
            "result_format": "text",
            "task_preview": "t",
        },
    )
    await other.close()


async def test_without_a_channel_the_subscription_only_replays(
    bus: EventBus, store: MemoryStore
) -> None:
    ref = make_ref()
    await persisted_history(store, ref)

    subscription = bus.subscribe(ref, after=0)

    assert subscription.live is False
    events = [event async for event in subscription.events()]
    assert [event.type for event in events] == ["run.queued", "child_run.started"]


async def test_open_channel_makes_the_subscription_live_and_seq_continues(
    bus: EventBus, store: MemoryStore
) -> None:
    ref = make_ref()
    await persisted_history(store, ref)

    bus.open_channel(ref)
    subscription = bus.subscribe(ref, after=0)
    assert subscription.live is True and bus.is_live(ref.run_id)
    received: list[BusEvent] = []

    async def read() -> None:
        async for event in subscription.events():
            received.append(event)

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.05)
    # 기록을 다 준 뒤에도 끝나지 않고 다음 발행을 기다린다.
    assert not reader.done()
    assert [event.type for event in received] == ["run.queued", "child_run.started"]
    [completed] = await bus.publish(
        ref,
        "child_run.completed",
        {
            "tool_call_id": "c",
            "child_run_id": str(uuid.uuid4()),
            "status": "succeeded",
            "steps": 1,
            "usage_total": {},
            "duration_ms": 5,
            "files": 0,
        },
    )
    assert completed.seq == 3
    await bus.publish(ref, "run.completed", {"status": "succeeded", "usage_total": {}, "steps": 2})
    await asyncio.wait_for(reader, 2)
    assert [event.seq for event in received] == [1, 2, 3, 4]


async def test_only_pings_flow_while_the_channel_is_quiet(bus: EventBus) -> None:
    ref = make_ref()
    bus.open_channel(ref)
    subscription = bus.subscribe(ref, after=0)
    chunks: list[bytes] = []

    async def read() -> None:
        async for chunk in sse_stream(subscription, ping_interval=0.02):
            chunks.append(chunk)

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.1)
    assert chunks and all(chunk.startswith(b"event: ping") for chunk in chunks)
    await bus.publish(ref, "run.completed", {"status": "cancelled", "usage_total": {}, "steps": 0})
    await asyncio.wait_for(reader, 2)
    assert chunks[-1].startswith(b"id: 1\nevent: run.completed")


async def test_open_channel_revives_an_ended_channel_and_end_drops_it(bus: EventBus) -> None:
    ref = make_ref()
    await bus.publish(ref, "run.queued", {"position": 1})
    bus.end(ref.run_id)
    assert not bus.is_live(ref.run_id)

    bus.open_channel(ref)
    assert bus.is_live(ref.run_id)
    await asyncio.sleep(0.1)
    # 되살린 채널은 drop 타이머가 꺼져 있어 남는다.
    assert bus.is_live(ref.run_id)

    bus.end(ref.run_id)
    await asyncio.sleep(0.1)
    assert not bus.is_live(ref.run_id)
    assert bus.subscribe(ref, after=0).live is False


async def test_open_channel_after_close_is_a_no_op(store: MemoryStore) -> None:
    instance = EventBus(store)
    await instance.close()
    ref = make_ref()

    instance.open_channel(ref)

    assert not instance.is_live(ref.run_id)
