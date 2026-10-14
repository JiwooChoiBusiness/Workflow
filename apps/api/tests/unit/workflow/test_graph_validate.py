"""그래프 검증(2단계 설계 3.1, M11). 픽스처를 조금씩 고쳐 정확히 어떤 이슈가 나오는지 고정한다."""

import time
from collections.abc import Callable
from typing import Any

import pytest

from app.schemas.workflow import Graph, Issue
from app.workflow.graph import analyze, subgraph, topological_order, validate

Node = Callable[..., dict[str, Any]]
FIXTURES = ["linear", "branch", "join", "parallel", "iteration"]


def found(issues: list[Issue]) -> list[tuple[str, str | None, str | None]]:
    return [(issue.code, issue.node_id or issue.edge_id, issue.field) for issue in issues]


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_graphs_have_no_issues(kit: Any, name: str) -> None:
    assert validate(kit.load(name), "workflow") == []


def test_cycle_marks_every_node_on_it(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("template_a", "template", {"template": "a"}),
            make_node("template_b", "template", {"template": "b"}),
            make_node("end_1", "end"),
        ],
        ["start>template_a", "template_a>template_b", "template_b>template_a", "template_b>end_1"],
    )

    assert found(validate(graph, "workflow")) == [
        ("cycle", "template_a", None),
        ("cycle", "template_b", None),
    ]


def test_self_loop_is_a_cycle(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("template_a", "template"),
            make_node("end_1", "end"),
        ],
        ["start>template_a", "template_a>template_a", "template_a>end_1"],
    )

    assert found(validate(graph, "workflow")) == [("cycle", "template_a", None)]


def test_unreachable_nodes(kit: Any) -> None:
    raw = kit.raw("linear")
    raw["nodes"].append({"id": "orphan", "type": "template", "config": {"template": "x"}})

    assert found(validate(Graph.model_validate(raw), "workflow")) == [
        ("unreachable_node", "orphan", None)
    ]


def test_reference_to_a_parallel_sibling_is_unreachable(kit: Any) -> None:
    raw = kit.raw("parallel")
    template_a = raw["nodes"][1]
    template_a["config"]["vars"].append({"name": "other", "var": "template_b.output"})

    assert found(validate(Graph.model_validate(raw), "workflow")) == [
        ("unreachable_reference", "template_a", "vars[1].var")
    ]


def test_reference_to_itself_or_downstream_is_unreachable(kit: Any) -> None:
    raw = kit.raw("linear")
    raw["nodes"][1]["config"]["vars"] += [
        {"name": "me", "var": "template_1.output"},
        {"name": "later", "var": "end_1.anything"},
    ]

    assert found(validate(Graph.model_validate(raw), "workflow")) == [
        ("unreachable_reference", "template_1", "vars[1].var"),
        ("unreachable_reference", "template_1", "vars[2].var"),
    ]


def test_unknown_nodes_outputs_and_bad_selectors(kit: Any) -> None:
    raw = kit.raw("linear")
    raw["nodes"][2]["config"]["outputs"] = [
        {"name": "a", "var": "nope.text"},
        {"name": "b", "var": "template_1.nope"},
        {"name": "c", "var": "template_1"},
        {"name": "d", "var": "sys.query"},
        {"name": "e", "var": "conv.topic"},
        {"name": "f", "var": "start.name.first"},
        {"name": "g", "var": "sys.run_id"},
    ]

    assert found(validate(Graph.model_validate(raw), "workflow")) == [
        ("unknown_variable", "end_1", "outputs[0].var"),
        ("unknown_variable", "end_1", "outputs[1].var"),
        ("invalid_selector", "end_1", "outputs[2].var"),
        ("unknown_variable", "end_1", "outputs[3].var"),
        ("unknown_variable", "end_1", "outputs[4].var"),
        ("invalid_selector", "end_1", "outputs[5].var"),
    ]


def test_template_references_are_checked_like_selectors(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            make_node(
                "answer_1",
                "answer",
                {
                    "text": (
                        "{{ start.q }} {{ start.nope }} {{ ghost.x }} {{ sys.query }} "
                        "{{ answer_2.text }} "
                        "{% for x in range(2) %}{{ x }}{{ loop.index }}{% endfor %}"
                    )
                },
            ),
            make_node("answer_2", "answer", {"text": "{{ conv.topic }} {{ env.TOKEN }}"}),
        ],
        ["start>answer_1", "answer_1>answer_2"],
    )

    issues = validate(graph, "chatflow", env_names=["TOKEN"], conversation_vars={"topic": "string"})

    assert found(issues) == [
        ("unreachable_reference", "answer_1", "text"),
        ("unknown_variable", "answer_1", "text"),
        ("unknown_variable", "answer_1", "text"),
    ]
    assert [issue.message for issue in issues][1:] == [
        "템플릿의 ghost은(는) 노드도 sys·env·conv도 아니다",
        "start에 nope 출력이 없다",
    ]


def test_env_and_conversation_names_are_checked_when_known(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("answer_1", "answer", {"text": "{{ env.MISSING }} {{ conv.nope }}"}),
        ],
        ["start>answer_1"],
    )

    assert validate(graph, "chatflow") == []
    assert [
        issue.code for issue in validate(graph, "chatflow", env_names=[], conversation_vars={})
    ] == [
        "unknown_variable",
        "unknown_variable",
    ]


def test_start_node_count(kit: Any, make_node: Node) -> None:
    no_start = kit.graph(
        [make_node("template_1", "template", {"template": "x"}), make_node("end_1", "end")],
        ["template_1>end_1"],
    )
    two_starts = kit.graph(
        [make_node("start", "start"), make_node("start_2", "start"), make_node("end_1", "end")],
        ["start>end_1", "start_2>end_1"],
    )

    assert found(validate(no_start, "workflow")) == [("start_missing", None, None)]
    assert found(validate(two_starts, "workflow")) == [
        ("start_duplicate", "start", None),
        ("start_duplicate", "start_2", None),
    ]


def test_workflow_and_chatflow_node_rules(kit: Any, make_node: Node) -> None:
    workflow = kit.graph(
        [
            make_node("start", "start"),
            make_node("answer_1", "answer", {"text": "hi"}),
            make_node(
                "variable_assigner_1",
                "variable_assigner",
                {"items": [{"target": "conv.a", "op": "clear"}]},
            ),
        ],
        ["start>answer_1", "answer_1>variable_assigner_1"],
    )
    chatflow = kit.graph([make_node("start", "start"), make_node("end_1", "end")], ["start>end_1"])

    assert found(validate(workflow, "workflow")) == [
        ("node_not_allowed", "answer_1", None),
        ("node_not_allowed", "variable_assigner_1", None),
        ("end_missing", None, None),
    ]
    assert found(validate(chatflow, "chatflow")) == [
        ("node_not_allowed", "end_1", None),
        ("answer_missing", None, None),
    ]


def test_handles_must_match_the_config(kit: Any) -> None:
    raw = kit.raw("branch")
    raw["edges"][1]["source_handle"] = "case_nope"
    raw["edges"][3]["source_handle"] = "fail"
    raw["edges"][4]["target_handle"] = "side"
    raw["edges"].append({"id": "e9", "source": "end_pass", "target": "end_fail"})

    issues = validate(Graph.model_validate(raw), "workflow")

    assert found(issues) == [
        ("invalid_handle", "if_else_1", "source_handle"),
        ("invalid_handle", "template_pass", "source_handle"),
        ("invalid_handle", "e5", "target_handle"),
        ("invalid_handle", "end_pass", "source_handle"),
    ]
    assert issues[1].message == "fail 핸들은 error.strategy가 branch일 때만 쓴다"
    assert [issue.edge_id for issue in issues] == ["e2", "e4", "e5", "e9"]


def test_control_nodes_only_take_the_fail_strategy(kit: Any) -> None:
    raw = kit.raw("linear")
    raw["nodes"][1]["error"] = {"strategy": "branch"}

    assert found(validate(Graph.model_validate(raw), "workflow")) == [
        ("unsupported_error_strategy", "template_1", "error.strategy")
    ]


def test_node_ids_types_and_config(kit: Any) -> None:
    raw = kit.raw("linear")
    raw["nodes"] += [
        {"id": "Bad-Id", "type": "template", "config": {}},
        {"id": "sys", "type": "template", "config": {}},
        {"id": "template_1", "type": "template", "config": {}},
        {"id": "knowledge_1", "type": "knowledge_retrieval", "config": {}},
        {"id": "template_v2", "type": "template", "version": 2, "config": {}},
        {"id": "if_else_9", "type": "if_else", "config": {"cases": []}},
    ]
    for node_id in ("Bad-Id", "sys", "knowledge_1", "template_v2", "if_else_9"):
        raw["edges"].append({"id": f"to_{node_id}", "source": "start", "target": node_id})

    issues = found(validate(Graph.model_validate(raw), "workflow"))

    assert issues == [
        ("invalid_node_id", "Bad-Id", "id"),
        ("invalid_node_id", "sys", "id"),
        ("duplicate_node_id", "template_1", "id"),
        ("unknown_node_type", "knowledge_1", "type"),
        ("unsupported_node_version", "template_v2", None),
        ("invalid_config", "if_else_9", "cases"),
    ]


def test_iteration_subgraph_rules(kit: Any) -> None:
    raw = kit.raw("iteration")
    raw["nodes"] += [
        {
            "id": "iteration_9",
            "type": "iteration",
            "parent_id": "iteration_1",
            "config": {"input_var": "start.docs", "output_var": "template_2.output"},
        },
        {"id": "end_9", "type": "end", "parent_id": "iteration_1", "config": {}},
        {"id": "template_x", "type": "template", "parent_id": "nowhere", "config": {}},
    ]
    raw["edges"] += [
        {"id": "cross", "source": "template_2", "target": "end_1"},
        {"id": "e_in", "source": "template_2", "target": "iteration_9"},
        {"id": "e_in2", "source": "template_2", "target": "iteration_start_1"},
        {"id": "e_end", "source": "template_2", "target": "end_9"},
    ]

    assert found(validate(Graph.model_validate(raw), "workflow")) == [
        ("nested_iteration", "iteration_9", None),
        ("node_not_allowed", "end_9", None),
        ("invalid_parent", "template_x", "parent_id"),
        ("cross_frame_edge", "cross", None),
        ("invalid_edge", "e_in2", None),
        # 하위 그래프가 없는 iteration_9의 output_var는 자기 하위 그래프를 가리킬 수 없다.
        ("unreachable_reference", "iteration_9", "output_var"),
    ]


def test_iteration_start_count_and_output_var(kit: Any) -> None:
    raw = kit.raw("iteration")
    raw["nodes"] = [node for node in raw["nodes"] if node["id"] != "iteration_start_1"]
    raw["edges"] = [edge for edge in raw["edges"] if edge["id"] != "e3"]
    raw["nodes"][1]["config"]["output_var"] = "start.docs"

    issues = found(validate(Graph.model_validate(raw), "workflow"))

    assert ("iteration_start_count", "iteration_1", None) in issues
    assert ("unreachable_reference", "iteration_1", "output_var") in issues


def test_outer_nodes_cannot_read_inside_an_iteration_but_inner_nodes_read_outside(
    kit: Any,
) -> None:
    raw = kit.raw("iteration")
    raw["nodes"][3]["config"]["vars"].append({"name": "outer", "var": "start.docs"})
    raw["nodes"][4]["config"]["outputs"].append({"name": "inner", "var": "template_2.output"})

    assert found(validate(Graph.model_validate(raw), "workflow")) == [
        ("unreachable_reference", "end_1", "outputs[1].var")
    ]


def test_if_else_operator_must_fit_the_variable_type(kit: Any) -> None:
    raw = kit.raw("branch")
    cases = raw["nodes"][1]["config"]["cases"]
    cases[0]["conditions"] += [
        {"var": "start.score", "op": "contains", "value": "1"},
        {"var": "start.score", "op": "in", "value": 3},
        {"var": "start.score", "op": "≥", "value": "많이"},
        {"var": "start.score", "op": "is_empty"},
    ]
    cases.append(
        {"id": "pass", "conditions": [{"var": "start.score", "op": "regex", "value": "("}]}
    )

    assert found(validate(Graph.model_validate(raw), "workflow")) == [
        ("duplicate_name", "if_else_1", "cases[1].id"),
        ("invalid_operator", "if_else_1", "cases[0].conditions[1].op"),
        ("invalid_config", "if_else_1", "cases[0].conditions[2].value"),
        ("invalid_config", "if_else_1", "cases[0].conditions[3].value"),
        ("invalid_operator", "if_else_1", "cases[1].conditions[0].op"),
    ]


def test_type_mismatch_is_only_a_warning(kit: Any) -> None:
    raw = kit.raw("join")
    raw["nodes"][4]["config"]["output_type"] = "array[object]"

    issues = validate(Graph.model_validate(raw), "workflow")

    assert {(issue.level, issue.code) for issue in issues} == {("warning", "type_mismatch")}
    assert analyze(Graph.model_validate(raw), "workflow").ok


def test_template_syntax_and_text_size(kit: Any) -> None:
    raw = kit.raw("linear")
    raw["nodes"][1]["config"]["template"] = "{% for x in %}"
    raw["nodes"][2]["config"]["outputs"][0]["name"] = "greeting"
    raw["nodes"].append(
        {"id": "template_big", "type": "template", "config": {"template": "가" * 30_000}}
    )
    raw["edges"].append({"id": "e9", "source": "start", "target": "template_big"})

    assert found(validate(Graph.model_validate(raw), "workflow")) == [
        ("text_too_long", "template_big", "template"),
        ("template_syntax", "template_1", "template"),
    ]


def test_size_limits(kit: Any, make_node: Node) -> None:
    nodes = [make_node("start", "start"), make_node("end_1", "end")]
    nodes += [make_node(f"template_{i}", "template", {"template": "x"}) for i in range(100)]
    edges = ["start>end_1"] + [f"start>template_{i}" for i in range(100)]
    edges += [f"template_{i}>end_1" for i in range(100)] + [
        f"template_{i}>end_1" for i in range(100)
    ]

    codes = [issue.code for issue in validate(kit.graph(nodes, edges), "workflow")]

    assert codes[:2] == ["too_many_nodes", "too_many_edges"]


def test_model_http_code_nodes_validate_with_their_outputs(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            make_node("llm_1", "llm", {"prompt": [{"role": "user", "text": "{{ start.q }}"}]}),
            make_node(
                "code_1",
                "code",
                {
                    "code": "def main(text):\n    return {'items': [text]}\n",
                    "inputs": [{"name": "text", "var": "llm_1.text"}],
                    "outputs": [{"name": "items", "type": "array[string]"}],
                },
            ),
            make_node(
                "question_classifier_1",
                "question_classifier",
                {
                    "query_var": "start.q",
                    "classes": [{"id": "x", "name": "엑스"}, {"id": "y", "name": "와이"}],
                },
            ),
            make_node("http_request_1", "http_request", {"url": "https://example.com"}),
            make_node(
                "end_x",
                "end",
                {
                    "outputs": [
                        {"name": "text", "var": "llm_1.text"},
                        {"name": "items", "var": "code_1.items"},
                        {"name": "status", "var": "http_request_1.status_code"},
                    ]
                },
            ),
            make_node(
                "end_y",
                "end",
                {"outputs": [{"name": "label", "var": "question_classifier_1.class_label"}]},
            ),
        ],
        [
            "start>llm_1",
            "llm_1>code_1",
            "code_1>question_classifier_1",
            "question_classifier_1:class_x>http_request_1",
            "http_request_1>end_x",
            "question_classifier_1:class_y>end_y",
        ],
    )

    assert validate(graph, "workflow") == []


def test_helpers_order_and_extract_subgraphs(kit: Any) -> None:
    graph = kit.load("iteration")

    assert topological_order(graph) == ["start", "iteration_1", "end_1"]
    assert topological_order(graph, "iteration_1") == ["iteration_start_1", "template_2"]
    inner = subgraph(graph, "iteration_1")
    assert [node.id for node in inner.nodes] == ["iteration_start_1", "template_2"]
    assert [edge.id for edge in inner.edges] == ["e3"]


def test_validating_100_nodes_and_300_edges_is_fast(make_node: Node, kit: Any) -> None:
    # start → 3개씩 33층(마지막 층 2개) → end. 노드마다 바로 위층을 읽는 템플릿을 둔다.
    middle = [f"t_{i}" for i in range(98)]
    layers = [["start"], *(middle[i : i + 3] for i in range(0, 98, 3))]
    nodes = [make_node("start", "start", {"fields": [{"name": "q", "type": "string"}]})]
    for depth, layer in enumerate(layers[1:], start=1):
        above = layers[depth - 1][0]
        for node_id in layer:
            source = "start.q" if above == "start" else f"{above}.output"
            template = "{{ a }}/{{ b }} {% for x in range(2) %}{{ x }}{% endfor %} " + node_id
            vars_ = [{"name": "a", "var": source}, {"name": "b", "var": "start.q"}]
            nodes.append(make_node(node_id, "template", {"template": template, "vars": vars_}))
    nodes.append(make_node("end_1", "end", {"outputs": [{"name": "v", "var": "t_97.output"}]}))
    edges = [
        f"{s}>{t}"
        for upper, lower in zip(layers, layers[1:], strict=False)
        for t in lower
        for s in upper
    ]
    edges += [f"{s}>end_1" for s in layers[-1]]
    skips = [f"{s}>{t}" for d in range(3, len(layers)) for t in layers[d] for s in layers[d - 2]]
    edges += skips[: 300 - len(edges)]
    graph = kit.graph(nodes, edges)
    assert (len(graph.nodes), len(graph.edges)) == (100, 300)

    started = time.perf_counter()
    cold = validate(graph, "workflow")
    cold_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    validate(graph, "workflow")
    warm_ms = (time.perf_counter() - started) * 1000

    print(f"\n노드 100개·엣지 300개 검증: 처음 {cold_ms:.1f}ms, 다시 {warm_ms:.1f}ms")
    assert cold == []
    # 목표는 50ms(설계 8장). 느린 CI에서 흔들리지 않게 상한은 넉넉히 둔다.
    assert cold_ms < 200
