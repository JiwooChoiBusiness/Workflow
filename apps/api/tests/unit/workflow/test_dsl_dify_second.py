"""Dify DSL의 2차 노드 가져오기(2단계 설계 5.2, M19).

loop(+loop-start, 반복 변수, 종료 조건, 100회 상한), list-operator(파일 key, 정렬, 개수, n번째
고르기), document-extractor(여러 파일이면 texts), tool(내장 도구 넷에 맞는 것만), human-input(버튼
하나·입력 칸)을 우리 노드로 옮기고, 맞는 것이 없으면 unsupported 자리로 남기는지 본다. 우리
형식의 2차 노드 픽스처가 검증을 통과하는지도 본다(라운드트립은 test_dsl_roundtrip이 모든 픽스처로
돈다).
"""

from pathlib import Path
from typing import Any

from app.schemas.workflow import Node
from app.workflow.dsl.dify_import import convert_dify
from app.workflow.dsl.import_ import PreparedImport, read_dsl
from app.workflow.graph import validate

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "dsl"


def imported(name: str) -> PreparedImport:
    return read_dsl((FIXTURES / name).read_text(encoding="utf-8"))


def by_id(nodes: list[Node]) -> dict[str, Node]:
    return {node.id: node for node in nodes}


def test_second_phase_nodes_come_in() -> None:
    prepared = imported("dify/workflow_second_nodes.yaml")
    document = prepared.document
    nodes = by_id(document.graph.nodes)

    # 반복 끝내기(loop-end)만 옮기지 못한다.
    assert prepared.unsupported == ["n603"]
    issues = validate(document.graph, document.kind, env_names=[], conversation_vars={})
    assert [(issue.code, issue.node_id) for issue in issues] == [("unsupported_node", "n603")]
    assert sorted((w.code, w.node_id) for w in prepared.warnings) == [
        ("human_input_settings", "n9"),
        ("loop_count", "n6"),
        ("tool_param_dropped", "n3"),
        ("tool_param_dropped", "n8"),
        ("unsupported_node", "n603"),
    ]

    assert nodes["n2"].config == {"file_var": "n1.docs"}
    assert nodes["n3"].config == {
        "tool": "json_parse",
        "params": {
            # 여러 파일을 읽은 Dify text(배열)는 우리 texts다.
            "text": {"mode": "template", "value": "{{ n2.texts }}"},
            "path": {"mode": "const", "value": "$.items[0]"},
        },
    }
    assert nodes["n4"].config["params"] == {
        "text": {"mode": "var", "value": "n1.q"},
        "pattern": {"mode": "const", "value": r"\d+"},
    }
    assert nodes["n5"].config == {
        "input_var": "n1.docs",
        "filter": {
            "logical": "and",
            "conditions": [
                {"key": "extension", "op": "=", "value": "pdf"},
                {"key": "size", "op": ">", "value": 10},
            ],
        },
        "sort": {"key": "size", "order": "desc"},
        "slice": {"offset": 0, "limit": 2},
    }
    assert nodes["n6"].config == {
        "loop_vars": [
            {"name": "num", "type": "number", "value": 0},
            {"name": "topic", "type": "string", "var": "n1.q"},
        ],
        "break_conditions": [{"var": "n6.num", "op": "≥", "value": "3"}],
        "logical": "and",
        "max_iterations": 100,
    }
    assert (nodes["n6start"].type, nodes["n6start"].parent_id) == ("loop_start", "n6")
    # 반복 변수는 Dify와 같이 loop 노드 이름으로 읽고 바꾼다.
    assert nodes["n601"].config == {"items": [{"target": "n6.num", "op": "+=", "value": 1}]}
    assert nodes["n602"].config["vars"] == [
        {"name": "n", "var": "n6.num"},
        {"name": "t", "var": "n6.topic"},
    ]
    assert nodes["n7"].config["params"] == {
        "timezone": {"mode": "const", "value": "Asia/Seoul"},
        "format": {"mode": "const", "value": "%Y-%m-%d %H:%M"},
    }
    assert nodes["n8"].config == {
        "tool": "web_fetch",
        "params": {"url": {"mode": "template", "value": "https://example.com/{{ n4.text }}"}},
    }
    human = nodes["n9"].config
    assert human["description"] == "{{ n1.q }}에 대한 결과: {{ n6.topic }}"
    assert [(f["name"], f["type"], f["required"]) for f in human["fields"]] == [
        ("note", "paragraph", False),
        ("verdict", "select", True),
        ("proof", "file", True),
    ]
    assert human["fields"][0]["default"] == "없음"
    assert human["fields"][1]["options"] == ["좋음", "나쁨"]
    edges = [(e.source, e.source_handle, e.target) for e in document.graph.edges]
    # 버튼 하나의 human-input은 그 버튼 핸들이 out이 된다.
    assert ("n9", "out", "n10") in edges
    assert ("n6start", "out", "n601") in edges


def test_tools_without_a_builtin_match_and_branching_forms_stay_unsupported() -> None:
    raw: dict[str, Any] = {
        "app": {"mode": "workflow", "name": "안 맞는 노드"},
        "workflow": {
            "graph": {
                "nodes": [
                    {"id": "1", "data": {"type": "start", "variables": []}},
                    {
                        "id": "2",
                        "data": {
                            "type": "tool",
                            "provider_id": "langgenius/google/google",
                            "tool_name": "google_search",
                        },
                    },
                    {
                        "id": "3",
                        "data": {
                            "type": "human-input",
                            "inputs": [{"type": "paragraph", "output_variable_name": "memo"}],
                            "user_actions": [{"id": "approve"}, {"id": "reject"}],
                        },
                    },
                    {"id": "4", "data": {"type": "human-input", "inputs": []}},
                ],
                "edges": [
                    {"source": "1", "target": "2"},
                    {"source": "2", "target": "3"},
                    {"source": "3", "sourceHandle": "reject", "target": "4"},
                ],
            }
        },
    }

    document, warnings = convert_dify(raw)

    nodes = by_id(document.graph.nodes)
    assert [nodes[key].type for key in ("n2", "n3", "n4")] == ["unsupported"] * 3
    reasons = {w.node_id: w.message for w in warnings if w.code == "unsupported_node"}
    assert "google/google_search" in reasons["n2"]
    assert "버튼마다 갈라지는" in reasons["n3"]
    assert "입력 칸이 없는" in reasons["n4"]


def test_old_loops_without_a_start_node_get_one() -> None:
    raw: dict[str, Any] = {
        "app": {"mode": "workflow", "name": "옛 반복"},
        "workflow": {
            "graph": {
                "nodes": [
                    {"id": "1", "data": {"type": "start", "variables": []}},
                    {
                        "id": "2",
                        "data": {
                            "type": "loop",
                            "loop_count": 3,
                            "loop_variables": [
                                {"label": "n", "var_type": "number", "value_type": "constant"}
                            ],
                            "start_node_id": "3",
                        },
                    },
                    {
                        "id": "3",
                        "parentId": "2",
                        "data": {
                            "type": "assigner",
                            "items": [
                                {
                                    "variable_selector": ["2", "n"],
                                    "operation": "+=",
                                    "input_type": "constant",
                                    "value": 2,
                                }
                            ],
                        },
                    },
                    {"id": "4", "data": {"type": "end", "outputs": []}},
                ],
                "edges": [{"source": "1", "target": "2"}, {"source": "2", "target": "4"}],
            }
        },
    }

    document, _ = convert_dify(raw)

    nodes = by_id(document.graph.nodes)
    assert (nodes["n2_start"].type, nodes["n2_start"].parent_id) == ("loop_start", "n2")
    assert nodes["n2_start"].title == "루프 시작"
    edges = [(edge.source, edge.target) for edge in document.graph.edges]
    assert ("n2_start", "n3") in edges
    assert validate(document.graph, "workflow") == []


def test_list_operator_pick_and_unknown_file_keys() -> None:
    raw: dict[str, Any] = {
        "app": {"mode": "workflow", "name": "목록"},
        "workflow": {
            "graph": {
                "nodes": [
                    {
                        "id": "1",
                        "data": {
                            "type": "start",
                            "variables": [{"variable": "docs", "type": "file-list"}],
                        },
                    },
                    {
                        "id": "2",
                        "data": {
                            "type": "list-operator",
                            "variable": ["1", "docs"],
                            "filter_by": {
                                "enabled": True,
                                "conditions": [
                                    {"key": "type", "comparison_operator": "in", "value": ["image"]}
                                ],
                            },
                            "order_by": {"enabled": True, "key": "name", "value": "asc"},
                            "extract_by": {"enabled": True, "serial": "2"},
                        },
                    },
                ],
                "edges": [{"source": "1", "target": "2"}],
            }
        },
    }

    document, warnings = convert_dify(raw)

    config = by_id(document.graph.nodes)["n2"].config
    # Dify는 고른 원소 하나만 result에 남긴다. 그 자리 하나로 잘라 같은 결과를 낸다.
    assert config["slice"] == {"offset": 1, "limit": 1}
    assert config["filter"]["conditions"] == [{"key": "type", "op": "in", "value": ["image"]}]
    codes = {(w.code, w.node_id) for w in warnings}
    assert {("list_key_unsupported", "n2"), ("list_extract_order", "n2")} <= codes
    issues = validate(document.graph, "workflow")
    # 옮기지 못한 key는 그대로 두어 검증 오류로 드러낸다.
    assert ("invalid_config", "filter.conditions[0].key") in {(i.code, i.field) for i in issues}


def test_our_second_phase_fixture_is_valid() -> None:
    document = imported("ours/workflow_second_nodes.yaml").document

    assert validate(document.graph, document.kind, env_names=[], conversation_vars={}) == []
    assert {node.type for node in document.graph.nodes} >= {
        "loop",
        "loop_start",
        "list_operator",
        "document_extractor",
        "tool",
        "human_input",
    }
