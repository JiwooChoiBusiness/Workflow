"""Dify DSL 부분 가져오기(2단계 설계 5.2, A9, M18).

tests/fixtures/dsl/dify의 예제 셋은 Dify DSL 구조를 보고 직접 쓴 파일이다. 지원 노드만 우리 노드로
들어오고 나머지는 unsupported 자리로 남으며 엣지는 그대로다. 노드 id·변수 참조·모델·핸들 변환과
옛 형식(케이스 없는 if-else, iteration-start 없는 iteration)도 본다.
"""

from pathlib import Path
from typing import Any

import pytest

from app.schemas.workflow import Node
from app.workflow.dsl.dify_import import convert_dify, node_id_like
from app.workflow.dsl.import_ import PreparedImport, parse_yaml, read_dsl
from app.workflow.dsl.schema import DslError
from app.workflow.graph import validate
from app.workflow.render import render

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "dsl" / "dify"


def imported(name: str) -> PreparedImport:
    return read_dsl((FIXTURES / name).read_text(encoding="utf-8"))


def nodes_of(prepared: PreparedImport) -> dict[str, Node]:
    return {node.id: node for node in prepared.document.graph.nodes}


def issues_of(prepared: PreparedImport) -> list[tuple[str, str | None]]:
    document = prepared.document
    issues = validate(
        document.graph,
        document.kind,
        env_names=[item.name for item in document.env_vars],
        conversation_vars={item.name: item.type for item in document.conversation_vars},
    )
    return [(issue.code, issue.node_id) for issue in issues]


def warnings_of(prepared: PreparedImport) -> list[tuple[str, str | None]]:
    return [(warning.code, warning.node_id) for warning in prepared.warnings]


def edges_of(prepared: PreparedImport) -> list[tuple[str, str, str]]:
    return [
        (edge.source, edge.source_handle, edge.target) for edge in prepared.document.graph.edges
    ]


def test_numeric_ids_get_an_n_prefix() -> None:
    assert node_id_like("1720000000001") == "n1720000000001"
    assert node_id_like("llm") == "llm"
    assert node_id_like("4start") == "n4start"
    assert node_id_like("Start-Node") == "start_node"
    assert node_id_like("sys") == "n_sys"
    assert len(node_id_like("9" * 40)) == 32


def test_workflow_example_comes_in_with_only_supported_nodes() -> None:
    prepared = imported("workflow_search.yaml")
    document = prepared.document
    nodes = nodes_of(prepared)

    assert (prepared.source, document.kind, document.name, document.icon) == (
        "dify",
        "workflow",
        "검색 요약",
        "🔎",
    )
    assert prepared.unsupported == []
    assert [node.type for node in document.graph.nodes] == [
        "start",
        "http_request",
        "code",
        "if_else",
        "template",
        "llm",
        "template",
        "variable_aggregator",
        "end",
    ]
    assert issues_of(prepared) == [("imported_code_unreviewed", "n1720000000003")]
    assert set(warnings_of(prepared)) == {
        ("notes_skipped", None),
        ("model_replaced", "n1720000000006"),
        ("dependencies_ignored", None),
        ("features_ignored", None),
        ("imported_code", None),
        ("env_value_missing", None),
    }
    assert [(item.name, item.value, item.secret) for item in document.env_vars] == [
        ("SEARCH_KEY", None, True),
        ("SEARCH_BASE", "https://search.example.com", False),
    ]
    assert document.graph.viewport == {"x": 12.5, "y": -40.0, "zoom": 0.7}

    start = nodes["n1720000000001"].config
    assert start["fields"] == [
        {
            "name": "keyword",
            "type": "string",
            "label": "검색어",
            "required": True,
            "max_length": 100,
        },
        {
            "name": "lang",
            "type": "select",
            "label": "언어",
            "required": False,
            "options": ["ko", "en"],
        },
    ]
    http = nodes["n1720000000002"]
    assert http.config["method"] == "GET"
    assert http.config["url"] == "{{ env.SEARCH_BASE }}/v1/search"
    assert http.config["headers"] == [{"name": "Accept", "value": "application/json"}]
    assert http.config["params"] == [
        {"name": "q", "value": "{{ n1720000000001.keyword }}"},
        {"name": "lang", "value": "{{ n1720000000001.lang }}"},
    ]
    assert http.config["auth"] == {"type": "bearer", "value": "{{ env.SEARCH_KEY }}"}
    assert http.config["timeout_sec"] == 30
    assert http.retry.max_attempts == 2 and http.retry.interval_ms == 500
    code = nodes["n1720000000003"]
    assert code.config["inputs"] == [{"name": "body", "var": "n1720000000002.body"}]
    assert code.config["outputs"] == [
        {"name": "count", "type": "number"},
        {"name": "titles", "type": "array[string]"},
    ]
    assert code.config["code"].startswith("import json\n\n\ndef main(body: str) -> dict:\n")
    assert code.ui == {"imported_code": True}
    assert nodes["n1720000000004"].config == {
        "cases": [
            {
                "id": "true",
                "logical": "and",
                "conditions": [{"var": "n1720000000003.count", "op": ">", "value": "0"}],
            }
        ]
    }
    llm = nodes["n1720000000006"].config
    assert llm["model"] == {"params": {"temperature": 0.3, "max_output_tokens": 512}}
    assert llm["prompt"][1] == {
        "role": "user",
        "text": "검색어: {{ n1720000000001.keyword }}\n결과:\n{{ n1720000000005.output }}",
    }
    assert nodes["n1720000000008"].config == {
        "vars": ["n1720000000006.text", "n1720000000007.output"],
        "output_type": "string",
    }
    assert nodes["n1720000000009"].config["outputs"] == [
        {"name": "summary", "var": "n1720000000008.output"},
        {"name": "count", "var": "n1720000000003.count"},
    ]
    assert ("n1720000000004", "case_true", "n1720000000005") in edges_of(prepared)
    assert ("n1720000000004", "else", "n1720000000007") in edges_of(prepared)
    assert len(edges_of(prepared)) == 9


def test_literal_braces_in_a_dify_prompt_stay_text() -> None:
    nodes = nodes_of(imported("workflow_search.yaml"))
    system = nodes["n1720000000006"].config["prompt"][0]["text"]

    assert "{{ '{{' }}이름}}" in system
    assert render(system, {}).text == (
        "검색 결과를 세 줄로 요약한다. 자리 표시는 {{이름}}처럼 쓰지 않는다. "
        '형식 예: {"요약": "..."}'
    )


def test_chatflow_example_keeps_unsupported_nodes_in_place() -> None:
    prepared = imported("chatflow_support.yaml")
    document = prepared.document
    nodes = nodes_of(prepared)

    assert document.kind == "chatflow"
    # 도구(time/current_time)는 M19부터 내장 current_time으로 들어온다.
    assert prepared.unsupported == ["n1730000000003"]
    assert sorted(issues_of(prepared)) == [("unsupported_node", "n1730000000003")]
    assert set(warnings_of(prepared)) == {
        ("model_replaced", "n1730000000002"),
        ("model_replaced", "n1730000000004"),
        ("model_replaced", "n1730000000006"),
        ("unsupported_node", "n1730000000003"),
        ("features_ignored", None),
    }
    assert nodes["n1730000000009"].type == "tool"
    assert nodes["n1730000000009"].config == {"tool": "current_time", "params": {}}
    knowledge = nodes["n1730000000003"]
    assert knowledge.type == "unsupported" and knowledge.title == "환불 규정 검색"
    assert knowledge.config["original_type"] == "knowledge-retrieval"
    assert knowledge.config["data"]["retrieval_mode"] == "multiple"
    # 옮기지 못한 노드의 엣지도 그대로 남는다.
    edges = edges_of(prepared)
    assert ("n1730000000002", "class_1", "n1730000000003") in edges
    assert ("n1730000000003", "out", "n1730000000004") in edges
    assert ("n1730000000009", "out", "n1730000000010") in edges

    classifier = nodes["n1730000000002"].config
    assert classifier["query_var"] == "sys.query"
    assert classifier["classes"] == [
        {"id": "1", "name": "환불·결제 문의", "description": "환불·결제 문의"},
        {"id": "2", "name": "배송 문의", "description": "배송 문의\n(주문 번호가 있을 때)"},
        {"id": "3", "name": "그 밖의 질문", "description": "그 밖의 질문"},
    ]
    assert {edge[1] for edge in edges if edge[0] == "n1730000000002"} == {
        "class_1",
        "class_2",
        "class_3",
    }
    llm = nodes["n1730000000004"].config
    assert llm["memory"] == {"enabled": True, "window_turns": 6}
    assert llm["prompt"] == [
        {
            "role": "system",
            "text": "다음 자료만 보고 환불 규정을 설명한다.\n{{ n1730000000003.result }}\n"
            "지금까지 다룬 주제: {{ conv.topic }}",
        },
        {"role": "user", "text": "{{ sys.query }}"},
    ]
    assert llm["vision"] == {"enabled": True, "var": "sys.files"}
    assert llm["model"] == {"params": {"temperature": 0.7, "top_p": 0.9}}
    extractor = nodes["n1730000000006"].config
    assert extractor["reasoning_mode"] == "tool_call"
    assert extractor["parameters"][1] == {
        "name": "urgency",
        "type": "string",
        "description": "급한 정도",
        "required": False,
        "enum": ["높음", "보통"],
    }
    assert nodes["n1730000000007"].config == {
        "items": [
            {"target": "conv.topic", "op": "overwrite", "value": "배송"},
            {"target": "conv.visits", "op": "+=", "value": 1},
        ]
    }
    assert nodes["n1730000000008"].config["text"] == (
        "주문 {{ n1730000000006.order_id }}의 배송 상황을 확인하겠습니다. "
        "(상담 {{ conv.visits }}번째)"
    )
    assert [(item.name, item.type, item.default) for item in document.conversation_vars] == [
        ("topic", "string", ""),
        ("visits", "number", 0),
    ]
    webapp = document.settings.webapp
    assert webapp.opening_statement == "안녕하세요! 무엇을 도와드릴까요?"
    assert webapp.suggested_questions == ["환불 규정이 궁금해요", "배송은 얼마나 걸리나요?"]


def test_batch_example_maps_iteration_items_and_files() -> None:
    prepared = imported("workflow_batch.yaml")
    document = prepared.document
    nodes = nodes_of(prepared)

    assert document.icon is None
    # list-operator·document-extractor는 M19부터 우리 노드로 들어온다.
    assert prepared.unsupported == ["n2"]
    assert sorted(issues_of(prepared)) == [("unsupported_node", "n2")]
    assert set(warnings_of(prepared)) == {
        ("file_upload_field", "n1"),
        ("unsupported_node", "n2"),
        ("iteration_parallel", "n4"),
        ("model_replaced", "n4002"),
        ("edge_dropped", None),
        ("icon_dropped", None),
    }
    assert nodes["n3"].config == {"input_var": "n2.list", "slice": {"offset": 0, "limit": 5}}
    assert nodes["n5"].config == {"file_var": "n1.files"}
    # 여러 파일을 읽은 Dify text(배열)는 우리 texts다.
    assert nodes["n6"].config["vars"][1] == {"name": "files_text", "var": "n5.texts"}
    assert nodes["n2"].config["original_type"] == "code"
    assert nodes["n1"].config["fields"][-1] == {
        "name": "files",
        "type": "file_list",
        "label": "파일",
        "required": False,
    }
    iteration = nodes["n4"]
    assert iteration.config == {
        "input_var": "n3.result",
        "output_var": "n4002.text",
        "parallel": True,
        "max_parallel": 4,
        "error_mode": "continue",
    }
    start, llm = nodes["n4start"], nodes["n4002"]
    assert (start.type, start.parent_id, llm.parent_id) == ("iteration_start", "n4", "n4")
    assert llm.config["prompt"] == [
        {
            "role": "user",
            "text": "{{ n4start.index }}번째 주소 {{ n4start.item }}의 내용을 두 줄로 요약해 줘",
        }
    ]
    assert llm.error.strategy == "default"
    assert llm.error.default_outputs == {"text": "요약 실패"}
    assert ("n4start", "out", "n4002") in edges_of(prepared)
    assert [(item.name, item.value) for item in document.env_vars] == [("MAX_ITEMS", "5")]


def test_iteration_without_a_start_node_gets_one() -> None:
    raw = parse_yaml(
        """
app: {mode: workflow, name: 옛 반복}
workflow:
  graph:
    nodes:
    - id: '1'
      data: {type: start, title: 시작, variables: []}
    - id: '2'
      data:
        type: iteration
        title: 반복
        iterator_selector: ['1', list]
        output_selector: ['3', output]
        start_node_id: '3'
    - id: '3'
      parentId: '2'
      data:
        type: template-transform
        title: 글
        template: '{{ x }}'
        variables:
        - {variable: x, value_selector: ['2', item]}
    - id: '4'
      data:
        type: end
        title: 끝
        outputs:
        - {variable: out, value_selector: ['2', output]}
    edges:
    - {source: '1', target: '2'}
    - {source: '2', target: '4'}
"""
    )

    document, _ = convert_dify(raw)

    by_id = {node.id: node for node in document.graph.nodes}
    assert by_id["n2_start"].type == "iteration_start"
    assert by_id["n2_start"].parent_id == "n2"
    assert by_id["n2_start"].title == "반복 시작"
    assert by_id["n3"].config["vars"] == [{"name": "x", "var": "n2_start.item"}]
    assert ("n2_start", "out", "n3") in [
        (edge.source, edge.source_handle, edge.target) for edge in document.graph.edges
    ]


def test_old_style_nodes_and_options() -> None:
    raw = parse_yaml(
        """
app: {mode: workflow, name: 옛 형식}
workflow:
  graph:
    nodes:
    - id: '1'
      data:
        type: start
        variables:
        - {variable: q, label: 질문, type: text-input}
        - {variable: mood, label: 기분, type: rating}
    - id: '2'
      data:
        type: if-else
        logical_operator: or
        conditions:
        - {variable_selector: ['1', q], comparison_operator: start with, value: 안녕}
        - {variable_selector: ['1', q], comparison_operator: all of, value: [a]}
        - {variable_selector: ['1', q], comparison_operator: empty}
    - id: '3'
      data:
        type: variable-assigner
        output_type: string
        variables:
        - ['1', q]
        - ['5', structured_output]
    - id: '5'
      data:
        type: llm
        model: {provider: openai, name: gpt-4o}
        structured_output_enabled: true
        structured_output:
          schema: {type: object, properties: {a: {type: string}}}
        prompt_config:
          jinja2_variables:
          - {variable: question, value_selector: ['1', q]}
          - {variable: who, value_selector: [sys, user_id]}
        prompt_template:
        - role: user
          edition_type: jinja2
          text: 옛 글
          jinja2_text: '{{ question }} ({{ who }})'
    - id: '6'
      data:
        type: http-request
        method: post
        url: https://example.com/{{#1.q#}}
        headers:
        - {key: X-Trace, value: '{{#sys.workflow_run_id#}}'}
        body:
          type: form-data
          data:
          - {key: name, type: text, value: '{{#1.q#}}'}
          - {key: upload, type: file, file: ['1', doc]}
        authorization:
          type: api-key
          config: {type: basic, api_key: dXNlcjpwYXNz}
        ssl_verify: false
    - id: '7'
      data:
        type: http-request
        method: put
        url: https://example.com
        body: {type: raw-text, data: 'raw {{#1.q#}}'}
        authorization:
          type: api-key
          config: {type: custom, header: X-Key, api_key: abc}
    - id: '8'
      data:
        type: assigner
        assigned_variable_selector: [conversation, memo]
        input_variable_selector: ['1', q]
        write_mode: over-write
    - id: '9'
      data: {type: agent, title: 에이전트}
    edges:
    - {source: '1', target: '2'}
    - {source: '2', sourceHandle: 'true', target: '3'}
    - {source: '2', sourceHandle: 'false', target: '9'}
    - {source: '9', sourceHandle: fail-branch, target: '3'}
"""
    )

    document, warnings = convert_dify(raw)

    by_id = {node.id: node for node in document.graph.nodes}
    assert by_id["n1"].config["fields"][1]["type"] == "string"
    assert by_id["n2"].config == {
        "cases": [
            {
                "id": "true",
                "logical": "or",
                "conditions": [
                    {"var": "n1.q", "op": "starts_with", "value": "안녕"},
                    {"var": "n1.q", "op": "all of", "value": ["a"]},
                    {"var": "n1.q", "op": "is_empty"},
                ],
            }
        ]
    }
    assert by_id["n3"].type == "variable_aggregator"
    assert by_id["n3"].config["vars"] == ["n1.q", "n5.json"]
    llm = by_id["n5"].config
    assert llm["json_mode"] is True and llm["output_schema"]["type"] == "object"
    assert llm["prompt"] == [
        {
            "role": "user",
            "text": "{% set question = n1.q %}{% set who = sys.user %}{{ question }} ({{ who }})",
        }
    ]
    http = by_id["n6"].config
    assert http["method"] == "POST" and http["url"] == "https://example.com/{{ n1.q }}"
    assert http["headers"] == [{"name": "X-Trace", "value": "{{ sys.run_id }}"}]
    assert http["body"] == {"type": "form", "content": [{"name": "name", "value": "{{ n1.q }}"}]}
    assert http["auth"] == {
        "type": "header",
        "header": "Authorization",
        "value": "Basic dXNlcjpwYXNz",
    }
    assert by_id["n7"].config["body"] == {"type": "raw", "content": "raw {{ n1.q }}"}
    assert by_id["n7"].config["auth"] == {"type": "header", "header": "X-Key", "value": "abc"}
    assert by_id["n8"].config == {
        "items": [{"target": "conv.memo", "op": "overwrite", "var": "n1.q"}]
    }
    assert by_id["n9"].type == "unsupported"
    edges = [(edge.source, edge.source_handle, edge.target) for edge in document.graph.edges]
    assert edges == [
        ("n1", "out", "n2"),
        ("n2", "case_true", "n3"),
        ("n2", "else", "n9"),
        ("n9", "fail", "n3"),
    ]
    codes = {warning.code for warning in warnings}
    assert {
        "start_field_type",
        "condition_unsupported",
        "http_form_data",
        "http_file_field",
        "http_ssl_verify",
        "unsupported_node",
    } <= codes
    issues = validate(document.graph, document.kind, env_names=[], conversation_vars={})
    # 옮기지 못한 조건 연산자는 원래 값 그대로 두어 검증 오류로 드러난다.
    assert ("invalid_config", "n2") in {(issue.code, issue.node_id) for issue in issues}


def test_uuid_case_and_class_ids_become_handle_names() -> None:
    raw: dict[str, Any] = {
        "app": {"mode": "workflow", "name": "핸들"},
        "workflow": {
            "graph": {
                "nodes": [
                    {"id": "a", "data": {"type": "start", "variables": []}},
                    {
                        "id": "b",
                        "data": {
                            "type": "if-else",
                            "cases": [
                                {
                                    "case_id": "8d5f2a10-1b2c-4d3e-9f40-112233445566",
                                    "logical_operator": "and",
                                    "conditions": [
                                        {
                                            "variable_selector": ["sys", "workflow_id"],
                                            "comparison_operator": "not empty",
                                        }
                                    ],
                                }
                            ],
                        },
                    },
                ],
                "edges": [
                    {"source": "a", "target": "b"},
                    {
                        "source": "b",
                        "sourceHandle": "8d5f2a10-1b2c-4d3e-9f40-112233445566",
                        "target": "a",
                    },
                ],
            }
        },
    }

    document, _ = convert_dify(raw)

    case = document.graph.nodes[1].config["cases"][0]
    assert case["id"] == "8d5f2a101b2c4d3e9f40112233445566"
    assert document.graph.edges[1].source_handle == f"case_{case['id']}"


@pytest.mark.parametrize("mode", ["chat", "agent-chat", "completion", None])
def test_only_workflow_and_advanced_chat_apps_come_in(mode: str | None) -> None:
    with pytest.raises(DslError) as caught:
        convert_dify({"app": {"mode": mode, "name": "x"}, "workflow": {"graph": {"nodes": []}}})

    assert caught.value.code == "unsupported_app_mode"


def test_an_app_without_nodes_is_refused() -> None:
    with pytest.raises(DslError) as caught:
        convert_dify({"app": {"mode": "workflow", "name": "x"}, "workflow": {}})

    assert caught.value.code == "invalid_dsl"
