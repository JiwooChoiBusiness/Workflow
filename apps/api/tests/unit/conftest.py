"""챗플로우 엔진 시험(M17)이 같이 쓰는 포트와 실행 도우미(chat_kit).

--import-mode=importlib에서는 테스트 모듈이 conftest를 import할 수 없어 픽스처로 넘긴다. 워크플로우
엔진 전반의 시험 도구는 tests/unit/workflow/conftest.py의 kit이다.
"""

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from app.llm.base import ModelAdapter
from app.runner.supervisor import CancelSignal
from app.schemas.workflow import WF_EVENT_DATA_MODELS, Graph, VarType, WorkflowKind
from app.streaming.event_bus import jsonable
from app.workflow.engine import Engine, RunContext, RunOutcome, WfEvent
from app.workflow.frames import Checkpoint
from app.workflow.memory import Turn
from app.workflow.nodes import default_registry
from app.workflow.nodes.base import NodeRegistry
from app.workflow.ports import NodeSettings, TaskPort


class ChatPorts:
    """EnginePorts. 모델과 과업 포트만 시험이 넣는다."""

    def __init__(self, model: ModelAdapter | None = None, tasks: TaskPort | None = None) -> None:
        self.model = model
        self.sandbox = None
        self.http = None
        self.files = None
        self.tasks = tasks
        self.node_settings = NodeSettings()
        self.checkpoints: list[Checkpoint] = []

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.perf_counter()

    async def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self.checkpoints.append(checkpoint)


@dataclass
class ChatRun:
    events: list[WfEvent]
    outcome: RunOutcome
    ports: ChatPorts
    context: RunContext

    def types(self) -> list[str]:
        return [event.type for event in self.events]

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [event.data for event in self.events if event.type == event_type]

    def answer(self) -> str:
        return "".join(data["text"] for data in self.of("answer.delta"))

    def index(self, event_type: str, node_id: str, *, last: bool = False) -> int:
        """node_id의 event_type 이벤트가 몇 번째인지. last면 마지막 것."""
        found = [
            position
            for position, event in enumerate(self.events)
            if event.type == event_type and event.data.get("node_id") == node_id
        ]
        assert found, f"{node_id}의 {event_type}이 없다"
        return found[-1] if last else found[0]


OnEvent = Callable[[WfEvent], Awaitable[None] | None]


def _check(event: WfEvent) -> None:
    model = WF_EVENT_DATA_MODELS[event.type]
    payload = jsonable(event.data)
    model.model_validate(payload)
    unknown = set(payload) - set(model.model_fields)
    assert not unknown, f"{event.type}에 스키마에 없는 필드: {unknown}"


@dataclass
class ChatKit:
    registry: NodeRegistry = field(default_factory=lambda: default_registry().copy())

    def node(
        self, node_id: str, node_type: str, config: dict[str, Any] | None = None, **extra: Any
    ) -> dict[str, Any]:
        return {"id": node_id, "type": node_type, "title": node_id, "config": config or {}, **extra}

    def graph(self, nodes: Sequence[dict[str, Any]], edges: Sequence[str]) -> Graph:
        """edges는 "source>target" 또는 "source:handle>target"."""
        parsed: list[dict[str, Any]] = []
        for index, spec in enumerate(edges, start=1):
            source, target = spec.split(">")
            source, _, handle = source.partition(":")
            parsed.append(
                {
                    "id": f"e{index}",
                    "source": source,
                    "target": target,
                    "source_handle": handle or "out",
                }
            )
        return Graph.model_validate({"nodes": list(nodes), "edges": parsed})

    async def run(
        self,
        graph: Graph,
        *,
        model: ModelAdapter | None = None,
        tasks: TaskPort | None = None,
        kind: WorkflowKind = "chatflow",
        inputs: Mapping[str, Any] | None = None,
        system: Mapping[str, Any] | None = None,
        conversation: Mapping[str, Any] | None = None,
        conversation_types: Mapping[str, VarType] | None = None,
        history: Sequence[Turn] = (),
        resume: Checkpoint | None = None,
        ports: ChatPorts | None = None,
        run_id: uuid.UUID | None = None,
        on_event: OnEvent | None = None,
    ) -> ChatRun:
        ports = ports or ChatPorts(model, tasks)
        context = RunContext(
            run_id=run_id or uuid.uuid4(),
            graph=graph,
            kind=kind,
            ports=ports,
            cancel=CancelSignal(),
            inputs=dict(inputs or {}),
            workflow_id=uuid.uuid4(),
            version_id=uuid.uuid4(),
            system=dict(system or {}),
            conversation=dict(conversation or {}),
            conversation_types=conversation_types,
            history=history,
            resume=resume,
        )
        events: list[WfEvent] = []
        run = Engine(self.registry).run(context)
        async with run:
            async for event in run:
                _check(event)
                events.append(event)
                if on_event is not None:
                    result = on_event(event)
                    if result is not None:
                        await result
        return ChatRun(events, run.outcome, ports, context)


@pytest.fixture
def chat_kit() -> ChatKit:
    return ChatKit()
