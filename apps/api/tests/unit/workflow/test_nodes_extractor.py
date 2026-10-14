"""parameter_extractor 노드(2단계 설계 2.3, 11.2, M13). 모델은 가짜 어댑터 각본이다."""

from collections.abc import Callable
from typing import Any

from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text, fake_tool_call
from app.llm.types import ErrorEvent, ForcedTool, Usage
from app.workflow.graph import validate

Node = Callable[..., dict[str, Any]]

PARAMETERS = [
    {"name": "city", "type": "string", "description": "도시 이름", "required": True},
    {"name": "days", "type": "number", "description": "며칠 뒤인지"},
    {"name": "unit", "type": "string", "enum": ["C", "F"]},
    {"name": "tags", "type": "array[string]"},
]


def extractor_graph(kit: Any, make_node: Node, **config: Any) -> Any:
    body = {"query_var": "start.q", "parameters": PARAMETERS, **config}
    names = [item["name"] for item in body["parameters"]] + ["__is_success", "__reason"]
    return kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            make_node("parameter_extractor_1", "parameter_extractor", body),
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": name, "var": f"parameter_extractor_1.{name}"} for name in names
                    ]
                },
            ),
        ],
        ["start>parameter_extractor_1", "parameter_extractor_1>end_1"],
    )


def tool_turn(values: dict[str, Any]) -> list[Any]:
    return [
        *fake_tool_call("extract_parameters", values),
        fake_end("tool_use", usage=Usage(input_tokens=10, output_tokens=5)),
    ]


def text_turn(text: str) -> list[Any]:
    return [*fake_text(text), fake_end(usage=Usage(input_tokens=7, output_tokens=3))]


async def run_with(kit: Any, make_node: Node, turns: list[Any], **config: Any) -> Any:
    adapter = FakeAdapter(turns)
    run = await kit.run(
        extractor_graph(kit, make_node, **config),
        inputs={"q": "사흘 뒤 부산 날씨를 섭씨로"},
        ports=kit.ports(model=adapter),
    )
    return run, adapter


def attempts(run: Any) -> list[dict[str, Any]]:
    record = next(
        e.node_run
        for e in run.events
        if e.type == "node.completed" and e.data["node_id"] == "parameter_extractor_1"
    )
    assert record is not None
    return record.process["attempts"]


async def test_tool_call_mode_forces_the_schema(kit: Any, make_node: Node) -> None:
    run, adapter = await run_with(
        kit, make_node, [tool_turn({"city": "부산", "days": 3, "unit": "C", "tags": ["날씨"]})]
    )

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {
        "city": "부산",
        "days": 3,
        "unit": "C",
        "tags": ["날씨"],
        "__is_success": True,
        "__reason": "",
    }
    [request] = adapter.requests
    [tool] = request.tools
    assert tool.name == "extract_parameters"
    assert request.params.tool_choice == ForcedTool(name="extract_parameters")
    schema = tool.input_schema
    assert schema["required"] == ["city"]
    assert schema["properties"]["unit"] == {"type": "string", "enum": ["C", "F"]}
    assert schema["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert "extract_parameters 도구" in request.system
    assert run.outcome.usage.input_tokens == 10
    assert attempts(run) == [{"mode": "tool_call", "problems": 0}]


async def test_missing_tool_call_falls_back_to_prompt_mode(kit: Any, make_node: Node) -> None:
    run, adapter = await run_with(
        kit,
        make_node,
        [text_turn("부산이요"), text_turn('```json\n{"city": "부산", "days": "3"}\n```')],
    )

    assert run.outcome.outputs["city"] == "부산" and run.outcome.outputs["days"] == 3
    assert run.outcome.outputs["__is_success"] is True
    second = adapter.requests[1]
    assert second.tools == [] and '"required": ["city"]' in second.system
    assert run.outcome.usage.input_tokens == 14
    assert [a["mode"] for a in attempts(run)] == ["tool_call", "prompt"]


async def test_rejected_tool_request_falls_back_to_prompt_mode(kit: Any, make_node: Node) -> None:
    refused = [ErrorEvent(code="model_request_invalid", message="schema 400", retryable=False)]
    run, _ = await run_with(kit, make_node, [refused, text_turn('{"city": "부산"}')])

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs["city"] == "부산"
    assert attempts(run)[0] == {"mode": "tool_call", "error": "model_request_invalid"}


async def test_bad_values_are_nulled_and_reported(kit: Any, make_node: Node) -> None:
    run, _ = await run_with(
        kit,
        make_node,
        [
            tool_turn({"days": "사흘", "unit": "K"}),
            text_turn('{"days": 3, "unit": "K"}'),
        ],
    )

    outputs = run.outcome.outputs
    assert run.outcome.status == "succeeded"
    assert (outputs["city"], outputs["days"], outputs["unit"]) == (None, 3, None)
    assert outputs["__is_success"] is False
    assert "city" in outputs["__reason"] and "unit" in outputs["__reason"]


async def test_no_json_at_all_still_succeeds_with_the_reason(kit: Any, make_node: Node) -> None:
    run, adapter = await run_with(
        kit, make_node, [text_turn("모르겠어요")], reasoning_mode="prompt"
    )

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs["__is_success"] is False
    assert run.outcome.outputs["__reason"] == "모델 응답에서 파라미터 JSON을 찾지 못했다"
    assert len(adapter.requests) == 1


async def test_retryable_model_errors_fail_the_node(kit: Any, make_node: Node) -> None:
    busy = [ErrorEvent(code="model_unavailable", message="429", retryable=True)]
    run, _ = await run_with(kit, make_node, [busy])

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert (run.outcome.error.code, run.outcome.error.retryable) == ("model_unavailable", True)


def test_validation_rules(kit: Any, make_node: Node) -> None:
    def issues(parameters: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
        graph = extractor_graph(kit, make_node, parameters=parameters)
        return [
            (i.code, i.field)
            for i in validate(graph, "workflow")
            if i.node_id == "parameter_extractor_1"
        ]

    assert issues(PARAMETERS) == []
    assert issues([{"name": "a"}, {"name": "a"}]) == [("duplicate_name", "parameters[1].name")]
    assert issues([{"name": "flag", "type": "boolean", "enum": ["x"]}]) == [
        ("invalid_config", "parameters[0].enum")
    ]
    assert issues([{"name": "__x"}])[0] == ("invalid_config", "parameters[0].name")
