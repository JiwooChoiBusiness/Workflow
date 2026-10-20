# pyright: strict
"""그래프 검증, 위상 정렬, 하위 그래프 추출(설계 3.1).

검증은 저장·실행·발행 때 모두 돈다. 오류가 하나라도 있으면 실행·발행을 거부하고 저장은 허용한다.
구조(노드 id, 엣지, 프레임, 도달성, 사이클)를 먼저 보고, 그다음 노드마다 config·변수 참조·템플릿을
본다. 변수 참조는 같은 프레임의 상류(경로가 있는 앞 노드)이거나, iteration·loop 하위 그래프에서 그
container의 상류를 가리켜야 한다. 아니면 unreachable_reference다. loop 하위 그래프는 자기 loop의
반복 변수("<loop id>.<이름>")도 읽는다.
"""

import re
import uuid
from collections import deque
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from app.schemas.workflow import (
    FAIL_HANDLE,
    IN_HANDLE,
    Edge,
    Graph,
    Issue,
    IssueLevel,
    Node,
    VarType,
    WorkflowKind,
)
from app.workflow.frames import (
    CONTAINER_ENTRIES,
    ENTRY_TYPES,
    LOOP_TYPE,
    START_TYPE,
    FrameGraph,
    build_frames,
)
from app.workflow.nodes import default_registry
from app.workflow.nodes.base import AnyRunner, NodeRegistry, TemplateField, VarRef
from app.workflow.render import TemplateError, template_names
from app.workflow.variables import (
    CONV,
    ENV,
    RESERVED_SCOPES,
    SCALAR_TYPES,
    SYS,
    Selector,
    SelectorError,
    assignable,
    parse_selector,
    sys_variables,
    type_matches,
)

MAX_NODES = 100
MAX_EDGES = 300
MAX_TEXT_BYTES = 64 * 1024
MAX_GRAPH_BYTES = 2 * 1024 * 1024
NODE_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
END_TYPE = "end"
ANSWER_TYPE = "answer"
AGENT_TYPE = "agent"
# DSL 가져오기가 옮기지 못한 노드(Dify의 2차·미지원 노드)의 자리. 레지스트리에 없어 팔레트에 나오지
# 않고, 엣지와 원래 설정을 남겨 둔 채 검증 오류로 실행·발행을 막는다.
UNSUPPORTED_TYPE = "unsupported"
_ENTRY_TYPES = ENTRY_TYPES
_ENTRY_OWNERS = {entry: owner for owner, entry in CONTAINER_ENTRIES.items()}


@dataclass
class Analysis:
    """검증 결과와 엔진이 바로 쓰는 색인. 오류가 있으면 엔진은 돌지 않는다."""

    graph: Graph
    kind: WorkflowKind
    issues: list[Issue]
    nodes: dict[str, Node]
    runners: dict[str, AnyRunner]
    configs: dict[str, BaseModel]
    # 노드 출력 이름 → 선언 타입. None이면 출력을 모른다(타입 미등록, config 오류).
    outputs: dict[str, dict[str, VarType] | None]
    frames: dict[str | None, FrameGraph] = field(default_factory=dict[str | None, FrameGraph])
    # container 노드가 도는 동안 하위 그래프에 보이는 값(loop의 반복 변수).
    inner_outputs: dict[str, dict[str, VarType]] = field(
        default_factory=dict[str, dict[str, VarType]]
    )

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _loc(parts: Sequence[int | str]) -> str:
    text = ""
    for part in parts:
        if isinstance(part, int):
            text += f"[{part}]"
        else:
            text += f".{part}" if text else part
    return text


def _long_texts(value: Any, path: str = "") -> Iterator[str]:
    if isinstance(value, str):
        if len(value) > MAX_TEXT_BYTES // 4 and len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            yield path
    elif isinstance(value, dict):
        for key, item in cast(dict[str, Any], value).items():
            yield from _long_texts(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(cast(list[Any], value)):
            yield from _long_texts(item, f"{path}[{index}]")


def _cyclic(nodes: Sequence[str], outgoing: Mapping[str, Sequence[Edge]]) -> set[str]:
    """순환에 든 노드(Tarjan SCC, 반복형). 자기 자신으로 가는 엣지도 순환이다."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: set[str] = set()
    for root in nodes:
        if root in index:
            continue
        index[root] = low[root] = len(index)
        stack.append(root)
        on_stack.add(root)
        work: list[tuple[str, Iterator[Edge]]] = [(root, iter(outgoing[root]))]
        while work:
            current, edges = work[-1]
            descended = False
            for edge in edges:
                target = edge.target
                if target not in index:
                    index[target] = low[target] = len(index)
                    stack.append(target)
                    on_stack.add(target)
                    work.append((target, iter(outgoing[target])))
                    descended = True
                    break
                if target in on_stack:
                    low[current] = min(low[current], index[target])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[current])
            if low[current] != index[current]:
                continue
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == current:
                    break
            if len(component) > 1 or any(e.target == current for e in outgoing[current]):
                result.update(component)
    return result


def _topological(frame: FrameGraph) -> list[str]:
    remaining = {node_id: len(frame.incoming[node_id]) for node_id in frame.nodes}
    queue = deque(node_id for node_id in frame.nodes if remaining[node_id] == 0)
    order: list[str] = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for edge in frame.outgoing[node_id]:
            remaining[edge.target] -= 1
            if remaining[edge.target] == 0:
                queue.append(edge.target)
    return order


class _Checker:
    def __init__(
        self,
        graph: Graph,
        kind: WorkflowKind,
        registry: NodeRegistry,
        env_names: Collection[str] | None,
        conversation_vars: Mapping[str, VarType] | None,
        agent_ids: Collection[uuid.UUID] | None = None,
    ) -> None:
        self.graph = graph
        self.kind: WorkflowKind = kind
        self.registry = registry
        self.env_names = frozenset(env_names) if env_names is not None else None
        self.conversation_vars = dict(conversation_vars) if conversation_vars is not None else None
        self.agent_ids = frozenset(agent_ids) if agent_ids is not None else None
        self.issues: list[Issue] = []
        self.nodes: dict[str, Node] = {}
        self.runners: dict[str, AnyRunner] = {}
        self.configs: dict[str, BaseModel] = {}
        self.outputs: dict[str, dict[str, VarType] | None] = {}
        self.inner_outputs: dict[str, dict[str, VarType]] = {}
        self.frames: dict[str | None, FrameGraph] = {}
        self._bit: dict[str, int] = {}
        self._ancestors: dict[str, int] = {}

    def add(
        self,
        level: IssueLevel,
        code: str,
        message: str,
        *,
        node_id: str | None = None,
        edge_id: str | None = None,
        field: str | None = None,
    ) -> None:
        self.issues.append(
            Issue(
                level=level,
                code=code,
                message=message,
                node_id=node_id,
                edge_id=edge_id,
                field=field,
            )
        )

    def run(self) -> Analysis:
        self._limits()
        self._nodes()
        self._kind_rules()
        self._frame_rules()
        self._structure(self._edges())
        self._node_checks()
        return Analysis(
            graph=self.graph,
            kind=self.kind,
            issues=self.issues,
            nodes=self.nodes,
            runners=self.runners,
            configs=self.configs,
            outputs=self.outputs,
            frames=self.frames,
            inner_outputs=self.inner_outputs,
        )

    def _limits(self) -> None:
        if len(self.graph.nodes) > MAX_NODES:
            message = f"노드는 {MAX_NODES}개까지다({len(self.graph.nodes)}개)"
            self.add("error", "too_many_nodes", message)
        if len(self.graph.edges) > MAX_EDGES:
            message = f"엣지는 {MAX_EDGES}개까지다({len(self.graph.edges)}개)"
            self.add("error", "too_many_edges", message)
        size = len(self.graph.model_dump_json().encode("utf-8"))
        if size > MAX_GRAPH_BYTES:
            self.add("error", "graph_too_large", f"그래프 JSON이 2MB를 넘는다({size}바이트)")

    def _nodes(self) -> None:
        # loop 안에서만 허용되는 노드(variable_assigner)를 보려고 부모의 타입을 먼저 안다.
        parent_types: dict[str, str] = {}
        for node in self.graph.nodes:
            parent_types.setdefault(node.id, node.type)
        for node in self.graph.nodes:
            if node.id in self.nodes:
                message = f"노드 id {node.id}이(가) 겹친다"
                self.add("error", "duplicate_node_id", message, node_id=node.id, field="id")
                continue
            self.nodes[node.id] = node
            self.outputs[node.id] = None
            if not NODE_ID.fullmatch(node.id) or node.id in RESERVED_SCOPES:
                message = "노드 id는 [a-z][a-z0-9_]{0,31}이고 sys·env·conv가 아니어야 한다"
                self.add("error", "invalid_node_id", message, node_id=node.id, field="id")
            runner = self.registry.get(node.type, node.version)
            if runner is None:
                if node.type == UNSUPPORTED_TYPE:
                    original = node.config.get("original_type")
                    what = f"{original} 노드" if isinstance(original, str) else "이 노드"
                    message = f"가져오지 못한 {what}다. 지원하는 노드로 바꿔 끼우거나 지운다"
                    self.add("error", "unsupported_node", message, node_id=node.id, field="type")
                elif self.registry.has_type(node.type):
                    message = f"{node.type} 노드의 버전 {node.version}을(를) 모른다"
                    self.add("error", "unsupported_node_version", message, node_id=node.id)
                else:
                    message = f"모르는 노드 타입이다: {node.type}"
                    self.add("error", "unknown_node_type", message, node_id=node.id, field="type")
                continue
            self.runners[node.id] = runner
            in_loop = node.parent_id is not None and parent_types.get(node.parent_id) == LOOP_TYPE
            if self.kind not in runner.kinds and not (in_loop and self.kind in runner.loop_kinds):
                message = f"{self.kind}에는 {node.type} 노드를 둘 수 없다"
                self.add("error", "node_not_allowed", message, node_id=node.id)
            if node.error.strategy not in runner.error_strategies:
                message = f"{node.type} 노드는 오류 처리 {node.error.strategy}를 쓸 수 없다"
                self.add(
                    "error",
                    "unsupported_error_strategy",
                    message,
                    node_id=node.id,
                    field="error.strategy",
                )
            for path in _long_texts(node.config):
                message = f"글자 필드는 {MAX_TEXT_BYTES // 1024}KB까지다"
                self.add("error", "text_too_long", message, node_id=node.id, field=path)
            try:
                config = runner.parse_config(node.config)
            except ValidationError as exc:
                for error in exc.errors():
                    message = f"config가 올바르지 않다: {error['msg']}"
                    location = _loc(error["loc"]) or None
                    self.add("error", "invalid_config", message, node_id=node.id, field=location)
                continue
            self.configs[node.id] = config
            self.outputs[node.id] = {spec.name: spec.type for spec in runner.outputs(config)}
            inner = runner.inner_outputs(config)
            if inner is not None:
                self.inner_outputs[node.id] = {spec.name: spec.type for spec in inner}

    def _kind_rules(self) -> None:
        starts = [node for node in self.nodes.values() if node.type == START_TYPE]
        if not starts:
            self.add("error", "start_missing", "start 노드가 없다")
        elif len(starts) > 1:
            for node in starts:
                message = f"start 노드는 하나여야 한다({len(starts)}개)"
                self.add("error", "start_duplicate", message, node_id=node.id)
        types = {node.type for node in self.nodes.values()}
        if self.kind == "workflow" and END_TYPE not in types:
            self.add("error", "end_missing", "워크플로우에는 end 노드가 하나 이상 있어야 한다")
        if self.kind == "chatflow" and ANSWER_TYPE not in types:
            self.add("error", "answer_missing", "챗플로우에는 answer 노드가 하나 이상 있어야 한다")

    def _frame_rules(self) -> None:
        for node in self.nodes.values():
            owner_type = _ENTRY_OWNERS.get(node.type)
            if node.parent_id is None:
                if owner_type is not None:
                    message = f"{node.type}는 {owner_type} 하위 그래프에만 둔다"
                    self.add("error", "invalid_parent", message, node_id=node.id, field="parent_id")
                continue
            parent = self.nodes.get(node.parent_id)
            if parent is None or parent.type not in CONTAINER_ENTRIES:
                message = f"parent_id {node.parent_id}은(는) iteration·loop 노드가 아니다"
                self.add("error", "invalid_parent", message, node_id=node.id, field="parent_id")
            elif owner_type is not None and owner_type != parent.type:
                message = f"{node.type}는 {owner_type} 하위 그래프에만 둔다"
                self.add("error", "invalid_parent", message, node_id=node.id, field="parent_id")
            where = parent.type if parent is not None else "iteration"
            if node.type in CONTAINER_ENTRIES:
                message = f"{where} 안에 {node.type}을(를) 둘 수 없다"
                self.add("error", "nested_iteration", message, node_id=node.id)
            elif node.type in (START_TYPE, END_TYPE):
                message = f"{where} 안에는 {node.type} 노드를 둘 수 없다"
                self.add("error", "node_not_allowed", message, node_id=node.id)
        for node in self.nodes.values():
            entry = CONTAINER_ENTRIES.get(node.type)
            if entry is None or node.parent_id is not None:
                continue
            count = sum(
                1
                for child in self.nodes.values()
                if child.parent_id == node.id and child.type == entry
            )
            if count != 1:
                message = f"{entry}가 {count}개다. 하위 그래프마다 하나여야 한다"
                self.add("error", f"{entry}_count", message, node_id=node.id)

    def _handles(self, node: Node) -> list[str] | None:
        runner, config = self.runners.get(node.id), self.configs.get(node.id)
        if runner is None or config is None:
            return None
        handles = runner.handles(config)
        return [*handles, FAIL_HANDLE] if node.error.strategy == "branch" else handles

    def _edges(self) -> list[Edge]:
        """엣지 규칙을 보고 구조 분석(도달성·사이클·상류)에 쓸 엣지를 돌려준다.

        핸들이 틀린 엣지도 사용자가 그린 연결이므로 구조 분석에는 넣는다. 없는 노드, 다른 프레임,
        start·iteration_start·loop_start로 들어오는 엣지만 뺀다.
        """
        structural: list[Edge] = []
        ids: set[str] = set()
        links: set[tuple[str, str, str]] = set()
        for edge in self.graph.edges:
            if edge.id in ids:
                self.add(
                    "error", "duplicate_edge_id", f"엣지 id {edge.id}이(가) 겹친다", edge_id=edge.id
                )
                continue
            ids.add(edge.id)
            source, target = self.nodes.get(edge.source), self.nodes.get(edge.target)
            if source is None or target is None:
                missing = edge.source if source is None else edge.target
                message = f"엣지가 없는 노드 {missing}을(를) 잇는다"
                self.add("error", "edge_unknown_node", message, edge_id=edge.id)
                continue
            if edge.target_handle != IN_HANDLE:
                message = f"들어오는 핸들은 {IN_HANDLE}뿐이다"
                self.add("error", "invalid_handle", message, edge_id=edge.id, field="target_handle")
            handles = self._handles(source)
            if handles is not None and edge.source_handle not in handles:
                if edge.source_handle == FAIL_HANDLE:
                    message = "fail 핸들은 error.strategy가 branch일 때만 쓴다"
                elif not handles:
                    message = f"{source.type} 노드에서는 엣지가 나갈 수 없다"
                else:
                    message = f"{source.id}에는 {edge.source_handle} 핸들이 없다"
                self.add(
                    "error",
                    "invalid_handle",
                    message,
                    node_id=source.id,
                    edge_id=edge.id,
                    field="source_handle",
                )
            link = (edge.source, edge.source_handle, edge.target)
            if link in links:
                message = "같은 핸들에서 같은 노드로 가는 엣지가 이미 있다"
                self.add("warning", "duplicate_edge", message, edge_id=edge.id)
            links.add(link)
            if target.type in _ENTRY_TYPES:
                message = f"{target.type} 노드로 들어오는 엣지는 둘 수 없다"
                self.add("error", "invalid_edge", message, edge_id=edge.id)
                continue
            if source.parent_id != target.parent_id:
                message = "엣지는 같은 하위 그래프(parent_id) 안의 노드만 잇는다"
                self.add("error", "cross_frame_edge", message, edge_id=edge.id)
                continue
            structural.append(edge)
        return structural

    def _structure(self, edges: list[Edge]) -> None:
        nodes = list(self.nodes.values())
        self._bit = {node.id: index for index, node in enumerate(nodes)}
        self.frames = build_frames(Graph(nodes=nodes, edges=edges))
        for frame_id, frame in self.frames.items():
            entry_type = START_TYPE
            if frame_id is not None:
                owner = self.nodes.get(frame_id)
                if owner is None or owner.type not in CONTAINER_ENTRIES:
                    continue
                entry_type = CONTAINER_ENTRIES[owner.type]
            if frame.entry is not None:
                reached = {frame.entry}
                queue = deque([frame.entry])
                while queue:
                    for edge in frame.outgoing[queue.popleft()]:
                        if edge.target not in reached:
                            reached.add(edge.target)
                            queue.append(edge.target)
                for node_id in frame.nodes:
                    if node_id not in reached and self.nodes[node_id].type not in _ENTRY_TYPES:
                        message = f"{entry_type}에서 닿지 않는 노드다"
                        self.add("error", "unreachable_node", message, node_id=node_id)
            cyclic = _cyclic(frame.nodes, frame.outgoing)
            for node_id in frame.nodes:
                if node_id in cyclic:
                    self.add("error", "cycle", "순환하는 엣지에 들어 있다", node_id=node_id)
            self._index_ancestors(frame, cyclic)

    def _index_ancestors(self, frame: FrameGraph, cyclic: set[str]) -> None:
        # 노드 100개·엣지 300개를 50ms 안에 보려고 상류 집합을 비트 연산으로 위상 순서대로 쌓는다.
        order = _topological(frame) if not cyclic else []
        if len(order) == len(frame.nodes):
            for node_id in order:
                mask = 0
                for edge in frame.incoming[node_id]:
                    mask |= self._ancestors[edge.source] | (1 << self._bit[edge.source])
                self._ancestors[node_id] = mask
            return
        for node_id in frame.nodes:
            mask = 0
            queue = deque(edge.source for edge in frame.incoming[node_id])
            while queue:
                source = queue.popleft()
                if mask >> self._bit[source] & 1:
                    continue
                mask |= 1 << self._bit[source]
                queue.extend(edge.source for edge in frame.incoming[source])
            self._ancestors[node_id] = mask

    def is_ancestor(self, ancestor: str, node_id: str) -> bool:
        return bool(self._ancestors.get(node_id, 0) >> self._bit[ancestor] & 1)

    def upstream(self, node: Node, target: Node) -> bool:
        if node.id == target.id:
            return False
        if node.parent_id == target.parent_id:
            return self.is_ancestor(target.id, node.id)
        if node.parent_id is not None and target.parent_id is None:
            return node.parent_id in self.nodes and self.is_ancestor(target.id, node.parent_id)
        return False

    def reference(
        self, node: Node, selector: Selector, field: str | None, *, inner: bool = False
    ) -> tuple[bool, VarType | None]:
        """참조가 맞으면 (True, 선언 타입 또는 모름=None). 틀리면 이슈를 남기고 (False, None).

        inner면 node의 하위 그래프 안에서 읽는 참조다(loop 종료 조건). 하위 노드 출력과 node 자신의
        반복 변수도 읽을 수 있다.
        """

        def fail(code: str, message: str) -> tuple[bool, VarType | None]:
            self.add("error", code, message, node_id=node.id, field=field)
            return False, None

        scope, name = selector.scope, selector.name
        base: VarType | None
        if scope == SYS:
            names = sys_variables(self.kind)
            if name not in names:
                return fail(
                    "unknown_variable", f"sys.{name}은(는) {self.kind}에 없는 시스템 변수다"
                )
            base = names[name]
        elif scope == ENV:
            if self.env_names is not None and name not in self.env_names:
                return fail("unknown_variable", f"환경 변수 {name}이(가) 없다")
            base = "string"
        elif scope == CONV:
            if self.kind != "chatflow":
                return fail("unknown_variable", "대화 변수(conv)는 챗플로우에만 있다")
            if self.conversation_vars is None:
                base = None
            elif name not in self.conversation_vars:
                return fail("unknown_variable", f"대화 변수 {name}이(가) 없다")
            else:
                base = self.conversation_vars[name]
        else:
            target = self.nodes.get(scope)
            if target is None:
                return fail("unknown_variable", f"노드 {scope}이(가) 없다")
            # 하위 그래프 안에서 자기 container를 읽으면 그 container가 도는 동안의 값(반복
            # 변수)이다.
            own = scope == node.parent_id or (inner and scope == node.id)
            variables = self.inner_outputs.get(scope) if own else None
            if own and target.type == LOOP_TYPE and self.outputs.get(scope) is None:
                # loop config를 읽지 못해 반복 변수를 모른다. 그 오류는 loop 노드에 이미 있다.
                return True, None
            if variables is not None:
                if name not in variables:
                    return fail("unknown_variable", f"{scope}에 반복 변수 {name}이(가) 없다")
                base = variables[name]
            else:
                below = inner and target.parent_id == node.id
                if not below and not self.upstream(node, target):
                    return fail(
                        "unreachable_reference", f"{scope}은(는) {node.id}의 상류 노드가 아니다"
                    )
                outputs = self.outputs.get(scope)
                if outputs is None:
                    return True, None
                if name not in outputs:
                    return fail("unknown_variable", f"{scope}에 {name} 출력이 없다")
                base = outputs[name]
        if selector.path:
            if base in SCALAR_TYPES:
                return fail("invalid_selector", f"{base} 값에는 하위 경로를 쓸 수 없다")
            return True, None
        return True, base

    def _check_ref(self, node: Node, ref: VarRef) -> None:
        try:
            selector = parse_selector(ref.selector)
        except SelectorError as exc:
            self.add("error", "invalid_selector", str(exc), node_id=node.id, field=ref.field)
            return
        ok, source = self.reference(node, selector, ref.field, inner=ref.inner)
        if ok and assignable(source, ref.expect) == "no":
            message = f"{ref.expect}이(가) 필요한데 {selector}는 {source}다"
            self.add("warning", "type_mismatch", message, node_id=node.id, field=ref.field)

    def _check_template(self, node: Node, template: TemplateField) -> None:
        try:
            found = template_names(template.text)
        except TemplateError as exc:
            self.add("error", exc.code, exc.message, node_id=node.id, field=template.field)
            return
        if template.names is not None:
            for name in sorted(found.names - template.names):
                message = f"템플릿의 {name}은(는) vars에 없는 이름이다"
                self.add(
                    "error", "unknown_variable", message, node_id=node.id, field=template.field
                )
            return
        for name in sorted(found.names):
            attrs = sorted(attr for owner, attr in found.attrs if owner == name)
            if name not in RESERVED_SCOPES and name not in self.nodes:
                message = f"템플릿의 {name}은(는) 노드도 sys·env·conv도 아니다"
                self.add(
                    "error", "unknown_variable", message, node_id=node.id, field=template.field
                )
            elif attrs:
                for attr in attrs:
                    self.reference(node, Selector(name, attr), template.field)
            elif (
                name not in RESERVED_SCOPES
                and not (name == node.parent_id and name in self.inner_outputs)
                and not self.upstream(node, self.nodes[name])
            ):
                message = f"{name}은(는) {node.id}의 상류 노드가 아니다"
                self.add(
                    "error", "unreachable_reference", message, node_id=node.id, field=template.field
                )

    def _check_defaults(self, node: Node) -> None:
        outputs = self.outputs.get(node.id)
        if outputs is None:
            return
        for name, value in node.error.default_outputs.items():
            field = f"error.default_outputs.{name}"
            declared = outputs.get(name)
            if declared is None:
                message = f"{node.id}에는 {name} 출력이 없다"
                self.add("error", "invalid_config", message, node_id=node.id, field=field)
            elif not type_matches(value, declared):
                message = f"기본값이 {declared} 타입이 아니다"
                self.add("warning", "type_mismatch", message, node_id=node.id, field=field)

    def _node_checks(self) -> None:
        for node in self.nodes.values():
            runner, config = self.runners.get(node.id), self.configs.get(node.id)
            if runner is None or config is None:
                continue
            for ref in runner.var_refs(config):
                self._check_ref(node, ref)
            for template in runner.template_fields(config):
                self._check_template(node, template)
            for issue in runner.validate(config, _View(self, node.id)):
                if issue.node_id is None:
                    issue = issue.model_copy(update={"node_id": node.id})
                self.issues.append(issue)
            if node.error.strategy == "default":
                self._check_defaults(node)


class _View:
    def __init__(self, checker: _Checker, node_id: str) -> None:
        self._checker = checker
        self._node_id = node_id

    @property
    def kind(self) -> WorkflowKind:
        return self._checker.kind

    @property
    def node_id(self) -> str:
        return self._node_id

    def node(self, node_id: str) -> Node | None:
        return self._checker.nodes.get(node_id)

    def outputs_of(self, node_id: str) -> Mapping[str, VarType] | None:
        return self._checker.outputs.get(node_id)

    def output_type(self, selector: str) -> VarType | None:
        try:
            parsed = parse_selector(selector)
        except SelectorError:
            return None
        if parsed.path:
            return None
        if parsed.scope == SYS:
            return sys_variables(self.kind).get(parsed.name)
        if parsed.scope == ENV:
            return "string"
        if parsed.scope == CONV:
            return (self._checker.conversation_vars or {}).get(parsed.name)
        outputs = self._checker.outputs.get(parsed.scope)
        return outputs.get(parsed.name) if outputs else None

    def inner_outputs(self, node_id: str) -> Mapping[str, VarType] | None:
        return self._checker.inner_outputs.get(node_id)

    def conversation_vars(self) -> Mapping[str, VarType] | None:
        return self._checker.conversation_vars

    def children(self, iteration_id: str) -> list[Node]:
        return [node for node in self._checker.nodes.values() if node.parent_id == iteration_id]

    def agent_known(self, agent_id: uuid.UUID) -> bool | None:
        known = self._checker.agent_ids
        return None if known is None else agent_id in known


def analyze(
    graph: Graph,
    kind: WorkflowKind,
    *,
    registry: NodeRegistry | None = None,
    env_names: Collection[str] | None = None,
    conversation_vars: Mapping[str, VarType] | None = None,
    agent_ids: Collection[uuid.UUID] | None = None,
) -> Analysis:
    """env_names·conversation_vars·agent_ids가 None이면 그 이름·id는 따지지 않는다(모를 때).

    agent_ids는 그래프의 agent 노드가 가리키는 에이전트 중 워크스페이스에 살아 있는 것이다.
    """
    checker = _Checker(
        graph, kind, registry or default_registry(), env_names, conversation_vars, agent_ids
    )
    return checker.run()


def validate(
    graph: Graph,
    kind: WorkflowKind,
    *,
    registry: NodeRegistry | None = None,
    env_names: Collection[str] | None = None,
    conversation_vars: Mapping[str, VarType] | None = None,
    agent_ids: Collection[uuid.UUID] | None = None,
) -> list[Issue]:
    return analyze(
        graph,
        kind,
        registry=registry,
        env_names=env_names,
        conversation_vars=conversation_vars,
        agent_ids=agent_ids,
    ).issues


def agent_references(graph: Graph) -> set[uuid.UUID]:
    """agent 노드가 가리키는 에이전트 id. id가 아닌 값은 config 검증이 알리므로 건너뛴다."""
    found: set[uuid.UUID] = set()
    for node in graph.nodes:
        if node.type != AGENT_TYPE:
            continue
        try:
            found.add(uuid.UUID(str(node.config.get("agent_id"))))
        except ValueError:
            continue
    return found


def topological_order(graph: Graph, iteration_id: str | None = None) -> list[str]:
    """프레임 하나의 위상 순서. 순환에 든 노드는 빠진다."""
    frame = build_frames(graph).get(iteration_id)
    return _topological(frame) if frame is not None else []


def subgraph(graph: Graph, iteration_id: str) -> Graph:
    """iteration·loop 하위 그래프(그 안의 노드와 엣지)만 뽑는다."""
    inner = {node.id for node in graph.nodes if node.parent_id == iteration_id}
    return Graph(
        nodes=[node for node in graph.nodes if node.id in inner],
        edges=[edge for edge in graph.edges if edge.source in inner and edge.target in inner],
    )
