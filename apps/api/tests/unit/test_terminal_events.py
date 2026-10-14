"""종료 이벤트 집합과 런 종류별 봉투·저장소(2단계 설계 3.9, 12장).

workflow.completed도 run.completed처럼 구독을 닫고, 에이전트 런의 봉투와 종료 판정은 v1 그대로다.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from app.schemas.events import TERMINAL_EVENT_TYPES
from app.streaming.event_bus import BusEvent, EventBus, RunKind, RunRef
from app.streaming.stores import RunKindStore

ROOT = Path(__file__).resolve().parents[4]
SCHEMA_FILE = ROOT / "packages" / "event-schema" / "run-events.schema.json"


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[BusEvent] = []

    async def write(self, events: Sequence[BusEvent]) -> None:
        self.rows.extend(events)

    async def load(
        self, ref: RunRef, *, after: int, before: int | None = None, limit: int | None = None
    ) -> list[BusEvent]:
        rows = [
            row
            for row in self.rows
            if row.run_id == ref.run_id and row.seq > after and (before is None or row.seq < before)
        ]
        return rows[:limit] if limit is not None else rows

    async def last_seq(self, ref: RunRef) -> int:
        return max((row.seq for row in self.rows if row.run_id == ref.run_id), default=0)


@pytest.fixture
def stores() -> dict[RunKind, MemoryStore]:
    return {"run": MemoryStore(), "wf_run": MemoryStore()}


@pytest.fixture
async def bus(stores: dict[RunKind, MemoryStore]) -> AsyncIterator[EventBus]:
    instance = EventBus(RunKindStore(stores), batch_window_sec=0.01, drop_delay_sec=0.05)
    instance.start()
    yield instance
    await instance.close()


def agent_ref() -> RunRef:
    return RunRef(run_id=uuid.uuid4(), session_id=uuid.uuid4(), workspace_id=uuid.uuid4())


def wf_ref(conversation_id: uuid.UUID | None = None) -> RunRef:
    return RunRef(
        run_id=uuid.uuid4(),
        session_id=None,
        workspace_id=uuid.uuid4(),
        conversation_id=conversation_id,
        kind="wf_run",
    )


async def collect(bus: EventBus, ref: RunRef, after: int = 0) -> list[BusEvent]:
    events: list[BusEvent] = []

    async def run() -> None:
        async for event in bus.subscribe(ref, after=after).events():
            events.append(event)

    await asyncio.wait_for(run(), 2)
    return events


def test_terminal_set_matches_the_schema_file() -> None:
    schema = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))

    assert TERMINAL_EVENT_TYPES == {"run.completed", "workflow.completed"}
    assert schema["x-terminal-types"] == sorted(TERMINAL_EVENT_TYPES)
    assert "x-terminal-type" not in schema
    assert schema["x-schema-version"] == 2


async def test_workflow_completed_closes_a_live_subscription(bus: EventBus) -> None:
    ref = wf_ref()
    await bus.publish(ref, "workflow.queued", {"position": 1})
    reader = asyncio.create_task(collect(bus, ref))
    await asyncio.sleep(0.01)

    await bus.publish(ref, "node.text_delta", {"node_id": "llm_1", "text": "조각"})
    await bus.publish(ref, "workflow.completed", {"status": "succeeded"})
    # 완료 뒤에 같은 런에 이벤트가 더 와도 구독은 이미 끝났다.
    await bus.publish(ref, "workflow.queued", {"position": 1})

    events = await reader
    assert [event.type for event in events] == [
        "workflow.queued",
        "node.text_delta",
        "workflow.completed",
    ]


async def test_agent_run_still_ends_on_run_completed(bus: EventBus) -> None:
    ref = agent_ref()
    await bus.publish(ref, "run.queued", {"position": 1})
    reader = asyncio.create_task(collect(bus, ref))
    await asyncio.sleep(0.01)

    await bus.publish(ref, "run.error", {"code": "x", "message": "y"})
    await bus.publish(ref, "run.completed", {"status": "failed"})

    events = await reader
    assert [event.type for event in events] == ["run.queued", "run.error", "run.completed"]


async def test_envelopes_by_run_kind(bus: EventBus) -> None:
    agent = agent_ref()
    plain = wf_ref()
    chat = wf_ref(conversation_id=uuid.uuid4())

    [first] = await bus.publish(agent, "run.queued", {"position": 1})
    [second] = await bus.publish(plain, "workflow.queued", {"position": 1})
    [third] = await bus.publish(chat, "workflow.queued", {"position": 1})

    # 에이전트 런 봉투는 v1과 같은 키다.
    assert set(json.loads(first.line)) == {"run_id", "session_id", "seq", "ts", "type", "data"}
    workflow = json.loads(second.line)
    assert workflow["session_id"] is None and "conversation_id" not in workflow
    chatflow = json.loads(third.line)
    assert chatflow["session_id"] is None
    assert chatflow["conversation_id"] == str(chat.conversation_id)
    assert third.envelope() == chatflow


async def test_events_are_stored_by_run_kind(
    bus: EventBus, stores: dict[RunKind, MemoryStore]
) -> None:
    agent = agent_ref()
    workflow = wf_ref()
    await bus.publish(agent, "run.queued", {"position": 1})
    await bus.publish(workflow, "workflow.queued", {"position": 1})
    # 델타는 4KB로 나뉘고 저장하지 않는다.
    pieces = await bus.publish(
        workflow, "node.text_delta", {"node_id": "llm_1", "node_run_id": "n", "text": "가" * 3000}
    )
    await bus.publish(workflow, "workflow.completed", {"status": "succeeded"})
    await bus.flush()

    assert len(pieces) > 1
    assert all(len(piece.data["text"].encode()) <= 4096 for piece in pieces)
    assert [row.type for row in stores["run"].rows] == ["run.queued"]
    assert [row.type for row in stores["wf_run"].rows] == ["workflow.queued", "workflow.completed"]
    assert {row.kind for row in stores["wf_run"].rows} == {"wf_run"}
    replay = await bus.load_persisted(workflow, after=0)
    assert [event.type for event in replay] == ["workflow.queued", "workflow.completed"]
    assert all(event.session_id is None for event in replay)
