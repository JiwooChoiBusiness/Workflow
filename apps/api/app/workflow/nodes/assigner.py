# pyright: strict
"""variable_assigner 노드(설계 2.3, 2.4).

대화 변수(conv.*)를 items 순서대로 바꾼다. 같은 대상을 두 번 쓰면 뒤 항목이 앞 항목의 결과를 본다.
새 값은 NodeResult.conv_updates로 돌려주고 엔진이 노드가 성공했을 때만 풀에 반영한다. 런이 끝나면
서비스가 wf_conversation.conversation_vars에 한 번에 쓴다(실패한 런은 쓰지 않는다).

loop 하위 그래프 안에서는 그 loop의 반복 변수("<loop id>.<이름>")도 바꾼다(loop_updates). 그래서
챗플로우 전용이지만 워크플로우의 loop 안에는 둘 수 있다(loop_kinds).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Issue, VarType
from app.workflow.frames import LOOP_TYPE
from app.workflow.nodes.base import (
    ALL_KINDS,
    CHATFLOW_ONLY,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    VarRef,
)
from app.workflow.variables import (
    ARRAY_ITEM,
    CONV,
    SelectorError,
    VariableError,
    assignable,
    coerce,
    describe,
    parse_selector,
)

AssignOp = Literal["overwrite", "append", "clear", "+=", "-="]
_LITERAL_TYPES: dict[str, VarType] = {
    "string": "string",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "file": "file",
    "array": "array[any]",
}


class AssignItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    op: AssignOp = "overwrite"
    # var(변수 선택자)와 value(고정값) 중 하나. clear는 둘 다 없다.
    var: str | None = None
    value: Any = None


class AssignerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AssignItem] = Field(min_length=1)


def cleared(declared: VarType | None) -> Any:
    if declared == "string":
        return ""
    if declared == "number":
        return 0
    if declared == "boolean":
        return False
    if declared == "object":
        return {}
    if declared in ARRAY_ITEM:
        return []
    return None


def _fit(value: Any, expected: VarType | None) -> Any:
    try:
        return coerce(value, expected)
    except VariableError as exc:
        raise NodeFailure(exc.code, exc.message) from None


def apply(op: AssignOp, current: Any, value: Any, declared: VarType | None) -> Any:
    if op == "clear":
        return cleared(declared)
    if op == "overwrite":
        return _fit(value, declared)
    if op == "append":
        items: Any = [] if current is None else current
        if not isinstance(items, list):
            raise NodeFailure("type_mismatch", "append 대상 대화 변수가 배열이 아니다")
        item_type = ARRAY_ITEM.get(declared) if declared is not None else None
        return [*items, _fit(value, item_type)]
    base = 0 if current is None else _fit(current, "number")
    amount = _fit(value, "number")
    if amount is None:
        raise NodeFailure("type_mismatch", f"{op}에 쓸 값이 없다")
    return base + amount if op == "+=" else base - amount


def _issue(
    code: str, message: str, field: str, level: Literal["error", "warning"] = "error"
) -> Issue:
    return Issue(level=level, code=code, message=message, field=field)


def _check_item(
    item: AssignItem,
    field: str,
    declared: VarType | None,
    graph: GraphView,
    what: str = "대화 변수",
) -> list[Issue]:
    """값 출처와 연산을 대상 변수의 선언 타입(모르면 None)에 비춰 본다."""
    issues: list[Issue] = []
    has_value = "value" in item.model_fields_set
    if item.op != "clear" and item.var is None and not has_value:
        message = "var와 value 중 하나가 있어야 한다"
        issues.append(_issue("invalid_config", message, field))
    elif item.var is not None and has_value:
        message = "var와 value를 함께 쓸 수 없다"
        issues.append(_issue("invalid_config", message, field))
    if declared is None:
        return issues
    if item.op == "append" and declared not in ARRAY_ITEM:
        message = f"append는 배열 {what}에만 쓴다({declared})"
        issues.append(_issue("invalid_operator", message, f"{field}.op"))
    elif item.op in ("+=", "-=") and declared != "number":
        message = f"{item.op}는 number {what}에만 쓴다({declared})"
        issues.append(_issue("invalid_operator", message, f"{field}.op"))
    elif item.op == "overwrite":
        source: VarType | None = None
        if item.var is not None:
            source = graph.output_type(item.var)
        elif has_value and item.value is not None:
            source = _LITERAL_TYPES.get(describe(item.value))
        if assignable(source, declared) == "no":
            message = f"{declared} {what}에 {source} 값을 넣는다"
            issues.append(_issue("type_mismatch", message, field, "warning"))
    return issues


class AssignerRunner(NodeRunner[AssignerConfig]):
    type = "variable_assigner"
    Config = AssignerConfig
    kinds = CHATFLOW_ONLY
    loop_kinds = ALL_KINDS
    writes_conversation = True

    def var_refs(self, config: AssignerConfig) -> list[VarRef]:
        return [
            VarRef(f"items[{i}].var", item.var)
            for i, item in enumerate(config.items)
            if item.var is not None
        ]

    def validate(self, config: AssignerConfig, graph: GraphView) -> list[Issue]:
        issues: list[Issue] = []
        node = graph.node(graph.node_id)
        loop_id = node.parent_id if node is not None else None
        parent = graph.node(loop_id) if loop_id is not None else None
        in_loop = parent is not None and parent.type == LOOP_TYPE
        # loop config를 읽지 못했으면 None이다. 그 오류는 loop 노드에 있어 여기서는 따지지 않는다.
        loop_vars = graph.inner_outputs(loop_id) if in_loop and loop_id is not None else None
        conversation = graph.conversation_vars()
        for i, item in enumerate(config.items):
            field = f"items[{i}]"
            try:
                target = parse_selector(item.target)
            except SelectorError as exc:
                issues.append(_issue("invalid_selector", str(exc), f"{field}.target"))
                continue
            if in_loop and target.scope == loop_id and not target.path:
                declared = loop_vars.get(target.name) if loop_vars is not None else None
                issues += _check_item(item, field, declared, graph, "반복 변수")
                if loop_vars is not None and target.name not in loop_vars:
                    message = f"loop {loop_id}에 반복 변수 {target.name}이(가) 없다"
                    issues.append(_issue("unknown_variable", message, f"{field}.target"))
                continue
            if target.scope != CONV or target.path:
                message = (
                    "target은 conv.<이름>이나 이 노드를 품은 loop의 반복 변수여야 한다"
                    if in_loop
                    else "target은 conv.<이름> 꼴이어야 한다"
                )
                issues.append(_issue("invalid_config", message, f"{field}.target"))
                continue
            if graph.kind != "chatflow" and in_loop:
                # loop 밖이면 노드 자체가 node_not_allowed라 따로 알리지 않는다.
                message = "대화 변수(conv)는 챗플로우에만 있다"
                issues.append(_issue("unknown_variable", message, f"{field}.target"))
                continue
            if conversation is None:
                issues += _check_item(item, field, None, graph)
                continue
            declared = conversation.get(target.name)
            if declared is None:
                issues += _check_item(item, field, None, graph)
                message = f"대화 변수 {target.name}이(가) 없다"
                issues.append(_issue("unknown_variable", message, f"{field}.target"))
                continue
            issues += _check_item(item, field, declared, graph)
        return issues

    async def run(
        self, ctx: NodeContext, config: AssignerConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        updates: dict[str, Any] = {}
        loop_updates: dict[str, Any] = {}
        applied: list[dict[str, Any]] = []
        for item in config.items:
            target = parse_selector(item.target)
            changed = updates if target.scope == CONV else loop_updates
            declared = ctx.pool.lookup(target).type
            current = changed[target.name] if target.name in changed else ctx.resolve(item.target)
            value = ctx.resolve(item.var) if item.var is not None else item.value
            changed[target.name] = apply(item.op, current, value, declared)
            applied.append({"target": item.target, "op": item.op, "value": value})
        return NodeResult(
            status="succeeded",
            conv_updates=updates,
            loop_updates=loop_updates,
            inputs={"items": applied},
        )
