"""런 이벤트 버스(설계 2.4, 2.9).

런마다 링버퍼(2000개)를 두고 구독자(SSE·blocking 응답)에게 나눠 준다. 영속 이벤트는 단일 writer
태스크가 250ms 또는 50건마다 한 트랜잭션으로 run_event에 쓴다. 늦게 붙은 구독자는 after 이후를
링버퍼에서 받고, 버퍼가 덮였거나 이미 버려졌으면 SQLite에서 영속 이벤트만 다시 받는다.

런이 끝나면(run.completed) 구독자가 있든 없든 버퍼를 60초 동안 남겼다가 버린다. 끝난 직후
새로 고침한 화면도 델타까지 받아 그릴 수 있게 하려는 것이다(12.2 M6 이후 확정). 같은 런이
재개되면(waiting_input → queued) 다시 발행하는 순간 채널이 살아나고 seq는 영속된 마지막 값에서
잇는다.
"""

import asyncio
import json
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from pydantic import BaseModel

from app.db.engine import Database
from app.db.models import RunEvent
from app.db.repositories.run_events import RunEventRepository
from app.schemas.events import DELTA_TEXT_FIELDS, TERMINAL_EVENT_TYPE, TRANSIENT_EVENT_TYPES

log = structlog.get_logger("app.streaming")

RING_SIZE = 2000
DELTA_CHUNK_BYTES = 4096
DROP_DELAY_SEC = 60.0
BATCH_MAX = 50
BATCH_WINDOW_SEC = 0.25


def _utcnow() -> datetime:
    return datetime.now(UTC)


def format_ts(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def jsonable(value: Any) -> Any:
    """이벤트 data를 JSON에 바로 넣을 수 있는 값으로. UUID·datetime·모델이 섞여 들어와도 된다."""
    if isinstance(value, Mapping):
        items: Mapping[Any, Any] = value
        return {str(key): jsonable(item) for key, item in items.items()}
    if isinstance(value, list | tuple):
        sequence: Sequence[Any] = value
        return [jsonable(item) for item in sequence]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return format_ts(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def split_utf8(text: str, limit: int = DELTA_CHUNK_BYTES) -> list[str]:
    """UTF-8로 limit바이트를 넘지 않게 글자 단위로 자른다."""
    if len(text) * 4 <= limit or len(text.encode("utf-8")) <= limit:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if current and size + width > limit:
            pieces.append("".join(current))
            current = []
            size = 0
        current.append(char)
        size += width
    if current:
        pieces.append("".join(current))
    return pieces


@dataclass(frozen=True, slots=True)
class RunRef:
    run_id: uuid.UUID
    session_id: uuid.UUID
    workspace_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class BusEvent:
    run_id: uuid.UUID
    session_id: uuid.UUID
    workspace_id: uuid.UUID
    seq: int
    ts: datetime
    type: str
    data: dict[str, Any]
    # SSE data 줄에 그대로 쓰는 JSON. 구독자마다 다시 직렬화하지 않게 발행할 때 한 번 만든다.
    line: str = field(repr=False)

    @property
    def persisted(self) -> bool:
        return self.type not in TRANSIENT_EVENT_TYPES

    def envelope(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "session_id": str(self.session_id),
            "seq": self.seq,
            "ts": format_ts(self.ts),
            "type": self.type,
            "data": self.data,
        }


def make_event(
    ref: RunRef, seq: int, ts: datetime, event_type: str, data: dict[str, Any]
) -> BusEvent:
    envelope = {
        "run_id": str(ref.run_id),
        "session_id": str(ref.session_id),
        "seq": seq,
        "ts": format_ts(ts),
        "type": event_type,
        "data": data,
    }
    line = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return BusEvent(ref.run_id, ref.session_id, ref.workspace_id, seq, ts, event_type, data, line)


class EventStore(Protocol):
    async def write(self, events: Sequence[BusEvent]) -> None: ...

    async def load(
        self, ref: RunRef, *, after: int, before: int | None = None, limit: int | None = None
    ) -> list[BusEvent]: ...

    async def last_seq(self, ref: RunRef) -> int: ...


class SqliteEventStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def write(self, events: Sequence[BusEvent]) -> None:
        by_workspace: dict[uuid.UUID, list[RunEvent]] = {}
        for event in events:
            by_workspace.setdefault(event.workspace_id, []).append(
                RunEvent(
                    run_id=event.run_id,
                    seq=event.seq,
                    type=event.type,
                    payload=event.data,
                    created_at=event.ts,
                )
            )
        async with self._db.write() as session:
            for workspace_id, rows in by_workspace.items():
                RunEventRepository(session, workspace_id).add_many(rows)

    async def load(
        self, ref: RunRef, *, after: int, before: int | None = None, limit: int | None = None
    ) -> list[BusEvent]:
        async with self._db.read() as session:
            rows = await RunEventRepository(session, ref.workspace_id).list_after(
                ref.run_id, after=after, before=before, limit=limit
            )
        return [make_event(ref, row.seq, row.created_at, row.type, row.payload) for row in rows]

    async def last_seq(self, ref: RunRef) -> int:
        async with self._db.read() as session:
            return await RunEventRepository(session, ref.workspace_id).max_seq(ref.run_id)


@dataclass(eq=False)
class _Channel:
    ref: RunRef
    buffer: deque[BusEvent]
    seq: int | None = None
    ended: bool = False
    subscribers: "set[Subscription]" = field(default_factory=lambda: set[Subscription]())
    drop_handle: asyncio.TimerHandle | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Subscription:
    """한 런의 이벤트를 after 다음부터 준다. 진행 중인 런이면 run.completed까지 따라간다."""

    def __init__(
        self,
        bus: "EventBus",
        ref: RunRef,
        *,
        after: int,
        snapshot: list[BusEvent],
        db_range: tuple[int, int | None] | None,
        live: bool,
    ) -> None:
        self.ref = ref
        self.live = live
        self._bus = bus
        self._after = after
        self._snapshot = snapshot
        self._db_range = db_range
        self._queue: asyncio.Queue[BusEvent | None] = asyncio.Queue()
        self._closed = False

    def push(self, event: BusEvent | None) -> None:
        if not self._closed:
            self._queue.put_nowait(event)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._unsubscribe(self)  # pyright: ignore[reportPrivateUsage]

    async def events(self) -> AsyncGenerator[BusEvent]:
        last = self._after
        try:
            if self._db_range is not None:
                after, before = self._db_range
                for event in await self._bus.load_persisted(self.ref, after=after, before=before):
                    if event.seq > last:
                        last = event.seq
                        yield event
            for event in self._snapshot:
                if event.seq > last:
                    last = event.seq
                    yield event
            if not self.live:
                return
            while True:
                event = await self._queue.get()
                if event is None:
                    return
                if event.seq <= last:
                    continue
                last = event.seq
                yield event
                if event.type == TERMINAL_EVENT_TYPE:
                    return
        finally:
            self.close()


class EventBus:
    def __init__(
        self,
        store: EventStore,
        *,
        ring_size: int = RING_SIZE,
        drop_delay_sec: float = DROP_DELAY_SEC,
        batch_max: int = BATCH_MAX,
        batch_window_sec: float = BATCH_WINDOW_SEC,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._store = store
        self._ring_size = ring_size
        self._drop_delay = drop_delay_sec
        self._batch_max = batch_max
        self._batch_window = batch_window_sec
        self._clock = clock
        self._channels: dict[uuid.UUID, _Channel] = {}
        self._pending: asyncio.Queue[BusEvent] = asyncio.Queue()
        self._wake = asyncio.Event()
        self._writer: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._write_loop(), name="event-bus-writer")

    @property
    def closed(self) -> bool:
        return self._closed

    def is_live(self, run_id: uuid.UUID) -> bool:
        channel = self._channels.get(run_id)
        return channel is not None and not channel.ended

    async def publish(
        self, ref: RunRef, event_type: str, data: Mapping[str, Any]
    ) -> list[BusEvent]:
        if self._closed:
            log.warning("event_bus.publish_after_close", run_id=str(ref.run_id), type=event_type)
            return []
        payload: dict[str, Any] = jsonable(data)
        channel = self._channels.get(ref.run_id)
        if channel is None:
            channel = _Channel(ref=ref, buffer=deque(maxlen=self._ring_size))
            self._channels[ref.run_id] = channel
        async with channel.lock:
            if channel.seq is None:
                # 이 프로세스에서 처음 발행하거나 채널을 버린 뒤다. 영속된 마지막 seq에서 잇는다.
                await self.flush()
                channel.seq = await self._store.last_seq(ref)
            published: list[BusEvent] = []
            for piece in self._pieces(event_type, payload):
                channel.seq += 1
                event = make_event(ref, channel.seq, self._clock(), event_type, piece)
                channel.buffer.append(event)
                published.append(event)
                if event.persisted:
                    self._pending.put_nowait(event)
                    if self._pending.qsize() >= self._batch_max:
                        self._wake.set()
                for subscriber in list(channel.subscribers):
                    subscriber.push(event)
            if channel.ended:
                channel.ended = False
                if channel.drop_handle is not None:
                    channel.drop_handle.cancel()
                    channel.drop_handle = None
        return published

    def _pieces(self, event_type: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        field_name = DELTA_TEXT_FIELDS.get(event_type)
        text = data.get(field_name) if field_name is not None else None
        if field_name is None or not isinstance(text, str):
            return [data]
        chunks = split_utf8(text)
        if len(chunks) == 1:
            return [data]
        return [{**data, field_name: chunk} for chunk in chunks]

    def subscribe(self, ref: RunRef, *, after: int = 0) -> Subscription:
        """채널이 살아 있으면 run.completed까지 따라가고, 아니면 남은 기록만 다시 준다.

        등록은 동기로 끝나므로 이 호출 뒤에 발행된 이벤트는 빠지지 않는다.
        """
        channel = self._channels.get(ref.run_id)
        live = channel is not None and not channel.ended and not self._closed
        snapshot: list[BusEvent] = []
        buffer_start: int | None = None
        if channel is not None and channel.buffer:
            buffer_start = channel.buffer[0].seq
            snapshot = [event for event in channel.buffer if event.seq > after]
        needs_db = buffer_start is None or buffer_start > after + 1
        subscription = Subscription(
            self,
            ref,
            after=after,
            snapshot=snapshot,
            db_range=(after, buffer_start) if needs_db else None,
            live=live,
        )
        if live and channel is not None:
            channel.subscribers.add(subscription)
        return subscription

    def _unsubscribe(self, subscription: Subscription) -> None:
        channel = self._channels.get(subscription.ref.run_id)
        if channel is not None:
            channel.subscribers.discard(subscription)

    def end(self, run_id: uuid.UUID) -> None:
        """런이 끝났거나(run.completed) waiting_input으로 멈췄다."""
        channel = self._channels.get(run_id)
        if channel is None:
            return
        channel.ended = True
        if channel.drop_handle is not None:
            channel.drop_handle.cancel()
            channel.drop_handle = None
        if self._drop_delay <= 0:
            self._drop(run_id)
            return
        channel.drop_handle = asyncio.get_running_loop().call_later(
            self._drop_delay, self._drop, run_id
        )

    def _drop(self, run_id: uuid.UUID) -> None:
        channel = self._channels.get(run_id)
        if channel is None or not channel.ended:
            return
        del self._channels[run_id]
        for subscriber in list(channel.subscribers):
            subscriber.push(None)

    async def load_persisted(
        self, ref: RunRef, *, after: int, before: int | None = None, limit: int | None = None
    ) -> list[BusEvent]:
        await self.flush()
        return await self._store.load(ref, after=after, before=before, limit=limit)

    async def flush(self) -> None:
        if self._writer is None or self._writer.done():
            # writer가 없으면(시작 전·종료 뒤) 지금 바로 쓴다.
            batch: list[BusEvent] = []
            while not self._pending.empty():
                batch.append(self._pending.get_nowait())
            if batch:
                try:
                    await self._store.write(batch)
                finally:
                    for _ in batch:
                        self._pending.task_done()
            return
        self._wake.set()
        await self._pending.join()

    async def close(self) -> None:
        """종료 훅. 남은 이벤트를 쓰고 구독을 모두 끝낸다(진행 중인 SSE 응답이 닫힌다)."""
        if self._closed:
            return
        self._closed = True
        try:
            await self.flush()
        finally:
            for channel in self._channels.values():
                if channel.drop_handle is not None:
                    channel.drop_handle.cancel()
                for subscriber in list(channel.subscribers):
                    subscriber.push(None)
            self._channels.clear()
            if self._writer is not None:
                self._writer.cancel()
                await asyncio.gather(self._writer, return_exceptions=True)
                self._writer = None

    async def _write_loop(self) -> None:
        while True:
            first = await self._pending.get()
            batch = [first]
            if not self._wake.is_set():
                try:
                    await asyncio.wait_for(self._wake.wait(), self._batch_window)
                except TimeoutError:
                    pass
            self._wake.clear()
            while len(batch) < self._batch_max:
                try:
                    batch.append(self._pending.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._store.write(batch)
            except Exception:
                # 기록 실패로 런을 멈추지 않는다. 링버퍼로 이미 전달된 이벤트는 화면에 남는다.
                log.exception("event_bus.write_failed", count=len(batch))
            finally:
                for _ in batch:
                    self._pending.task_done()
