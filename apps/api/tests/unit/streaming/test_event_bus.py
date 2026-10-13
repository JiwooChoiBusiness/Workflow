"""이벤트 버스: seq, 델타 분할, 링버퍼·SQLite 재생, 배치 기록, 채널 폐기, SSE(설계 2.4, 6.5)."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import pytest

from app.streaming.event_bus import BusEvent, EventBus, RunRef, jsonable, split_utf8
from app.streaming.sse import format_event, format_ping, sse_stream


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[BusEvent] = []
        self.batches: list[int] = []

    async def write(self, events: Sequence[BusEvent]) -> None:
        self.batches.append(len(events))
        self.rows.extend(events)

    async def load(
        self, ref: RunRef, *, after: int, before: int | None = None, limit: int | None = None
    ) -> list[BusEvent]:
        rows = [
            row
            for row in self.rows
            if row.run_id == ref.run_id and row.seq > after and (before is None or row.seq < before)
        ]
        rows.sort(key=lambda row: row.seq)
        return rows[:limit] if limit is not None else rows

    async def last_seq(self, ref: RunRef) -> int:
        return max((row.seq for row in self.rows if row.run_id == ref.run_id), default=0)


def make_ref() -> RunRef:
    return RunRef(run_id=uuid.uuid4(), session_id=uuid.uuid4(), workspace_id=uuid.uuid4())


@pytest.fixture
async def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
async def bus(store: MemoryStore) -> AsyncIterator[EventBus]:
    instance = EventBus(store, batch_window_sec=0.01, drop_delay_sec=0.05)
    instance.start()
    yield instance
    await instance.close()


async def collect(subscription: object, timeout: float = 2.0) -> list[BusEvent]:
    events: list[BusEvent] = []

    async def run() -> None:
        async for event in subscription.events():  # type: ignore[attr-defined]
            events.append(event)

    await asyncio.wait_for(run(), timeout)
    return events


async def test_seq_starts_at_one_and_envelope_has_common_fields(bus: EventBus) -> None:
    ref = make_ref()

    [first] = await bus.publish(ref, "run.queued", {"position": 1})
    [second] = await bus.publish(ref, "step.started", {"step": 1})

    assert (first.seq, second.seq) == (1, 2)
    envelope = json.loads(first.line)
    assert envelope == {
        "run_id": str(ref.run_id),
        "session_id": str(ref.session_id),
        "seq": 1,
        "ts": envelope["ts"],
        "type": "run.queued",
        "data": {"position": 1},
    }
    assert envelope["ts"].endswith("Z")


async def test_deltas_are_split_into_4kb_pieces(bus: EventBus, store: MemoryStore) -> None:
    ref = make_ref()
    text = "한글" * 3000

    pieces = await bus.publish(ref, "message.delta", {"message_id": uuid.uuid4(), "text": text})

    assert len(pieces) > 1
    assert all(len(piece.data["text"].encode()) <= 4096 for piece in pieces)
    assert "".join(piece.data["text"] for piece in pieces) == text
    assert [piece.seq for piece in pieces] == list(range(1, len(pieces) + 1))
    await bus.flush()
    # 델타는 저장하지 않는다.
    assert store.rows == []


async def test_persisted_events_are_written_in_batches(store: MemoryStore) -> None:
    bus = EventBus(store, batch_window_sec=0.2, batch_max=3)
    bus.start()
    ref = make_ref()
    try:
        for step in range(7):
            await bus.publish(ref, "step.started", {"step": step})
        await bus.publish(ref, "thinking.delta", {"text": "x"})
        await bus.flush()
    finally:
        await bus.close()

    assert [row.seq for row in store.rows] == list(range(1, 8))
    assert all(size <= 3 for size in store.batches)
    assert sum(store.batches) == 7


async def test_live_subscription_follows_until_run_completed(bus: EventBus) -> None:
    ref = make_ref()
    await bus.publish(ref, "run.queued", {"position": 1})
    subscription = bus.subscribe(ref, after=0)

    async def produce() -> None:
        await asyncio.sleep(0.01)
        await bus.publish(ref, "message.delta", {"message_id": uuid.uuid4(), "text": "안녕"})
        await bus.publish(ref, "run.completed", {"status": "succeeded"})
        await bus.publish(ref, "step.started", {"step": 99})

    producer = asyncio.create_task(produce())
    events = await collect(subscription)
    await producer

    assert [event.type for event in events] == ["run.queued", "message.delta", "run.completed"]


async def test_late_subscriber_gets_buffered_events_after_seq(bus: EventBus) -> None:
    ref = make_ref()
    await bus.publish(ref, "run.queued", {"position": 1})
    await bus.publish(ref, "message.delta", {"message_id": uuid.uuid4(), "text": "가"})
    await bus.publish(ref, "message.delta", {"message_id": uuid.uuid4(), "text": "나"})
    subscription = bus.subscribe(ref, after=2)
    await bus.publish(ref, "run.completed", {"status": "succeeded"})

    events = await collect(subscription)

    assert [(event.seq, event.type) for event in events] == [
        (3, "message.delta"),
        (4, "run.completed"),
    ]


async def test_evicted_buffer_falls_back_to_persisted_events(store: MemoryStore) -> None:
    bus = EventBus(store, ring_size=4, batch_window_sec=0.01)
    bus.start()
    ref = make_ref()
    try:
        for step in range(1, 5):
            await bus.publish(ref, "step.started", {"step": step})
            await bus.publish(ref, "thinking.delta", {"text": f"t{step}"})
        subscription = bus.subscribe(ref, after=0)
        await bus.publish(ref, "run.completed", {"status": "succeeded"})
        events = await collect(subscription)
    finally:
        await bus.close()

    # seq 1~4는 버퍼에서 밀려나 SQLite의 영속 이벤트(step.started)만 다시 온다. 5~8은 버퍼에서 온다.
    assert [(event.seq, event.type) for event in events] == [
        (1, "step.started"),
        (3, "step.started"),
        (5, "step.started"),
        (6, "thinking.delta"),
        (7, "step.started"),
        (8, "thinking.delta"),
        (9, "run.completed"),
    ]


async def test_ended_channel_resumes_and_seq_continues(bus: EventBus, store: MemoryStore) -> None:
    ref = make_ref()
    await bus.publish(ref, "run.queued", {"position": 1})
    await bus.publish(ref, "run.completed", {"status": "waiting_input"})
    bus.end(ref.run_id)

    assert not bus.is_live(ref.run_id)
    # 재개하면 영속된 마지막 seq에서 잇는다.
    [resolved] = await bus.publish(
        ref, "interaction.resolved", {"interaction_id": "i", "kind": "question"}
    )
    assert resolved.seq == 3
    assert bus.is_live(ref.run_id)

    # 재개된 채널은 살아 있으므로 구독은 예전 기록(waiting_input의 run.completed 포함)을 지나
    # 이번 run.completed까지 따라간다.
    subscription = bus.subscribe(ref, after=0)
    await bus.publish(ref, "run.completed", {"status": "succeeded"})
    replay = await collect(subscription)
    assert [event.type for event in replay] == [
        "run.queued",
        "run.completed",
        "interaction.resolved",
        "run.completed",
    ]


async def test_ended_channel_is_kept_for_the_delay_then_dropped(bus: EventBus) -> None:
    ref = make_ref()
    await bus.publish(ref, "run.queued", {"position": 1})
    live = bus.subscribe(ref, after=0)
    await bus.publish(ref, "message.delta", {"message_id": uuid.uuid4(), "text": "델타"})
    await bus.publish(ref, "run.completed", {"status": "succeeded"})
    bus.end(ref.run_id)
    await collect(live)

    # 지연 시간 안에 붙으면 델타까지 버퍼에서 받고 바로 끝난다(live가 아니다).
    replay = await collect(bus.subscribe(ref, after=0))
    assert [event.type for event in replay] == ["run.queued", "message.delta", "run.completed"]

    await asyncio.sleep(0.1)
    after_drop = await collect(bus.subscribe(ref, after=0))
    assert [event.type for event in after_drop] == ["run.queued", "run.completed"]


async def test_buffer_is_kept_even_when_nobody_was_listening(bus: EventBus) -> None:
    # 구독자 없이 끝난 런(화면이 닫혀 있던 미리보기, 끊긴 SSE)도 지연 시간 동안은 델타를 다시 준다.
    ref = make_ref()
    await bus.publish(ref, "run.queued", {"position": 1})
    await bus.publish(ref, "message.delta", {"message_id": uuid.uuid4(), "text": "아무도 안 봄"})
    await bus.publish(ref, "run.completed", {"status": "succeeded"})
    bus.end(ref.run_id)

    replay = await collect(bus.subscribe(ref, after=0))
    assert [event.type for event in replay] == ["run.queued", "message.delta", "run.completed"]

    await asyncio.sleep(0.1)
    after_drop = await collect(bus.subscribe(ref, after=0))
    assert [event.type for event in after_drop] == ["run.queued", "run.completed"]


async def test_zero_delay_drops_the_buffer_right_away(store: MemoryStore) -> None:
    bus = EventBus(store, batch_window_sec=0.01, drop_delay_sec=0)
    bus.start()
    ref = make_ref()
    try:
        await bus.publish(ref, "run.queued", {"position": 1})
        await bus.publish(ref, "message.delta", {"message_id": uuid.uuid4(), "text": "곧 사라짐"})
        await bus.publish(ref, "run.completed", {"status": "succeeded"})
        bus.end(ref.run_id)

        replay = await collect(bus.subscribe(ref, after=0))
    finally:
        await bus.close()

    assert [event.type for event in replay] == ["run.queued", "run.completed"]


async def test_close_ends_open_subscriptions_and_rejects_new_events(store: MemoryStore) -> None:
    bus = EventBus(store, batch_window_sec=0.01)
    bus.start()
    ref = make_ref()
    await bus.publish(ref, "run.queued", {"position": 1})
    subscription = bus.subscribe(ref, after=0)

    waiter = asyncio.create_task(collect(subscription))
    await asyncio.sleep(0.01)
    await bus.close()
    events = await waiter

    assert [event.type for event in events] == ["run.queued"]
    assert await bus.publish(ref, "step.started", {"step": 1}) == []
    assert [row.type for row in store.rows] == ["run.queued"]


def test_split_utf8_never_breaks_characters() -> None:
    pieces = split_utf8("가" * 2000, limit=4096)

    assert all(len(piece.encode()) <= 4096 for piece in pieces)
    assert "".join(pieces) == "가" * 2000
    assert split_utf8("short") == ["short"]


def test_jsonable_converts_ids_and_times() -> None:
    value = uuid.uuid4()
    moment = datetime(2026, 10, 13, 1, 2, 3, tzinfo=UTC)

    assert jsonable({"a": value, "b": [moment], "c": (1, 2)}) == {
        "a": str(value),
        "b": ["2026-10-13T01:02:03.000Z"],
        "c": [1, 2],
    }


async def test_sse_format_and_ping(bus: EventBus) -> None:
    ref = make_ref()
    [event] = await bus.publish(ref, "run.queued", {"position": 2})

    raw = format_event(event).decode()
    assert raw.startswith("id: 1\nevent: run.queued\ndata: {")
    assert raw.endswith("\n\n")
    assert json.loads(raw.split("data: ", 1)[1])["data"] == {"position": 2}
    ping = format_ping().decode()
    assert ping.startswith("event: ping\ndata: ") and "id:" not in ping

    subscription = bus.subscribe(ref, after=0)
    chunks: list[bytes] = []

    async def read() -> None:
        async for chunk in sse_stream(subscription, ping_interval=0.02):
            chunks.append(chunk)

    reader = asyncio.create_task(read())
    await asyncio.sleep(0.07)
    await bus.publish(ref, "run.completed", {"status": "succeeded"})
    await asyncio.wait_for(reader, 2)

    kinds = [
        chunk.decode().split("\n")[1] if chunk.startswith(b"id:") else "ping" for chunk in chunks
    ]
    assert kinds[0] == "event: run.queued"
    assert "ping" in kinds
    assert kinds[-1] == "event: run.completed"
