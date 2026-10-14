"""DSL 내보내기→가져오기 라운드트립(2단계 설계 5.1, A9, M18).

픽스처를 가져와 내보내고 다시 가져오면 그래프가 같아야 한다. 화면에서 만든 그래프(가져온 적 없는
그래프)는 가져올 때 code 노드에 ui.imported_code가 붙는 것만 다르다.
"""

import uuid
from pathlib import Path
from typing import Any

import pytest

from app.schemas.workflow import Graph, WorkflowSettings
from app.workflow.dsl.export import build_document, document_data, dump_yaml
from app.workflow.dsl.import_ import parse_yaml, read_dsl
from app.workflow.dsl.schema import AGENT_SLUG_KEY, DslDocument, DslEnvVar

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "dsl"
ALL_FIXTURES = sorted(path.relative_to(FIXTURES).as_posix() for path in FIXTURES.rglob("*.y*ml"))
AGENT_ID = uuid.UUID("0192d2a4-7c1e-7b3a-9f00-5e1d2c3b4a59")


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def graph_data(document: DslDocument) -> dict[str, Any]:
    return document.graph.model_dump(mode="json")


def node_types(document: DslDocument) -> set[str]:
    return {node.type for node in document.graph.nodes}


def test_fixtures_cover_every_first_phase_node() -> None:
    assert {"ours/chatflow_all_nodes.yaml", "dify/workflow_search.yaml"} <= set(ALL_FIXTURES)
    chatflow = read_dsl(load("ours/chatflow_all_nodes.yaml")).document
    first_phase = {
        "start",
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
        "agent",
    }
    # end는 워크플로우 전용이라 챗플로우에 둘 수 없다. 다른 픽스처가 쓴다.
    assert first_phase | {"iteration_start"} == node_types(chatflow)
    assert "end" in node_types(read_dsl(load("ours/workflow_branches.yaml")).document)


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_export_then_import_gives_the_same_graph(name: str) -> None:
    first = read_dsl(load(name)).document

    text = dump_yaml(first)
    again = read_dsl(text).document

    assert graph_data(again) == graph_data(first)
    assert again.model_dump(exclude={"graph"}) == first.model_dump(exclude={"graph"})
    # 두 번째 내보내기는 첫 번째와 글자까지 같다.
    assert dump_yaml(again) == text


def test_a_graph_made_on_the_canvas_only_gains_the_code_review_flag() -> None:
    original = read_dsl(load("ours/chatflow_all_nodes.yaml")).document
    nodes = [
        node.model_copy(update={"ui": {}}) if node.type == "code" else node
        for node in original.graph.nodes
    ]
    canvas = original.graph.model_copy(update={"nodes": nodes})
    document = build_document(
        kind="chatflow",
        name="고객 문의 분류",
        icon="📨",
        description="",
        graph=canvas,
        env_vars=[],
        conversation_vars=original.conversation_vars,
        settings=original.settings,
        agent_slugs={AGENT_ID: "support-bot"},
    )

    prepared = read_dsl(dump_yaml(document))

    flagged = {node.id for node in prepared.document.graph.nodes if node.ui.get("imported_code")}
    assert flagged == {"parse"}
    restored = [
        node.model_copy(update={"ui": {}}) if node.id == "parse" else node
        for node in prepared.document.graph.nodes
    ]
    assert Graph(nodes=restored, edges=prepared.document.graph.edges, viewport=canvas.viewport) == (
        canvas
    )
    [agent] = prepared.agents
    assert (agent.node_id, agent.agent_id, agent.slug) == ("escalate", AGENT_ID, "support-bot")


def test_agent_slug_is_written_only_for_agents_that_exist() -> None:
    graph = Graph.model_validate(
        {
            "nodes": [
                {"id": "known", "type": "agent", "config": {"agent_id": str(AGENT_ID)}},
                {"id": "gone", "type": "agent", "config": {"agent_id": str(uuid.uuid4())}},
            ]
        }
    )
    document = build_document(
        kind="workflow",
        name="에이전트",
        icon=None,
        description="",
        graph=graph,
        env_vars=[],
        conversation_vars=[],
        settings=WorkflowSettings(),
        agent_slugs={AGENT_ID: "support-bot"},
    )

    configs = {node["id"]: node["config"] for node in document_data(document)["graph"]["nodes"]}
    assert configs["known"][AGENT_SLUG_KEY] == "support-bot"
    assert AGENT_SLUG_KEY not in configs["gone"]
    # 저장된 그래프는 건드리지 않는다.
    assert AGENT_SLUG_KEY not in graph.nodes[0].config


def test_yaml_keeps_korean_and_emoji_and_writes_code_as_a_block() -> None:
    text = dump_yaml(read_dsl(load("ours/chatflow_all_nodes.yaml")).document)

    assert "name: 고객 문의 분류\n" in text
    assert "icon: 📨\n" in text
    assert "무엇을 도와드릴까요? 😀" in text
    assert "\\u" not in text and "\\U" not in text
    assert "code: |\n" in text
    assert "        def main(body: str) -> dict:\n" in text
    # 별칭(&, *)을 쓰지 않는다.
    assert "&id" not in text and "*id" not in text


def test_text_that_cannot_be_a_block_is_quoted_and_survives() -> None:
    text = dump_yaml(read_dsl(load("ours/workflow_iteration.yaml")).document)

    assert 'code: "def main(topics: str) -> dict:\\n\\titems' in text
    split = next(node for node in read_dsl(text).document.graph.nodes if node.id == "split")
    assert split.config["code"].splitlines()[1].endswith("if t.strip()]  ")


TRICKY = [
    "",
    "yes",
    "no",
    "on",
    "null",
    "~",
    "true",
    "1e3",
    "0x1F",
    "010",
    "2024-01-01",
    "12:30",
    "=",
    "<<",
    " leading space",
    "trailing space ",
    "tab\tinside",
    "crlf\r\nline",
    "multi\nline\n",
    "multi\nline\n\n\n",
    "\n",
    "\n starts with break",
    "#not a comment",
    "key: value",
    "- dash",
    "'single' and \"double\"",
    "{{ start.q }} {% if x %}y{% endif %}",
    "emoji 👨‍💻 and 한글",
    "line separator",
    "nel\x85char",
    "bell\x07char",
    "backslash \\ end\\",
]


def test_tricky_strings_survive_the_round_trip() -> None:
    graph = Graph.model_validate(
        {
            "nodes": [
                {
                    "id": "t1",
                    "type": "template",
                    "title": "\t제목 ",
                    "config": {"template": value, "vars": []},
                    "ui": {"note": value},
                }
                for value in TRICKY
            ]
        }
    )
    # id가 겹쳐도 DSL은 그대로 옮긴다(검증이 알린다).
    document = DslDocument(
        dsl_version=1,
        kind="workflow",
        name="글자 시험",
        env_vars=[DslEnvVar(name="TRICKY", value="value: with colon # and hash")],
        conversation_vars=[],
        graph=graph,
    )

    again = read_dsl(dump_yaml(document)).document

    assert [node.config["template"] for node in again.graph.nodes] == TRICKY
    assert [node.ui["note"] for node in again.graph.nodes] == TRICKY
    assert {node.title for node in again.graph.nodes} == {"\t제목 "}
    assert again.env_vars[0].value == "value: with colon # and hash"


def test_numbers_and_nested_config_values_keep_their_types() -> None:
    config = {
        "a": 0,
        "b": 1.5,
        "c": 1e-7,
        "d": 10**18,
        "e": -0.0,
        "f": [True, False, None],
        "g": {"nested": {"deep": ["x", 1, {"k": "v"}]}},
        "h": "1.5",
    }
    graph = Graph.model_validate({"nodes": [{"id": "n", "type": "x", "config": config}]})
    document = DslDocument(dsl_version=1, kind="workflow", name="숫자", graph=graph)

    again = read_dsl(dump_yaml(document)).document

    assert again.graph.nodes[0].config == config
    assert type(again.graph.nodes[0].config["b"]) is float


def test_default_node_and_edge_fields_are_left_out() -> None:
    document = read_dsl(load("ours/workflow_branches.yaml")).document

    data = document_data(document)

    route = next(node for node in data["graph"]["nodes"] if node["id"] == "route")
    assert set(route) == {"id", "type", "version", "title", "position", "config"}
    edges = {edge["id"]: edge for edge in data["graph"]["edges"]}
    assert edges["e1"] == {"id": "e1", "source": "start", "target": "route"}
    assert edges["e2"]["source_handle"] == "case_adult"
    assert "viewport" not in data["graph"]


def test_secret_values_are_written_only_when_given() -> None:
    settings = WorkflowSettings()
    hidden = build_document(
        kind="workflow",
        name="비밀",
        icon=None,
        description="",
        graph=Graph(),
        env_vars=[
            DslEnvVar(name="PLAIN", value="보이는 값"),
            DslEnvVar(name="TOKEN", secret=True),
        ],
        conversation_vars=[],
        settings=settings,
        agent_slugs={},
    )
    shown = hidden.model_copy(
        update={
            "env_vars": [
                DslEnvVar(name="PLAIN", value="보이는 값"),
                DslEnvVar(name="TOKEN", value="s3cret-value", secret=True),
            ]
        }
    )

    assert parse_yaml(dump_yaml(hidden))["env_vars"] == [
        {"name": "PLAIN", "value": "보이는 값", "secret": False},
        {"name": "TOKEN", "secret": True},
    ]
    assert parse_yaml(dump_yaml(shown))["env_vars"][1] == {
        "name": "TOKEN",
        "value": "s3cret-value",
        "secret": True,
    }
    imported = read_dsl(dump_yaml(hidden))
    assert imported.document.env_vars[1] == DslEnvVar(name="TOKEN", secret=True)
    assert [warning.code for warning in imported.warnings] == ["env_value_missing"]
