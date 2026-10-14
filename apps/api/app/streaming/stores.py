"""런 종류별 이벤트 저장소(2단계 설계 3.9, 12장).

버스·링버퍼·SSE는 1단계 것을 그대로 쓰고 영속 테이블만 런 종류에 따라 다르다. 에이전트 런은
run_event(event_bus.SqliteEventStore), 워크플로우 런은 wf_run_event다. 버스는 RunKindStore 하나를
받아 이벤트·ref의 kind로 저장소를 고른다.
"""

import uuid
from collections.abc import Mapping, Sequence

from app.db.engine import Database
from app.db.models import WfRunEvent
from app.db.repositories.workflows import WfRunEventRepository
from app.streaming.event_bus import (
    BusEvent,
    EventStore,
    RunKind,
    RunRef,
    SqliteEventStore,
    make_event,
)


class WfRunEventStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def write(self, events: Sequence[BusEvent]) -> None:
        by_workspace: dict[uuid.UUID, list[WfRunEvent]] = {}
        for event in events:
            by_workspace.setdefault(event.workspace_id, []).append(
                WfRunEvent(
                    wf_run_id=event.run_id,
                    seq=event.seq,
                    type=event.type,
                    payload=event.data,
                    created_at=event.ts,
                )
            )
        async with self._db.write() as session:
            for workspace_id, rows in by_workspace.items():
                WfRunEventRepository(session, workspace_id).add_many(rows)

    async def load(
        self, ref: RunRef, *, after: int, before: int | None = None, limit: int | None = None
    ) -> list[BusEvent]:
        async with self._db.read() as session:
            rows = await WfRunEventRepository(session, ref.workspace_id).list_after(
                ref.run_id, after=after, before=before, limit=limit
            )
        return [make_event(ref, row.seq, row.created_at, row.type, row.payload) for row in rows]

    async def last_seq(self, ref: RunRef) -> int:
        async with self._db.read() as session:
            return await WfRunEventRepository(session, ref.workspace_id).max_seq(ref.run_id)


class RunKindStore:
    def __init__(self, stores: Mapping[RunKind, EventStore]) -> None:
        self._stores = dict(stores)

    async def write(self, events: Sequence[BusEvent]) -> None:
        by_kind: dict[RunKind, list[BusEvent]] = {}
        for event in events:
            by_kind.setdefault(event.kind, []).append(event)
        for kind, batch in by_kind.items():
            await self._stores[kind].write(batch)

    async def load(
        self, ref: RunRef, *, after: int, before: int | None = None, limit: int | None = None
    ) -> list[BusEvent]:
        return await self._stores[ref.kind].load(ref, after=after, before=before, limit=limit)

    async def last_seq(self, ref: RunRef) -> int:
        return await self._stores[ref.kind].last_seq(ref)


def sqlite_stores(db: Database) -> RunKindStore:
    return RunKindStore({"run": SqliteEventStore(db), "wf_run": WfRunEventStore(db)})
