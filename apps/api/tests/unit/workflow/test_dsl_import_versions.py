"""DSL 가져오기의 형식·버전 판정과 가져온 그래프 손질(2단계 설계 5.1, 9.3, M18).

DSL·노드 버전이 더 새면 거부하고 낮으면 변환기를 거친다. YAML은 safe 로더로 읽고 병합 키와
별칭으로 부풀린 문서, 깊은 중첩, 파이썬 태그를 거부한다. 가져온 code 노드는 검토 전까지 검증 오류,
승인 모드 off는 inherit, agent 노드의 agent_slug는 떼어 서비스에 넘긴다.
"""

import uuid
from typing import Any

import pytest

from app.schemas.workflow import Graph
from app.workflow.dsl import import_ as dsl_import
from app.workflow.dsl.export import dump_yaml
from app.workflow.dsl.import_ import (
    MAX_YAML_VALUES,
    detect_format,
    load_document,
    parse_yaml,
    read_dsl,
    upgrade_nodes,
)
from app.workflow.dsl.schema import DslDocument, DslError
from app.workflow.graph import validate
from app.workflow.nodes import default_registry
from app.workflow.nodes.template import TemplateRunner

AGENT_ID = "0192d2a4-7c1e-7b3a-9f00-5e1d2c3b4a59"


def document(**fields: Any) -> dict[str, Any]:
    nodes = [
        {"id": "start", "type": "start", "config": {"fields": []}},
        {
            "id": "say",
            "type": "template",
            "config": {"template": "안녕", "vars": []},
        },
        {"id": "end_1", "type": "end", "config": {"outputs": [{"name": "x", "var": "say.output"}]}},
    ]
    edges = [
        {"id": "e1", "source": "start", "target": "say"},
        {"id": "e2", "source": "say", "target": "end_1"},
    ]
    return {
        "dsl_version": 1,
        "kind": "workflow",
        "name": "시험",
        "graph": {"nodes": nodes, "edges": edges},
        **fields,
    }


def yaml_text(data: dict[str, Any]) -> str:
    return dump_yaml(DslDocument.model_validate(data))


def code_of(call: Any) -> str:
    with pytest.raises(DslError) as caught:
        call()
    return caught.value.code


def test_a_newer_dsl_version_is_refused() -> None:
    with pytest.raises(DslError) as caught:
        load_document(document(dsl_version=2))

    assert caught.value.code == "unsupported_dsl_version"
    assert "dsl_version 1까지" in caught.value.message


@pytest.mark.parametrize("version", [0, -1, "1", True, None, 1.5])
def test_dsl_version_must_be_a_positive_integer(version: object) -> None:
    assert code_of(lambda: load_document(document(dsl_version=version))) == "invalid_dsl"


def test_an_older_dsl_version_goes_through_the_upgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dsl_import, "DSL_VERSION", 2)

    def rename(data: dict[str, Any]) -> dict[str, Any]:
        upgraded = dict(data)
        upgraded["name"] = upgraded.pop("app_name")
        return upgraded

    raw = document()
    raw["app_name"] = raw.pop("name")

    loaded, _ = load_document(raw, dsl_upgrades={1: rename})

    assert loaded.name == "시험" and loaded.dsl_version == 2
    missing = load_document
    assert code_of(lambda: missing(document(), dsl_upgrades={})) == "unsupported_dsl_version"


class TemplateV2(TemplateRunner):
    version = 2


def test_newer_node_versions_are_refused() -> None:
    raw = document()
    raw["graph"]["nodes"][1]["version"] = 3

    with pytest.raises(DslError) as caught:
        load_document(raw)

    assert caught.value.code == "node_version_too_new"
    assert caught.value.details == [
        {"node_id": "say", "type": "template", "version": 3, "latest": 1}
    ]


def test_older_node_versions_go_through_the_converters() -> None:
    registry = default_registry().copy()
    registry.register(TemplateV2())
    raw = document()

    def to_v2(config: dict[str, Any]) -> dict[str, Any]:
        return {**config, "template": config["template"] + "하세요"}

    loaded, warnings = load_document(raw, registry=registry, node_upgrades={("template", 1): to_v2})

    say = next(node for node in loaded.graph.nodes if node.id == "say")
    assert (say.version, say.config["template"]) == (2, "안녕하세요")
    assert [(item.code, item.node_id) for item in warnings] == [("node_upgraded", "say")]
    # 원래 문서의 노드는 그대로다.
    assert raw["graph"]["nodes"][1]["config"]["template"] == "안녕"


def test_an_older_node_without_a_converter_stays_for_the_validator() -> None:
    registry = default_registry().copy()
    registry.register(TemplateV2())
    graph = Graph.model_validate(document()["graph"])

    upgraded, warnings = upgrade_nodes(graph, registry, {})

    assert upgraded == graph and warnings == []


def test_unknown_node_types_are_kept_and_reported_by_validation() -> None:
    raw = document()
    raw["graph"]["nodes"][1]["type"] = "future_node"

    loaded, _ = load_document(raw)

    issues = validate(loaded.graph, "workflow")
    assert ("unknown_node_type", "say") in {(issue.code, issue.node_id) for issue in issues}


def test_format_detection() -> None:
    assert detect_format({"dsl_version": 1}) == "dsl"
    assert detect_format({"app": {"mode": "workflow"}, "kind": "app"}) == "dify"
    assert code_of(lambda: detect_format({"name": "x"})) == "unknown_dsl_format"


def test_schema_problems_come_back_with_locations() -> None:
    raw = document()
    raw["graph"]["nodes"][1]["colour"] = "red"
    raw["kind"] = "agent"

    with pytest.raises(DslError) as caught:
        load_document(raw)

    assert caught.value.code == "invalid_dsl"
    locations = [tuple(item["loc"]) for item in caught.value.details]
    assert ("kind",) in locations
    assert ("graph", "nodes", 1, "colour") in locations


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("name: [unclosed\n", "행"),
        ("- just\n- a list\n", "매핑"),
        ("", "매핑"),
        ("a: 1\n---\nb: 2\n", "YAML"),
        ("a: !!python/object/apply:os.system ['echo hi']\n", "YAML"),
        ("a: !!binary aGVsbG8=\n", "bytes"),
        ("a: .nan\n", "NaN"),
    ],
)
def test_broken_or_unsafe_yaml_is_invalid(text: str, fragment: str) -> None:
    with pytest.raises(DslError) as caught:
        parse_yaml(text)

    assert caught.value.code == "invalid_yaml"
    assert fragment in caught.value.message


def test_syntax_errors_point_at_the_line() -> None:
    with pytest.raises(DslError) as caught:
        parse_yaml("dsl_version: 1\nname: 시험\ngraph: {nodes: [\n")

    assert caught.value.details["line"] >= 3


def test_merge_keys_are_refused() -> None:
    text = "base: &b {x: 1}\nchild:\n  <<: *b\n  y: 2\n"

    assert code_of(lambda: parse_yaml(text)) == "invalid_yaml"


def test_alias_bombs_are_cut_off() -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    for level in range(1, 8):
        refs = ", ".join([f"*a{level - 1}"] * 10)
        lines.append(f"a{level}: &a{level} [{refs}]")
    # 펼치면 값이 10**8개다. 읽기는 공유 객체라 가볍고 평범한 값으로 옮길 때 끊는다.
    assert 10**8 > MAX_YAML_VALUES

    with pytest.raises(DslError) as caught:
        parse_yaml("\n".join(lines) + "\n")

    assert caught.value.code == "invalid_yaml"
    assert "너무" in caught.value.message


def test_deep_nesting_is_refused() -> None:
    assert code_of(lambda: parse_yaml("a: " + "[" * 3000 + "]" * 3000 + "\n")) == "invalid_yaml"
    assert code_of(lambda: parse_yaml("a: " + "[" * 150 + "]" * 150 + "\n")) == "invalid_yaml"


def test_a_few_aliases_are_still_fine() -> None:
    loaded = parse_yaml("shared: &s {k: v}\none: *s\ntwo: *s\n")

    assert loaded == {"shared": {"k": "v"}, "one": {"k": "v"}, "two": {"k": "v"}}


def test_dates_become_text_and_bare_equals_reads_as_text() -> None:
    loaded = parse_yaml("when: 2024-01-02\nat: 2024-01-02T03:04:05Z\nop: =\n2: two\ntrue: yes\n")

    assert loaded == {
        "when": "2024-01-02",
        "at": "2024-01-02T03:04:05+00:00",
        "op": "=",
        "2": "two",
        "true": True,
    }


def test_imported_code_nodes_need_a_review() -> None:
    raw = document()
    raw["graph"]["nodes"][1] = {
        "id": "say",
        "type": "code",
        "config": {
            "code": "def main() -> dict:\n    return {'output': 'hi'}\n",
            "outputs": [{"name": "output", "type": "string"}],
        },
        "ui": {"collapsed": True},
    }

    prepared = read_dsl(yaml_text(raw))

    say = next(node for node in prepared.document.graph.nodes if node.id == "say")
    assert say.ui == {"collapsed": True, "imported_code": True}
    issues = validate(prepared.document.graph, "workflow")
    assert [(issue.code, issue.node_id, issue.field) for issue in issues] == [
        ("imported_code_unreviewed", "say", "code")
    ]
    assert [warning.code for warning in prepared.warnings] == ["imported_code"]
    reviewed = prepared.document.graph.model_copy(
        update={
            "nodes": [
                node.model_copy(update={"ui": {"collapsed": True}}) if node.id == "say" else node
                for node in prepared.document.graph.nodes
            ]
        }
    )
    assert validate(reviewed, "workflow") == []


def test_approval_mode_off_comes_in_as_inherit() -> None:
    prepared = read_dsl(yaml_text(document(settings={"approval_mode": "off"})))

    assert prepared.document.settings.approval_mode == "inherit"
    assert [warning.code for warning in prepared.warnings] == ["approval_mode_reset"]


def test_conversation_vars_are_dropped_for_workflows() -> None:
    raw = document(conversation_vars=[{"name": "topic", "type": "string"}])

    prepared = read_dsl(yaml_text(raw))

    assert prepared.document.conversation_vars == []
    assert [warning.code for warning in prepared.warnings] == ["conversation_vars_dropped"]


def test_agent_slug_is_taken_off_for_the_service() -> None:
    raw = document()
    raw["graph"]["nodes"][1] = {
        "id": "say",
        "type": "agent",
        "config": {"agent_id": AGENT_ID, "agent_slug": "helper", "instruction": "도와줘"},
    }
    raw["graph"]["nodes"][2]["config"]["outputs"][0]["var"] = "say.text"
    raw["graph"]["nodes"].append(
        {"id": "broken", "type": "agent", "config": {"agent_id": "not-an-id", "agent_slug": ""}}
    )
    raw["graph"]["edges"].append({"id": "e3", "source": "start", "target": "broken"})

    prepared = read_dsl(yaml_text(raw))

    assert [(ref.node_id, ref.agent_id, ref.slug) for ref in prepared.agents] == [
        ("say", uuid.UUID(AGENT_ID), "helper"),
        ("broken", None, None),
    ]
    say = next(node for node in prepared.document.graph.nodes if node.id == "say")
    assert "agent_slug" not in say.config
    graph = prepared.document.graph.model_copy(
        update={"nodes": [node for node in prepared.document.graph.nodes if node.id != "broken"]}
    )
    graph = graph.model_copy(update={"edges": [edge for edge in graph.edges if edge.id != "e3"]})
    missing = validate(graph, "workflow", agent_ids=set())
    assert [(issue.code, issue.node_id, issue.field) for issue in missing] == [
        ("agent_not_found", "say", "agent_id")
    ]
    assert validate(graph, "workflow", agent_ids={uuid.UUID(AGENT_ID)}) == []
    # 에이전트 목록을 모르는 곳(엔진)은 따지지 않는다.
    assert validate(graph, "workflow") == []


def test_unsupported_nodes_are_errors_but_references_to_them_are_not() -> None:
    raw = document()
    raw["graph"]["nodes"][1] = {
        "id": "say",
        "type": "unsupported",
        "title": "지식 검색",
        "config": {"source": "dify", "original_type": "knowledge-retrieval", "data": {}},
    }
    raw["graph"]["nodes"][2]["config"]["outputs"][0]["var"] = "say.result"

    prepared = read_dsl(yaml_text(raw))

    assert prepared.unsupported == ["say"]
    issues = validate(prepared.document.graph, "workflow")
    assert [(issue.code, issue.node_id) for issue in issues] == [("unsupported_node", "say")]
    assert "knowledge-retrieval" in issues[0].message
