"""packages/event-schema/run-events.schema.json이 Pydantic 모델과 같은지.

1단계 설계 4.5·13.2와 2단계 설계 3.9(스키마 v2)의 이벤트 표를 본다.
"""

import json
import runpy
from pathlib import Path

from jsonschema import Draft202012Validator

from app.schemas.events import (
    DELTA_TEXT_FIELDS,
    EVENT_DATA_MODELS,
    TRANSIENT_EVENT_TYPES,
    run_events_json_schema,
)

ROOT = Path(__file__).resolve().parents[4]
SCHEMA_FILE = ROOT / "packages" / "event-schema" / "run-events.schema.json"
DESIGN_TYPES = {
    "run.queued",
    "run.started",
    "workspace.restoring",
    "workspace.restored",
    "workspace.restore_warning",
    "workspace.recreated",
    "step.started",
    "message.delta",
    "thinking.delta",
    "tool_call.started",
    "tool_call.input_delta",
    "tool_call.ready",
    "tool_call.output_delta",
    "tool_call.completed",
    "file.changed",
    "run.waiting_input",
    "interaction.resolved",
    "run.resumed",
    "run.retrying",
    "context.compacted",
    "context.prompt_changed",
    "usage",
    "message.completed",
    "run.completed",
    "run.error",
    "ping",
}
# 2단계 설계 3.9의 표. 사용량은 1단계 usage와 겹치지 않게 node.usage다.
WF_DESIGN_TYPES = {
    "workflow.queued",
    "workflow.started",
    "node.started",
    "node.text_delta",
    "node.progress",
    "node.waiting",
    "node.retrying",
    "node.usage",
    "node.completed",
    "iteration.started",
    "iteration.next",
    "iteration.completed",
    "answer.delta",
    "answer.completed",
    "workflow.waiting_input",
    "workflow.resumed",
    "workflow.error",
    "workflow.completed",
}
# 오케스트레이션 설계 6.1의 표(같은 v2 안에서 더한 타입).
ORCH_DESIGN_TYPES = {"child_run.started", "child_run.completed"}


def test_generated_file_is_up_to_date() -> None:
    script = runpy.run_path(str(ROOT / "scripts" / "gen-event-schema.py"))

    assert SCHEMA_FILE.read_text(encoding="utf-8") == script["render"]()


def test_schema_covers_the_design_table() -> None:
    schema = run_events_json_schema()
    Draft202012Validator.check_schema(schema)

    every = DESIGN_TYPES | WF_DESIGN_TYPES | ORCH_DESIGN_TYPES
    assert set(EVENT_DATA_MODELS) == every
    assert {variant["title"] for variant in schema["oneOf"]} == every
    assert TRANSIENT_EVENT_TYPES <= DESIGN_TYPES | WF_DESIGN_TYPES
    assert set(DELTA_TEXT_FIELDS) <= TRANSIENT_EVENT_TYPES


def test_schema_validates_a_real_envelope() -> None:
    validator = Draft202012Validator(json.loads(SCHEMA_FILE.read_text(encoding="utf-8")))
    event = {
        "run_id": "01a1337d-c3f8-7448-bba7-275aec382116",
        "session_id": "01a1337d-c3ec-758a-9617-dbbf6afef6a2",
        "seq": 7,
        "ts": "2026-10-13T05:01:00.123Z",
        "type": "tool_call.completed",
        "data": {
            "tool_call_id": "call_1",
            "status": "ok",
            "exit_code": 0,
            "timed_out": False,
            "duration_ms": 12,
            "output_excerpt": "ok",
            "output_bytes": 2,
        },
    }

    assert list(validator.iter_errors(event)) == []
    broken = {**event, "data": {**event["data"], "status": "unknown"}}
    assert list(validator.iter_errors(broken))
