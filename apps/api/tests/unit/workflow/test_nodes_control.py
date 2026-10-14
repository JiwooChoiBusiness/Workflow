"""제어 노드 7종과 레지스트리(2단계 설계 2.3, M11)."""

from collections.abc import Callable
from typing import Any

import pytest

from app.workflow.graph import validate
from app.workflow.nodes import default_registry
from app.workflow.nodes.assigner import apply, cleared
from app.workflow.nodes.base import NodeFailure
from app.workflow.nodes.if_else import evaluate
from app.workflow.nodes.start import InputError, StartField, coerce_inputs

Node = Callable[..., dict[str, Any]]

FILE = {
    "file_id": "0190c1b2-0000-7000-8000-000000000001",
    "filename": "보고서.pdf",
    "mime": "application/pdf",
    "size_bytes": 10,
    "storage_key": "wf-files/w/f",
    "source": "wf_file",
}


def fields(*specs: dict[str, Any]) -> list[StartField]:
    return [StartField.model_validate(spec) for spec in specs]


def test_start_coerces_types_and_fills_defaults() -> None:
    values = coerce_inputs(
        fields(
            {"name": "title", "type": "string"},
            {"name": "count", "type": "number"},
            {"name": "flag", "type": "boolean"},
            {"name": "level", "type": "select", "options": ["상", "하"], "default": "하"},
            {"name": "note", "type": "paragraph"},
            {"name": "doc", "type": "file"},
            {"name": "docs", "type": "file_list"},
        ),
        {"title": 12, "count": "3.5", "flag": True, "doc": FILE, "docs": [FILE], "extra": 1},
    )

    assert values == {
        "title": "12",
        "count": 3.5,
        "flag": True,
        "level": "하",
        "note": None,
        "doc": FILE,
        "docs": [FILE],
    }


def test_start_reports_every_bad_field() -> None:
    with pytest.raises(InputError) as caught:
        coerce_inputs(
            fields(
                {"name": "title", "type": "string", "required": True},
                {"name": "short", "type": "string", "max_length": 3},
                {"name": "count", "type": "number"},
                {"name": "flag", "type": "boolean"},
                {"name": "level", "type": "select", "options": ["상", "하"]},
                {"name": "doc", "type": "file"},
            ),
            {
                "title": "",
                "short": "길다길다",
                "count": "많이",
                "flag": "yes",
                "level": "중",
                "doc": {"file_id": "x"},
            },
        )

    assert set(caught.value.errors) == {"title", "short", "count", "flag", "level", "doc"}


async def test_start_fields_become_start_outputs(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "n", "type": "number"}]}),
            make_node("end_1", "end", {"outputs": [{"name": "n", "var": "start.n"}]}),
        ],
        ["start>end_1"],
    )

    run = await kit.run(graph, inputs={"n": "7"})

    assert run.outcome.outputs == {"n": 7}
    start = run.events[2].node_run
    assert start is not None and start.inputs == {"n": "7"} and start.outputs == {"n": 7}


async def test_end_reads_null_for_skipped_and_merges_in_completion_order(kit: Any) -> None:
    run = await kit.run(kit.load("branch"), inputs={"score": 100})

    assert run.outcome.outputs == {"result": "합격(100점)"}
    assert run.statuses()["end_fail"] == "skipped"


def test_template_node_can_only_use_names_from_vars(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "items", "type": "string"}]}),
            make_node(
                "template_1",
                "template",
                {
                    "template": "{{ raw }}{{ start.items }}",
                    "vars": [{"name": "raw", "var": "start.items"}],
                },
            ),
            make_node("end_1", "end", {"outputs": [{"name": "text", "var": "template_1.output"}]}),
        ],
        ["start>template_1", "template_1>end_1"],
    )

    # 변수 풀 전체가 아니라 vars가 컨텍스트라 start.items를 바로 쓸 수 없다.
    issues = validate(graph, "workflow")
    assert [(issue.code, issue.field) for issue in issues] == [("unknown_variable", "template")]


async def test_missing_keys_render_empty_with_a_warning(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "doc", "type": "file"}]}),
            make_node(
                "template_1",
                "template",
                {"template": "{{ d }}|{{ d.nope }}|", "vars": [{"name": "d", "var": "start.doc"}]},
            ),
            make_node("end_1", "end", {"outputs": [{"name": "text", "var": "template_1.output"}]}),
        ],
        ["start>template_1", "template_1>end_1"],
    )

    run = await kit.run(graph, inputs={"doc": FILE})

    assert run.outcome.outputs == {"text": "보고서.pdf||"}
    template = next(d for d in run.of("node.completed") if d["node_id"] == "template_1")
    assert template["warnings"] == ["missing_variable: nope"]


async def test_template_node_output_and_warnings(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "items", "type": "string"}]}),
            make_node(
                "template_1",
                "template",
                {
                    "template": "{% for x in raw | fromjson %}[{{ x }}]{% endfor %}{{ raw2 }}",
                    "vars": [
                        {"name": "raw", "var": "start.items"},
                        {"name": "raw2", "var": "start.items"},
                    ],
                },
            ),
            make_node("end_1", "end", {"outputs": [{"name": "text", "var": "template_1.output"}]}),
        ],
        ["start>template_1", "template_1>end_1"],
    )

    run = await kit.run(graph, inputs={"items": "[1, 2]"})

    assert run.outcome.outputs == {"text": "[1][2][1, 2]"}
    template = next(
        e
        for e in run.events
        if e.data.get("node_id") == "template_1" and e.type == "node.completed"
    )
    assert template.node_run is not None
    assert template.node_run.inputs == {"raw": "[1, 2]", "raw2": "[1, 2]"}


async def test_template_errors_become_node_errors(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node(
                "template_1", "template", {"template": "{% for i in range(20000) %}{% endfor %}"}
            ),
            make_node("end_1", "end", {"outputs": [{"name": "t", "var": "template_1.output"}]}),
        ],
        ["start>template_1", "template_1>end_1"],
    )

    run = await kit.run(graph)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "template_limit"


@pytest.mark.parametrize(
    ("op", "actual", "expected", "result"),
    [
        ("=", "a", "a", True),
        ("=", 5, "5", True),
        ("=", True, "true", True),
        ("=", True, 1, False),
        ("≠", "a", "b", True),
        ("contains", "hello", "ell", True),
        ("contains", ["a", "b"], "b", True),
        ("not_contains", ["a", "b"], "c", True),
        ("starts_with", "hello", "he", True),
        ("ends_with", "hello", "lo", True),
        ("is_empty", "", None, True),
        ("is_empty", [], None, True),
        ("is_empty", 0, None, False),
        ("is_not_empty", {"a": 1}, None, True),
        (">", 10, 9, True),
        ("<", "3", 4, True),
        ("≥", 5, "5", True),
        ("≤", 5.5, 5, False),
        ("in", "b", ["a", "b"], True),
        ("not_in", 3, [1, 2], True),
        ("regex", "order-123", r"\d{3}$", True),
        ("=", None, "a", False),
        ("≠", None, "a", True),
        ("contains", None, "a", False),
        ("not_contains", None, "a", True),
        (">", None, 1, False),
    ],
)
def test_if_else_operators(op: str, actual: Any, expected: Any, result: bool) -> None:
    assert evaluate(op, actual, expected) is result


def test_if_else_number_comparison_with_text_is_a_type_mismatch() -> None:
    with pytest.raises(NodeFailure) as caught:
        evaluate(">", "많이", 3)

    assert caught.value.code == "type_mismatch"


async def test_if_else_takes_the_first_matching_case(kit: Any, make_node: Node) -> None:
    cases = [
        {
            "id": "a",
            "logical": "or",
            "conditions": [
                {"var": "start.n", "op": ">", "value": 100},
                {"var": "start.word", "op": "=", "value": "특급"},
            ],
        },
        {"id": "b", "conditions": [{"var": "start.n", "op": ">", "value": 10}]},
    ]
    graph = kit.graph(
        [
            make_node(
                "start",
                "start",
                {
                    "fields": [
                        {"name": "n", "type": "number"},
                        {"name": "word", "type": "string"},
                    ]
                },
            ),
            make_node("if_else_1", "if_else", {"cases": cases}),
            make_node("template_a", "template", {"template": "a"}),
            make_node("template_b", "template", {"template": "b"}),
            make_node("template_else", "template", {"template": "else"}),
            make_node(
                "variable_aggregator_1",
                "variable_aggregator",
                {
                    "vars": ["template_a.output", "template_b.output", "template_else.output"],
                },
            ),
            make_node(
                "end_1", "end", {"outputs": [{"name": "v", "var": "variable_aggregator_1.output"}]}
            ),
        ],
        [
            "start>if_else_1",
            "if_else_1:case_a>template_a",
            "if_else_1:case_b>template_b",
            "if_else_1:else>template_else",
            "template_a>variable_aggregator_1",
            "template_b>variable_aggregator_1",
            "template_else>variable_aggregator_1",
            "variable_aggregator_1>end_1",
        ],
    )

    for inputs, expected in [
        ({"n": 50, "word": "특급"}, "a"),
        ({"n": 50, "word": "보통"}, "b"),
        ({"n": 1, "word": "보통"}, "else"),
    ]:
        run = await kit.run(graph, inputs=inputs)
        assert run.outcome.outputs == {"v": expected}, inputs


async def test_answer_streams_and_concatenates_in_completion_order(
    kit: Any, make_node: Node
) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("answer_1", "answer", {"text": "질문은 {{ sys.query }}. "}),
            make_node("answer_2", "answer", {"text": "{{ answer_1.text }}끝"}),
        ],
        ["start>answer_1", "answer_1>answer_2"],
    )

    run = await kit.run(
        graph, kind="chatflow", system={"query": "날씨", "conversation_id": "c1", "turn": 2}
    )

    assert run.outcome.status == "succeeded"
    assert [d["text"] for d in run.of("answer.delta")] == ["질문은 날씨. ", "질문은 날씨. 끝"]
    assert [d["node_id"] for d in run.of("answer.delta")] == ["answer_1", "answer_2"]
    assert run.outcome.answer == "질문은 날씨. 질문은 날씨. 끝"
    # 챗플로우에는 end 출력이 없다.
    assert run.outcome.outputs == {}


async def test_assigner_updates_conversation_variables(kit: Any, make_node: Node) -> None:
    items = [
        {"target": "conv.topic", "op": "overwrite", "var": "sys.query"},
        {"target": "conv.count", "op": "+=", "value": 2},
        {"target": "conv.count", "op": "-=", "value": "0.5"},
        {"target": "conv.history", "op": "append", "var": "sys.query"},
        {"target": "conv.memo", "op": "clear"},
    ]
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("answer_1", "answer", {"text": "{{ conv.topic }}"}),
            make_node("variable_assigner_1", "variable_assigner", {"items": items}),
            make_node("answer_2", "answer", {"text": "/{{ conv.topic }}/{{ conv.count }}"}),
        ],
        ["start>answer_1", "answer_1>variable_assigner_1", "variable_assigner_1>answer_2"],
    )
    types = {"topic": "string", "count": "number", "history": "array[string]", "memo": "string"}

    run = await kit.run(
        graph,
        kind="chatflow",
        system={"query": "여행"},
        conversation={"topic": "날씨", "count": 1, "history": ["날씨"], "memo": "x"},
        conversation_types=types,
    )

    assert run.outcome.status == "succeeded"
    assert run.outcome.conversation == {
        "topic": "여행",
        "count": 2.5,
        "history": ["날씨", "여행"],
        "memo": "",
    }
    # 앞 answer는 바뀌기 전 값, 뒤 answer는 바뀐 값을 본다.
    assert run.outcome.answer == "날씨/여행/2.5"


def test_assigner_operations() -> None:
    assert apply("overwrite", 1, "3", "number") == 3
    assert apply("append", None, 1, "array[number]") == [1]
    assert apply("+=", None, 2, "number") == 2
    assert cleared("array[file]") == [] and cleared("object") == {} and cleared(None) is None
    with pytest.raises(NodeFailure):
        apply("append", "문자열", 1, "array[any]")
    with pytest.raises(NodeFailure):
        apply("overwrite", None, {"a": 1}, "number")


async def test_iteration_fixture_validates_and_runs(kit: Any) -> None:
    graph = kit.load("iteration")
    assert validate(graph, "workflow") == []

    run = await kit.run(graph, inputs={"docs": [FILE, {**FILE, "filename": "부록.txt"}]})

    assert run.outcome.status == "succeeded"
    # 파일은 템플릿에서 파일 이름으로 렌더된다.
    assert run.outcome.outputs == {"names": ["0: 보고서.pdf", "1: 부록.txt"]}


def test_registry_describes_every_type() -> None:
    described = {item.type: item for item in default_registry().describe()}

    implemented = {
        "start",
        "end",
        "answer",
        "template",
        "if_else",
        "variable_aggregator",
        "variable_assigner",
        "llm",
        "http_request",
        "code",
        "question_classifier",
        "parameter_extractor",
        "iteration",
        "iteration_start",
        "agent",
    }
    assert set(described) == implemented
    assert all(item.implemented for item in described.values())
    assert described["end"].kinds == ["workflow"]
    assert described["answer"].kinds == ["chatflow"]
    assert described["variable_assigner"].kinds == ["chatflow"]
    assert described["template"].outputs[0].name == "output"
    assert described["start"].dynamic_outputs is True
    assert described["if_else"].branching is True
    assert described["llm"].error_strategies == ["fail", "default", "branch"]
    assert described["if_else"].error_strategies == ["fail"]
    assert "properties" in described["if_else"].config_schema
