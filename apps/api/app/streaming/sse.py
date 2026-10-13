"""SSE 전송(설계 6.5).

각 이벤트는 `id: <seq>`, `event: <type>`, `data: <JSON 한 줄>`이다. 15초 동안 보낼 것이 없으면
ping을 보낸다. 런이 끝나면(run.completed, waiting_input 포함) 응답을 닫는다.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime

from starlette.responses import StreamingResponse

from app.streaming.event_bus import BusEvent, Subscription, format_ts

PING_INTERVAL_SEC = 15.0
SSE_MEDIA_TYPE = "text/event-stream"
# X-Accel-Buffering은 프록시가 없어도 vite dev 프록시 때문에 둔다(6.5).
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def format_event(event: BusEvent) -> bytes:
    return f"id: {event.seq}\nevent: {event.type}\ndata: {event.line}\n\n".encode()


def format_ping(now: datetime | None = None) -> bytes:
    data = {"type": "ping", "ts": format_ts(now or datetime.now(UTC)), "data": {}}
    line = json.dumps(data, separators=(",", ":"))
    return f"event: ping\ndata: {line}\n\n".encode()


async def sse_stream(
    subscription: Subscription, *, ping_interval: float = PING_INTERVAL_SEC
) -> AsyncIterator[bytes]:
    events = subscription.events()
    pending: asyncio.Future[BusEvent] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(events))
            done, _ = await asyncio.wait({pending}, timeout=ping_interval)
            if not done:
                yield format_ping()
                continue
            future, pending = pending, None
            try:
                event = future.result()
            except StopAsyncIteration:
                return
            yield format_event(event)
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.wait({pending})
        await events.aclose()
        subscription.close()


def sse_response(
    subscription: Subscription,
    *,
    headers: Mapping[str, str] | None = None,
    ping_interval: float = PING_INTERVAL_SEC,
) -> StreamingResponse:
    return StreamingResponse(
        sse_stream(subscription, ping_interval=ping_interval),
        media_type=SSE_MEDIA_TYPE,
        headers={**SSE_HEADERS, **(headers or {})},
    )
