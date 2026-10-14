# pyright: strict
"""DSL 가져오기의 DB 없는 부분(2단계 설계 5.1, 9.3).

YAML 글을 읽어 형식을 가리고(우리 DSL·Dify DSL), 버전을 맞춘 DslDocument로 만든 뒤 가져온 그래프를
손질한다. 에이전트 slug로 id 찾기, 워크플로우·초안 쓰기, 검증은 app/services/workflow_dsl.py가 한다.

가져오는 파일은 신뢰하지 않는다. YAML은 safe 로더로 읽되 병합 키(<<)는 받지 않는다. 병합은 별칭을
겹쳐 노드 목록을 거듭제곱으로 불릴 수 있다. 별칭이 가리키는 값은 같은 객체를 공유하므로 읽기 자체는
가볍지만 펼쳐 보는 순간 커지므로, 평범한 값으로 옮길 때 값 개수와 깊이 상한으로 끊는다.
"""

import copy
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cache
from typing import Any, Literal, cast

from app.schemas.workflow import DslWarning, Graph, Node
from app.workflow.dsl.dify_import import convert_dify
from app.workflow.dsl.schema import (
    AGENT_SLUG_KEY,
    DSL_VERSION,
    DslDocument,
    DslError,
    validate_document,
)
from app.workflow.graph import AGENT_TYPE, UNSUPPORTED_TYPE
from app.workflow.nodes import default_registry
from app.workflow.nodes.base import NodeRegistry
from app.workflow.nodes.code import IMPORTED_CODE_FLAG

DslSource = Literal["dsl", "dify"]
# 2MB 파일에서 정상적인 그래프는 이 근처에도 가지 않는다. 넘으면 별칭으로 부풀린 문서로 본다.
MAX_YAML_VALUES = 200_000
MAX_YAML_DEPTH = 100
CODE_TYPE = "code"

DocumentUpgrade = Callable[[dict[str, Any]], dict[str, Any]]
NodeUpgrade = Callable[[dict[str, Any]], dict[str, Any]]
# dsl_version n의 문서(평범한 사전)를 n+1로 바꾸는 함수. 아직 형식을 바꾼 적이 없다.
DSL_UPGRADES: dict[int, DocumentUpgrade] = {}
# (노드 타입, 버전 n) → 그 버전의 config를 n+1 버전 config로 바꾸는 함수. 노드 config 형식을 바꾸면
# 레지스트리에 새 버전 러너를 올리고 여기에 변환기를 더한다. 아직 올린 노드가 없다.
NODE_UPGRADES: dict[tuple[str, int], NodeUpgrade] = {}


@dataclass
class AgentRef:
    """가져온 agent 노드가 가리키던 에이전트. 서비스가 id로 못 찾으면 slug로 찾는다."""

    node_id: str
    agent_id: uuid.UUID | None
    slug: str | None


@dataclass
class PreparedImport:
    document: DslDocument
    source: DslSource
    agents: list[AgentRef] = field(default_factory=list[AgentRef])
    unsupported: list[str] = field(default_factory=list[str])
    warnings: list[DslWarning] = field(default_factory=list[DslWarning])


@cache
def _loader() -> type[Any]:
    # PyYAML은 가져오기·내보내기 때만 불러온다(설계 8장 메모리 예산). yaml.safe_load와 같은 순수
    # 파이썬 SafeLoader라 깊은 중첩은 파이썬 재귀 상한(RecursionError)에서 끊긴다. C 로더(libyaml)는
    # 노드를 C 재귀로 만들어 그 상한을 거치지 않으므로 쓰지 않는다.
    import yaml
    from yaml.constructor import ConstructorError

    class Loader(yaml.SafeLoader):
        def flatten_mapping(self, node: yaml.MappingNode) -> None:
            for key, _value in node.value:
                if key.tag == "tag:yaml.org,2002:merge":
                    raise ConstructorError(None, None, "병합 키(<<)는 쓸 수 없다", key.start_mark)
            super().flatten_mapping(node)

    def equals_sign(loader: Loader, node: yaml.ScalarNode) -> str:
        # PyYAML은 따옴표 없는 =를 value 태그로 읽고 safe 로더에는 그 생성자가 없다. if_else
        # 연산자를 손으로 op: =처럼 쓴 파일도 받도록 글자로 읽는다.
        return loader.construct_scalar(node)

    Loader.add_constructor("tag:yaml.org,2002:value", equals_sign)
    return Loader


class _Walk:
    def __init__(self) -> None:
        self.count = 0


def _too_complex() -> DslError:
    return DslError(
        "invalid_yaml", "YAML 값이 너무 많거나 너무 깊다(별칭으로 부풀린 문서일 수 있다)"
    )


def _key(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int | float | date):
        return value.isoformat() if isinstance(value, date) else str(value)
    raise DslError("invalid_yaml", f"YAML 키로 쓸 수 없는 값이다: {type(value).__name__}")


def _plain(value: object, walk: _Walk, depth: int) -> Any:
    """YAML이 만든 값을 JSON으로 쓸 수 있는 값으로. 날짜는 ISO 글로 바꾼다."""
    walk.count += 1
    if walk.count > MAX_YAML_VALUES or depth > MAX_YAML_DEPTH:
        raise _too_complex()
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DslError("invalid_yaml", "숫자 값에 무한대나 NaN은 쓸 수 없다")
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        items = cast(dict[object, object], value).items()
        return {_key(key): _plain(item, walk, depth + 1) for key, item in items}
    if isinstance(value, list | tuple):
        return [_plain(item, walk, depth + 1) for item in cast(list[object], value)]
    raise DslError("invalid_yaml", f"DSL에 쓸 수 없는 YAML 값이다: {type(value).__name__}")


def parse_yaml(text: str) -> dict[str, Any]:
    """YAML 문서 하나를 평범한 사전으로. 형식 오류는 DslError(invalid_yaml)."""
    import yaml

    loader = _loader()(text)
    try:
        loaded: object = loader.get_single_data()
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        where = f"({mark.line + 1}행 {mark.column + 1}열)" if mark is not None else ""
        details = {"line": mark.line + 1, "column": mark.column + 1} if mark is not None else None
        problem = exc.problem or str(exc)
        message = f"YAML을 읽지 못했다{where}: {problem}"
        raise DslError("invalid_yaml", message, details=details) from None
    except yaml.YAMLError as exc:
        raise DslError("invalid_yaml", f"YAML을 읽지 못했다: {exc}") from None
    except RecursionError:
        raise _too_complex() from None
    finally:
        loader.dispose()
    if not isinstance(loaded, dict):
        raise DslError("invalid_yaml", "YAML 맨 위가 키: 값 묶음(매핑)이 아니다")
    try:
        return cast(dict[str, Any], _plain(cast(object, loaded), _Walk(), 0))
    except RecursionError:
        raise _too_complex() from None


def detect_format(raw: Mapping[str, Any]) -> DslSource:
    if "dsl_version" in raw:
        return "dsl"
    if isinstance(raw.get("app"), dict):
        return "dify"
    raise DslError(
        "unknown_dsl_format", "우리 DSL(dsl_version)도 Dify DSL(app, workflow)도 아닌 YAML이다"
    )


def upgrade_nodes(
    graph: Graph,
    registry: NodeRegistry,
    upgrades: Mapping[tuple[str, int], NodeUpgrade] = NODE_UPGRADES,
) -> tuple[Graph, list[DslWarning]]:
    """노드 버전을 레지스트리의 최신 버전으로 맞춘다.

    더 새 버전의 노드가 있으면 가져오기를 거부한다. 옛 버전인데 변환기가 없으면 그대로 두고 검증이
    알리게 한다(그 버전 러너가 남아 있으면 그대로 돈다). 모르는 타입도 그대로 둔다.
    """
    nodes: list[Node] = []
    warnings: list[DslWarning] = []
    too_new: list[dict[str, Any]] = []
    for node in graph.nodes:
        latest = registry.latest_version(node.type)
        if latest is None or node.version == latest:
            nodes.append(node)
            continue
        if node.version > latest:
            too_new.append(
                {"node_id": node.id, "type": node.type, "version": node.version, "latest": latest}
            )
            continue
        config = copy.deepcopy(node.config)
        version = node.version
        while version < latest and (step := upgrades.get((node.type, version))) is not None:
            config = step(config)
            version += 1
        if version != node.version:
            nodes.append(node.model_copy(update={"version": version, "config": config}))
            message = f"{node.type} 노드를 버전 {node.version}에서 {version}(으)로 바꿨다"
            warnings.append(DslWarning(code="node_upgraded", message=message, node_id=node.id))
        else:
            nodes.append(node)
    if too_new:
        raise DslError(
            "node_version_too_new",
            "이 서버보다 새 버전의 노드가 들어 있다. 서버를 올린 뒤 가져온다",
            details=too_new,
        )
    return graph.model_copy(update={"nodes": nodes}), warnings


def load_document(
    raw: Mapping[str, Any],
    *,
    registry: NodeRegistry | None = None,
    dsl_upgrades: Mapping[int, DocumentUpgrade] = DSL_UPGRADES,
    node_upgrades: Mapping[tuple[str, int], NodeUpgrade] = NODE_UPGRADES,
) -> tuple[DslDocument, list[DslWarning]]:
    """우리 DSL 사전을 지금 버전의 문서로. 더 새 dsl_version이면 거부한다."""
    version = raw.get("dsl_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise DslError("invalid_dsl", "dsl_version은 1 이상의 정수여야 한다")
    if version > DSL_VERSION:
        raise DslError(
            "unsupported_dsl_version",
            f"이 서버는 dsl_version {DSL_VERSION}까지 읽는다(파일은 {version}). "
            "서버를 올린 뒤 가져온다",
        )
    data = dict(raw)
    while version < DSL_VERSION:
        step = dsl_upgrades.get(version)
        if step is None:
            message = f"dsl_version {version}을(를) 바꾸는 변환기가 없다"
            raise DslError("unsupported_dsl_version", message)
        version += 1
        data = {**step(data), "dsl_version": version}
    document = validate_document(data)
    graph, warnings = upgrade_nodes(document.graph, registry or default_registry(), node_upgrades)
    return document.model_copy(update={"graph": graph}), warnings


def _uuid(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def prepare(document: DslDocument, source: DslSource, warnings: list[DslWarning]) -> PreparedImport:
    """가져온 그래프를 저장할 모양으로 손질한다(설계 5.1, 9.3).

    code 노드에는 ui.imported_code를 붙여 검토 전 실행·발행을 막고, agent 노드의 agent_slug는 떼어
    서비스가 id를 찾게 넘기며, 승인 모드 off는 inherit로 되돌린다.
    """
    out = list(warnings)
    nodes: list[Node] = []
    agents: list[AgentRef] = []
    code_nodes = 0
    for node in document.graph.nodes:
        if node.type == CODE_TYPE:
            code_nodes += 1
            node = node.model_copy(update={"ui": {**node.ui, IMPORTED_CODE_FLAG: True}})
        elif node.type == AGENT_TYPE:
            config = dict(node.config)
            slug = config.pop(AGENT_SLUG_KEY, None)
            agents.append(
                AgentRef(
                    node_id=node.id,
                    agent_id=_uuid(config.get("agent_id")),
                    slug=slug if isinstance(slug, str) and slug else None,
                )
            )
            node = node.model_copy(update={"config": config})
        nodes.append(node)
    if code_nodes:
        message = (
            f"code 노드 {code_nodes}개가 들어 있다. 노드마다 코드를 읽고 검토 완료를 눌러야 "
            "실행·발행할 수 있다"
        )
        out.append(DslWarning(code="imported_code", message=message))
    settings = document.settings
    if settings.approval_mode == "off":
        settings = settings.model_copy(update={"approval_mode": "inherit"})
        message = "승인 모드 off는 가져오지 않고 inherit로 두었다. 필요하면 설정에서 다시 끈다"
        out.append(DslWarning(code="approval_mode_reset", message=message))
    conversation = document.conversation_vars
    if document.kind == "workflow" and conversation:
        conversation = []
        message = "워크플로우에는 대화 변수가 없어 conversation_vars를 버렸다"
        out.append(DslWarning(code="conversation_vars_dropped", message=message))
    for item in document.env_vars:
        if item.value is None:
            what = "비밀 환경 변수" if item.secret else "환경 변수"
            message = f"{what} {item.name}의 값이 없다. 실행 전에 값을 넣는다"
            out.append(DslWarning(code="env_value_missing", message=message))
    graph = document.graph.model_copy(update={"nodes": nodes})
    return PreparedImport(
        document=document.model_copy(
            update={"graph": graph, "settings": settings, "conversation_vars": conversation}
        ),
        source=source,
        agents=agents,
        unsupported=[node.id for node in nodes if node.type == UNSUPPORTED_TYPE],
        warnings=out,
    )


def read_dsl(text: str, registry: NodeRegistry | None = None) -> PreparedImport:
    """YAML 글 → 저장할 준비가 된 문서. 거부할 문제는 DslError로 알린다."""
    raw = parse_yaml(text)
    source = detect_format(raw)
    if source == "dify":
        document, warnings = convert_dify(raw)
    else:
        document, warnings = load_document(raw, registry=registry)
    return prepare(document, source, warnings)
