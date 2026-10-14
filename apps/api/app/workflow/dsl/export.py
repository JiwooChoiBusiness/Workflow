# pyright: strict
"""DSL 내보내기(2단계 설계 5.1).

버전 행을 DslDocument로 바꾸고 YAML 글로 쓴다. 비밀 환경 변수의 복호화와 에이전트 slug 조회는
서비스가 해서 넘긴다. YAML은 사람이 읽고 고칠 수 있게 블록 형식으로 쓰고, 여러 줄 글
(코드·프롬프트)은 | 블록으로, 한글·이모지는 그대로 둔다. 기본값인 노드·엣지 필드는 빼도
가져올 때 같은 값으로 채워진다.
"""

import uuid
from collections.abc import Mapping, Sequence
from functools import cache
from typing import Any

from app.schemas.workflow import (
    IN_HANDLE,
    OUT_HANDLE,
    ConversationVar,
    Edge,
    ErrorPolicy,
    Graph,
    Node,
    RetryPolicy,
    WorkflowKind,
    WorkflowSettings,
)
from app.workflow.dsl.schema import AGENT_SLUG_KEY, DSL_VERSION, DslDocument, DslEnvVar
from app.workflow.graph import AGENT_TYPE


def _agent_id(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def build_document(
    *,
    kind: WorkflowKind,
    name: str,
    icon: str | None,
    description: str,
    graph: Graph,
    env_vars: Sequence[DslEnvVar],
    conversation_vars: Sequence[ConversationVar],
    settings: WorkflowSettings,
    agent_slugs: Mapping[uuid.UUID, str],
) -> DslDocument:
    """agent_slugs는 그래프의 agent 노드가 가리키는 살아 있는 에이전트의 id → slug다."""
    nodes: list[Node] = []
    for node in graph.nodes:
        agent_id = _agent_id(node.config.get("agent_id")) if node.type == AGENT_TYPE else None
        slug = agent_slugs.get(agent_id) if agent_id is not None else None
        if slug is not None:
            node = node.model_copy(update={"config": {**node.config, AGENT_SLUG_KEY: slug}})
        nodes.append(node)
    return DslDocument(
        dsl_version=DSL_VERSION,
        kind=kind,
        name=name,
        icon=icon,
        description=description,
        env_vars=list(env_vars),
        conversation_vars=list(conversation_vars),
        settings=settings,
        graph=graph.model_copy(update={"nodes": nodes}),
    )


def _node_data(node: Node) -> dict[str, Any]:
    data = node.model_dump(mode="json")
    out: dict[str, Any] = {
        "id": data["id"],
        "type": data["type"],
        "version": data["version"],
        "title": data["title"],
        "position": data["position"],
    }
    if node.parent_id is not None:
        out["parent_id"] = node.parent_id
    out["config"] = data["config"]
    if node.error != ErrorPolicy():
        out["error"] = data["error"]
    if node.retry != RetryPolicy():
        out["retry"] = data["retry"]
    if node.ui:
        out["ui"] = data["ui"]
    return out


def _edge_data(edge: Edge) -> dict[str, Any]:
    out: dict[str, Any] = {"id": edge.id, "source": edge.source}
    if edge.source_handle != OUT_HANDLE:
        out["source_handle"] = edge.source_handle
    out["target"] = edge.target
    if edge.target_handle != IN_HANDLE:
        out["target_handle"] = edge.target_handle
    return out


def document_data(document: DslDocument) -> dict[str, Any]:
    """YAML로 쓸 사전. 키 순서는 사람이 위에서부터 읽기 좋은 순서다."""
    out: dict[str, Any] = {
        "dsl_version": document.dsl_version,
        "kind": document.kind,
        "name": document.name,
    }
    if document.icon is not None:
        out["icon"] = document.icon
    out["description"] = document.description
    out["env_vars"] = [
        item.model_dump(mode="json", exclude_none=True, include={"name", "value", "secret"})
        for item in document.env_vars
    ]
    out["conversation_vars"] = [item.model_dump(mode="json") for item in document.conversation_vars]
    # 설정의 null은 모두 "정하지 않음"이라 빼도 가져올 때 같은 값이 된다.
    out["settings"] = document.settings.model_dump(mode="json", exclude_none=True)
    graph: dict[str, Any] = {
        "nodes": [_node_data(node) for node in document.graph.nodes],
        "edges": [_edge_data(edge) for edge in document.graph.edges],
    }
    if document.graph.viewport:
        graph["viewport"] = document.graph.viewport
    out["graph"] = graph
    return out


_UNICODE_BREAKS = ("\x85", "\u2028", "\u2029")


@cache
def _dumper() -> type[Any]:
    # PyYAML은 내보내기·가져오기 때만 불러온다(설계 8장 메모리 예산).
    import yaml

    class Dumper(yaml.SafeDumper):
        # 같은 객체가 두 번 나와도 &앵커·*별칭을 쓰지 않는다. 가져오기는 별칭을 의심한다.
        def ignore_aliases(self, data: Any) -> bool:
            return True

    def represent_str(dumper: Dumper, value: str) -> yaml.ScalarNode:
        style: str | None = None
        if any(char in value for char in _UNICODE_BREAKS):
            # YAML은 이 글자들을 줄바꿈으로 읽어 공백으로 접는다. 큰따옴표 안에서는 이스케이프된다.
            style = '"'
        elif "\n" in value:
            # 블록으로 쓸 수 없는 글(줄 끝 공백, 제어 문자)은 에미터가 큰따옴표로 바꿔 쓴다.
            style = "|"
        if style is None:
            return dumper.represent_str(value)
        return dumper.represent_scalar(  # pyright: ignore[reportUnknownMemberType]
            "tag:yaml.org,2002:str", value, style=style
        )

    Dumper.add_representer(str, represent_str)
    return Dumper


def dump_yaml(document: DslDocument) -> str:
    import yaml

    text: str = yaml.dump(
        document_data(document),
        Dumper=_dumper(),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=float("inf"),
    )
    return text
