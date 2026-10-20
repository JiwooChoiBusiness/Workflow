"""human_input 노드의 폼 대기와 재개(2단계 설계 2.4, 3.7, 3.9).

엔진 수준에서 폼으로 멈추고(pending kind=form, workflow.waiting_input의 form), 같은 체크포인트에
응답을 RunContext.reply로 넘겨 재개하는 흐름을 본다. 응답 값은 start 필드와 같은 규칙으로 맞춰
필드마다 출력하고, 시간 초과는 input_timeout, 다른 폼에 보낸 응답은 같은 폼으로 다시 묻는다. 반복 안
대기와 병렬 대기(하나씩 차례로), 검증, M19 전에 저장된 대기 행도 본다. 서비스·API 경로는
tests/integration/test_human_input.py가 본다.
"""

import dataclasses
from typing import Any

import pytest

from app.schemas.wf_run import WfPendingOut
from app.schemas.workflow import WF_EVENT_DATA_MODELS
from app.streaming.event_bus import jsonable
from app.workflow.engine import NodeReply
from app.workflow.graph import validate

Node = Any

FIELDS = [
    {"name": "ok", "type": "boolean", "label": "승인", "required": True},
    {"name": "count", "type": "number", "label": "수량"},
    {"name": "level", "type": "select", "options": ["상", "하"], "default": "하"},
    {"name": "memo", "type": "paragraph", "max_length": 20},
]


def form_graph(kit: Any, make_node: Node, fields: list[dict[str, Any]] | None = None) -> Any:
    return kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "topic", "type": "string"}]}),
            make_node(
                "human_input_1",
                "human_input",
                {"description": "**{{ start.topic }}** 검토", "fields": fields or FIELDS},
            ),
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": field["name"], "var": f"human_input_1.{field['name']}"}
                        for field in (fields or FIELDS)
                    ]
                },
            ),
        ],
        ["start>human_input_1", "human_input_1>end_1"],
    )


async def resume(
    kit: Any, graph: Any, checkpoint: Any, reply: NodeReply | None, **options: Any
) -> Any:
    """kit.run과 같되 RunContext.reply를 넣는다. 이벤트는 스키마 모델로 검사한다."""
    context = dataclasses.replace(kit.context(graph, resume=checkpoint, **options), reply=reply)
    events = []
    run = kit.engine.run(context)
    async with run:
        async for event in run:
            model = WF_EVENT_DATA_MODELS[event.type]
            payload = jsonable(event.data)
            model.model_validate(payload)
            assert not set(payload) - set(model.model_fields)
            events.append(event)
    return run.outcome, events


async def test_the_form_waits_and_the_reply_becomes_outputs(kit: Any, make_node: Node) -> None:
    graph = form_graph(kit, make_node)
    assert validate(graph, "workflow") == []

    first = await kit.run(graph, inputs={"topic": "분기 보고서"})

    assert first.outcome.status == "waiting_input"
    pending = first.outcome.pending
    assert pending is not None
    assert (pending.node_id, pending.kind, pending.interaction, pending.child_run_id) == (
        "human_input_1",
        "form",
        None,
        None,
    )
    form = pending.form
    assert form is not None and form.form_id.startswith("form_")
    assert form.description == "**분기 보고서** 검토"
    assert [(f.name, f.type, f.required) for f in form.fields] == [
        ("ok", "boolean", True),
        ("count", "number", False),
        ("level", "select", False),
        ("memo", "paragraph", False),
    ]
    [waiting] = first.of("workflow.waiting_input")
    assert waiting["kind"] == "form" and waiting["interaction"] is None
    assert waiting["form"]["form_id"] == form.form_id
    assert first.types()[-2:] == ["workflow.waiting_input", "workflow.completed"]
    checkpoint = first.outcome.checkpoint
    assert checkpoint is not None
    assert checkpoint.node_state["human_input_1"]["form_id"] == form.form_id
    # 대기 기록은 서비스가 WfPendingOut으로 읽는다.
    assert WfPendingOut.model_validate(pending.model_dump(mode="json")).form == form

    reply = NodeReply(
        "human_input_1", None, {"form_id": form.form_id, "values": {"ok": True, "count": "3"}}
    )
    outcome, events = await resume(kit, graph, checkpoint, reply, inputs={"topic": "분기 보고서"})

    assert outcome.status == "succeeded", outcome.error
    assert outcome.outputs == {"ok": True, "count": 3, "level": "하", "memo": None}
    resumed = [event.data for event in events if event.type == "workflow.resumed"]
    assert resumed == [{"node_id": "human_input_1"}]
    started = next(event for event in events if event.type == "node.started")
    # 기다리던 노드 런을 이어 간다.
    first_run = next(
        event.node_run
        for event in first.events
        if event.type == "node.completed" and event.data["node_id"] == "human_input_1"
    )
    assert started.data["node_run_id"] == first_run.node_run_id
    record = next(
        event.node_run
        for event in events
        if event.type == "node.completed" and event.data["node_id"] == "human_input_1"
    )
    assert record.inputs["values"]["count"] == 3


async def test_timeout_fails_with_input_timeout(kit: Any, make_node: Node) -> None:
    graph = form_graph(kit, make_node)
    first = await kit.run(graph)
    form = first.outcome.pending.form

    reply = NodeReply("human_input_1", None, {"form_id": form.form_id, "timeout": True})
    outcome, _ = await resume(kit, graph, first.outcome.checkpoint, reply)

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert (outcome.error.code, outcome.error.node_id) == ("input_timeout", "human_input_1")


async def test_bad_values_fail_and_a_stale_reply_asks_again(kit: Any, make_node: Node) -> None:
    graph = form_graph(kit, make_node)
    first = await kit.run(graph)
    form = first.outcome.pending.form

    bad = NodeReply("human_input_1", None, {"form_id": form.form_id, "values": {"count": 1}})
    outcome, _ = await resume(kit, graph, first.outcome.checkpoint, bad)
    assert outcome.error is not None and outcome.error.code == "invalid_inputs"

    stale = NodeReply("human_input_1", None, {"form_id": "form_old", "values": {"ok": True}})
    outcome, _ = await resume(kit, graph, first.outcome.checkpoint, stale)
    assert outcome.status == "waiting_input"
    assert outcome.pending is not None and outcome.pending.form is not None
    assert outcome.pending.form.form_id == form.form_id
    assert outcome.pending.form.created_at == form.created_at


async def test_a_form_inside_an_iteration_waits_per_item(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("list_1", "test_flaky", {"value": ["가", "나"]}),
            make_node(
                "iteration_1",
                "iteration",
                {"input_var": "list_1.value", "output_var": "human_input_1.note"},
            ),
            make_node("iteration_start_1", "iteration_start", parent_id="iteration_1"),
            make_node(
                "human_input_1",
                "human_input",
                {
                    "description": "{{ iteration_start_1.item }} 확인",
                    "fields": [{"name": "note", "type": "string", "required": True}],
                },
                parent_id="iteration_1",
            ),
            make_node(
                "end_1", "end", {"outputs": [{"name": "notes", "var": "iteration_1.output"}]}
            ),
        ],
        [
            "start>list_1",
            "list_1>iteration_1",
            "iteration_1>end_1",
            "iteration_start_1>human_input_1",
        ],
    )
    assert validate(graph, "workflow", registry=kit.registry) == []

    first = await kit.run(graph)
    pending = first.outcome.pending
    assert pending is not None and pending.iteration_index == 0
    assert pending.form is not None and pending.form.description == "가 확인"

    reply = NodeReply(
        "human_input_1", 0, {"form_id": pending.form.form_id, "values": {"note": "A"}}
    )
    second, _ = await resume(kit, graph, first.outcome.checkpoint, reply)
    assert second.status == "waiting_input"
    pending = second.pending
    assert pending is not None and pending.iteration_index == 1
    assert pending.form is not None and pending.form.description == "나 확인"

    reply = NodeReply(
        "human_input_1", 1, {"form_id": pending.form.form_id, "values": {"note": "B"}}
    )
    third, _ = await resume(kit, graph, second.checkpoint, reply)
    assert third.status == "succeeded", third.error
    assert third.outputs == {"notes": ["A", "B"]}


async def test_parallel_waits_are_answered_one_at_a_time(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node(
                "human_input_1",
                "human_input",
                {"fields": [{"name": "a", "type": "string"}]},
            ),
            make_node("test_wait_1", "test_wait", {"value": "티켓"}),
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "a", "var": "human_input_1.a"},
                        {"name": "w", "var": "test_wait_1.value"},
                    ]
                },
            ),
        ],
        ["start>human_input_1", "start>test_wait_1", "human_input_1>end_1", "test_wait_1>end_1"],
    )

    first = await kit.run(graph)

    # 노드 id 순으로 하나만 기록한다(3.7).
    pending = first.outcome.pending
    assert pending is not None and (pending.node_id, pending.kind) == ("human_input_1", "form")
    assert pending.form is not None
    reply = NodeReply(
        "human_input_1", None, {"form_id": pending.form.form_id, "values": {"a": "x"}}
    )

    second, _ = await resume(kit, graph, first.outcome.checkpoint, reply)

    # 승인을 기다리던 쪽은 저장한 state로 다시 돌아 끝나고 런이 성공한다.
    assert second.status == "succeeded", second.error
    assert second.outputs == {"a": "x", "w": "티켓"}


def test_validation_uses_the_start_field_rules(kit: Any, make_node: Node) -> None:
    graph = form_graph(
        kit,
        make_node,
        [
            {"name": "a", "type": "select"},
            {"name": "a", "type": "string"},
            {"name": "n", "type": "number", "default": "많이"},
        ],
    )
    found = {(issue.code, issue.field) for issue in validate(graph, "workflow")}

    assert {
        ("invalid_config", "fields[0].options"),
        ("duplicate_name", "fields[1].name"),
        ("invalid_config", "fields[2].default"),
    } <= found
    empty = form_graph(kit, make_node)
    raw = empty.model_dump()
    raw["nodes"][1]["config"]["fields"] = []
    issues = validate(type(empty).model_validate(raw), "workflow")
    assert ("invalid_config", "fields") in {(issue.code, issue.field) for issue in issues}


@pytest.mark.parametrize(
    ("stored", "kind"),
    [
        (
            {
                "node_id": "agent_1",
                "iteration_index": None,
                "child_run_id": None,
                "interaction": {
                    "interaction_id": "int_1",
                    "kind": "question",
                    "question": "몇 개?",
                    "created_at": "2026-10-14T00:00:00Z",
                },
            },
            "question",
        ),
    ],
)
def test_waits_saved_before_m19_get_their_kind(stored: dict[str, Any], kind: str) -> None:
    pending = WfPendingOut.model_validate(stored)

    assert pending.kind == kind
    assert pending.form is None


def test_catalog_entry() -> None:
    from app.workflow.nodes import default_registry

    described = {item.type: item for item in default_registry().describe()}
    entry = described["human_input"]

    assert entry.dynamic_outputs is True
    assert entry.kinds == ["chatflow", "workflow"]
    assert entry.error_strategies == ["fail"]
    assert set(entry.config_schema["properties"]) == {"description", "fields"}
