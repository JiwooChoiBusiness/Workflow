"""이벤트 스키마 v2의 워크플로우 부분(2단계 설계 3.9).

엔진이 실제로 내는 이벤트를 워크플로우 런 봉투(session_id null)에 담아 생성된 JSON Schema로
검사한다. 노드가 낼 수 있는 이벤트와 저장·분할 규칙도 스키마 상수와 맞는지 본다.
"""

import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict

from app.runner.supervisor import CancelSignal
from app.schemas.events import (
    DELTA_TEXT_FIELDS,
    EVENT_DATA_MODELS,
    TERMINAL_EVENT_TYPES,
    TRANSIENT_EVENT_TYPES,
    WF_EVENT_DATA_MODELS,
)
from app.schemas.workflow import Graph
from app.streaming.event_bus import RunRef, jsonable, make_event
from app.workflow.engine import Engine, EngineRun, RunContext, WfEvent
from app.workflow.frames import Checkpoint
from app.workflow.nodes import default_registry
from app.workflow.nodes.base import (
    NODE_EVENT_TYPES,
    NodeContext,
    NodeResult,
    NodeRunner,
    OutputSpec,
)
from app.workflow.ports import NodeSettings

ROOT = Path(__file__).resolve().parents[4]
SCHEMA_FILE = ROOT / "packages" / "event-schema" / "run-events.schema.json"


class Ports:
    model = None
    sandbox = None
    http = None
    files = None
    tasks = None
    node_settings = NodeSettings()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.perf_counter()

    async def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        pass


class EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TalkNode(NodeRunner[EmptyConfig]):
    """모델 노드 흉내. 글 조각과 사용량을 낸다."""

    type = "test_talk"
    Config = EmptyConfig
    fixed_outputs = (OutputSpec("text", "string"),)

    async def run(
        self, ctx: NodeContext, config: EmptyConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        await ctx.emit("node.text_delta", {"text": "안녕"})
        await ctx.emit(
            "node.usage",
            {"model": "fake-model", "input_tokens": 3, "output_tokens": 2, "estimated": False},
        )
        return NodeResult(status="succeeded", outputs={"text": "안녕"})


def validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_FILE.read_text(encoding="utf-8")))


async def test_engine_events_fit_the_v2_schema_in_a_workflow_envelope() -> None:
    registry = default_registry().copy()
    registry.register(TalkNode())
    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "start", "type": "start", "config": {}},
                {"id": "talk_1", "type": "test_talk", "config": {}},
                {
                    "id": "if_1",
                    "type": "if_else",
                    "config": {
                        "cases": [
                            {
                                "id": "yes",
                                "conditions": [
                                    {"var": "talk_1.text", "op": "contains", "value": "안녕"}
                                ],
                            }
                        ]
                    },
                },
                {"id": "end_1", "type": "end", "config": {"outputs": []}},
                {"id": "end_2", "type": "end", "config": {"outputs": []}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "talk_1"},
                {"id": "e2", "source": "talk_1", "target": "if_1"},
                {"id": "e3", "source": "if_1", "source_handle": "case_yes", "target": "end_1"},
                {"id": "e4", "source": "if_1", "source_handle": "else", "target": "end_2"},
            ],
        }
    )
    run_id = uuid.uuid4()
    context = RunContext(
        run_id=run_id, graph=graph, kind="workflow", ports=Ports(), cancel=CancelSignal()
    )
    ref = RunRef(run_id=run_id, session_id=None, workspace_id=uuid.uuid4(), kind="wf_run")
    check = validator()
    seen: list[str] = []
    run = Engine(registry).run(context)
    async with run:
        async for seq, event in enumerate_async(run):
            envelope = json.loads(
                make_event(ref, seq, datetime.now(UTC), event.type, jsonable(event.data)).line
            )
            errors = [error.message for error in check.iter_errors(envelope)]
            assert errors == [], (event.type, errors)
            seen.append(event.type)

    assert run.outcome.status == "succeeded"
    assert {"node.text_delta", "node.usage", "node.completed", "workflow.completed"} <= set(seen)
    queued = json.loads(
        make_event(ref, 1, datetime.now(UTC), "workflow.queued", {"position": 1}).line
    )
    assert list(check.iter_errors(queued)) == []


async def enumerate_async(run: EngineRun) -> AsyncIterator[tuple[int, WfEvent]]:
    seq = 0
    async for event in run:
        seq += 1
        yield seq, event


def test_workflow_types_join_the_shared_constants() -> None:
    assert set(WF_EVENT_DATA_MODELS) <= set(EVENT_DATA_MODELS)
    assert NODE_EVENT_TYPES <= set(WF_EVENT_DATA_MODELS)
    # 1단계 usage와 겹치지 않게 노드 사용량은 node.usage다.
    assert "usage" not in WF_EVENT_DATA_MODELS and "node.usage" in NODE_EVENT_TYPES
    assert {"node.text_delta", "node.progress", "node.waiting", "answer.delta"} <= (
        TRANSIENT_EVENT_TYPES
    )
    assert DELTA_TEXT_FIELDS["node.text_delta"] == "text"
    assert DELTA_TEXT_FIELDS["answer.delta"] == "text"
    assert "workflow.completed" in TERMINAL_EVENT_TYPES


def test_agent_envelope_with_a_session_still_validates() -> None:
    event = {
        "run_id": "01a1337d-c3f8-7448-bba7-275aec382116",
        "session_id": "01a1337d-c3ec-758a-9617-dbbf6afef6a2",
        "seq": 3,
        "ts": "2026-10-14T05:01:00.123Z",
        "type": "run.queued",
        "data": {"position": 1},
    }
    check = validator()

    assert list(check.iter_errors(event)) == []
    assert list(check.iter_errors({**event, "session_id": None})) == []
    assert list(check.iter_errors({**event, "session_id": 7}))
