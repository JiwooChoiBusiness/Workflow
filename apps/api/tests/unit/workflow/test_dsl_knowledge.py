"""지식 노드의 DSL(3단계 설계 11장, M24).

우리 DSL은 kb_ids 옆에 kb_slugs(knowledge_index는 kb_slug)를 써서 다른 PC에서도 slug로 다시 잇는다.
id를 찾는 일은 서비스가 하고(tests/integration/test_knowledge_chatflow.py) 여기서는 내보낸 모양과
가져올 때 떼어 낸 참조를 본다. Dify knowledge-retrieval은 설정을 옮기고 KB는 비워 둔다.
"""

import uuid
from pathlib import Path
from typing import Any

from app.schemas.workflow import Graph, Node, WorkflowSettings
from app.workflow.dsl.export import build_document, dump_yaml
from app.workflow.dsl.import_ import KnowledgeRef, PreparedImport, read_dsl
from app.workflow.dsl.schema import KB_SLUG_KEY, KB_SLUGS_KEY
from app.workflow.graph import validate

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "dsl" / "dify"
KB_RULES = uuid.UUID("0196f000-0000-7000-8000-0000000000d1")
KB_GONE = uuid.UUID("0196f000-0000-7000-8000-0000000000d2")


def knowledge_graph() -> Graph:
    def node(node_id: str, node_type: str, config: dict[str, Any]) -> dict[str, Any]:
        return {"id": node_id, "type": node_type, "title": node_id, "config": config}

    return Graph.model_validate(
        {
            "nodes": [
                node("start", "start", {"fields": [{"name": "body", "type": "paragraph"}]}),
                node(
                    "knowledge_1",
                    "knowledge_retrieval",
                    {"kb_ids": [str(KB_RULES), str(KB_GONE)], "retrieval": {"top_k": 3}},
                ),
                node(
                    "index_1",
                    "knowledge_index",
                    {"kb_id": str(KB_RULES), "source": {"text_var": "start.body"}},
                ),
                node("index_2", "knowledge_index", {"source": {"text_var": "start.body"}}),
                node("end_1", "end", {"outputs": []}),
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "knowledge_1"},
                {"id": "e2", "source": "knowledge_1", "target": "index_1"},
                {"id": "e3", "source": "index_1", "target": "index_2"},
                {"id": "e4", "source": "index_2", "target": "end_1"},
            ],
        }
    )


def nodes_of(prepared: PreparedImport) -> dict[str, Node]:
    return {node.id: node for node in prepared.document.graph.nodes}


def exported() -> str:
    document = build_document(
        kind="workflow",
        name="지식 넣고 찾기",
        icon=None,
        description="",
        graph=knowledge_graph(),
        env_vars=[],
        conversation_vars=[],
        settings=WorkflowSettings(),
        agent_slugs={},
        kb_slugs={KB_RULES: "rules"},
    )
    configs = {node.id: node.config for node in document.graph.nodes}
    # 찾지 못한 KB 자리는 null이다.
    assert configs["knowledge_1"][KB_SLUGS_KEY] == ["rules", None]
    assert configs["index_1"][KB_SLUG_KEY] == "rules"
    assert KB_SLUG_KEY not in configs["index_2"]
    return dump_yaml(document)


def test_slugs_are_written_and_taken_off_on_import() -> None:
    text = exported()

    assert "kb_slugs:" in text and "kb_slug: rules" in text
    prepared = read_dsl(text)
    assert prepared.knowledge == [
        KnowledgeRef(
            node_id="knowledge_1", many=True, kb_ids=[KB_RULES, KB_GONE], slugs=["rules", None]
        ),
        KnowledgeRef(node_id="index_1", many=False, kb_ids=[KB_RULES], slugs=["rules"]),
    ]
    nodes = nodes_of(prepared)
    assert KB_SLUGS_KEY not in nodes["knowledge_1"].config
    assert KB_SLUG_KEY not in nodes["index_1"].config
    assert nodes["knowledge_1"].config["kb_ids"] == [str(KB_RULES), str(KB_GONE)]


def test_unreadable_ids_still_link_by_slug() -> None:
    text = exported().replace(str(KB_RULES), "not-a-uuid", 1)

    prepared = read_dsl(text)

    [first, _] = prepared.knowledge
    assert first.kb_ids == [None, KB_GONE] and first.slugs == ["rules", None]


def dify_imported() -> PreparedImport:
    return read_dsl((FIXTURES / "knowledge_chat.yaml").read_text(encoding="utf-8"))


def test_dify_multiple_retrieval_settings_are_carried_over() -> None:
    prepared = dify_imported()
    nodes = nodes_of(prepared)

    assert prepared.unsupported == []
    rules = nodes["n1740000000002"]
    assert rules.type == "knowledge_retrieval" and rules.title == "규정 검색"
    assert rules.ui == {
        "dify_dataset_ids": [
            "6a4f0000-0000-4000-8000-000000000001",
            "6a4f0000-0000-4000-8000-000000000002",
        ]
    }
    config = rules.config
    assert config["kb_ids"] == [] and config["query_var"] == "sys.query"
    assert config["retrieval"] == {
        "top_k": 6,
        "score_threshold_enabled": False,
        "reranking_mode": "llm",
        "weights": {"vector_weight": 0.6, "keyword_weight": 0.4},
    }
    # 우리 필드 이름 규칙에 맞지 않는 조건(Owner-Name)은 버리고 변수 참조는 {var}로 읽는다.
    assert config["metadata_filter"] == {
        "mode": "manual",
        "logical_operator": "and",
        "conditions": [
            {
                "name": "team",
                "comparison_operator": "is",
                "value": {"var": "n1740000000001.team"},
            },
            {"name": "year", "comparison_operator": "≥", "value": 2024},
        ],
    }


def test_dify_single_retrieval_and_automatic_filter() -> None:
    nodes = nodes_of(dify_imported())

    faq = nodes["n1740000000003"].config
    assert faq == {
        "kb_ids": [],
        "query_var": "sys.query",
        "metadata_filter": {"mode": "automatic"},
    }


def test_dify_llm_context_moves_to_our_context() -> None:
    prepared = dify_imported()
    llm = nodes_of(prepared)["n1740000000004"].config

    assert llm["context"] == {"var": "n1740000000002.result"}
    assert llm["prompt"][0] == {"role": "system", "text": "아래 자료로만 답한다.\n"}
    assert prepared.document.settings.webapp.citations_enabled is True


def test_dify_warnings_and_validation_ask_to_pick_knowledge_bases() -> None:
    prepared = dify_imported()

    warned = {(warning.code, warning.node_id) for warning in prepared.warnings}
    assert {
        ("knowledge_unlinked", "n1740000000002"),
        ("knowledge_unlinked", "n1740000000003"),
        ("rerank_model_replaced", "n1740000000002"),
        ("condition_unsupported", "n1740000000002"),
        ("single_retrieval_unsupported", "n1740000000003"),
        ("context_moved", "n1740000000004"),
    } <= warned
    document = prepared.document
    issues = validate(
        document.graph,
        document.kind,
        conversation_vars={},
        env_names=[],
    )
    assert sorted((issue.code, issue.node_id) for issue in issues if issue.level == "error") == [
        ("kb_required", "n1740000000002"),
        ("kb_required", "n1740000000003"),
    ]
