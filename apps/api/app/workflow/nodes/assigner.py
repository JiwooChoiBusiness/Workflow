# pyright: strict
"""variable_assigner 노드(설계 2.3). 챗플로우 전용.

대화 변수(conv.*)를 items 순서대로 바꾼다. 같은 대상을 두 번 쓰면 뒤 항목이 앞 항목의 결과를 본다.
새 값은 NodeResult.conv_updates로 돌려주고 엔진이 노드가 성공했을 때만 풀에 반영한다. 런이 끝나면
서비스가 wf_conversation.conversation_vars에 한 번에 쓴다(실패한 런은 쓰지 않는다).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Issue, VarType
from app.workflow.nodes.base import (
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


class AssignerRunner(NodeRunner[AssignerConfig]):
    type = "variable_assigner"
    Config = AssignerConfig
    kinds = CHATFLOW_ONLY

    def var_refs(self, config: AssignerConfig) -> list[VarRef]:
        return [
            VarRef(f"items[{i}].var", item.var)
            for i, item in enumerate(config.items)
            if item.var is not None
        ]

    def validate(self, config: AssignerConfig, graph: GraphView) -> list[Issue]:
        issues: list[Issue] = []
        conversation = graph.conversation_vars()
        for i, item in enumerate(config.items):
            field = f"items[{i}]"
            try:
                target = parse_selector(item.target)
            except SelectorError as exc:
                issues.append(_issue("invalid_selector", str(exc), f"{field}.target"))
                continue
            if target.scope != CONV or target.path:
                message = "target은 conv.<이름> 꼴이어야 한다"
                issues.append(_issue("invalid_config", message, f"{field}.target"))
                continue
            has_value = "value" in item.model_fields_set
            if item.op != "clear" and item.var is None and not has_value:
                message = "var와 value 중 하나가 있어야 한다"
                issues.append(_issue("invalid_config", message, field))
            elif item.var is not None and has_value:
                message = "var와 value를 함께 쓸 수 없다"
                issues.append(_issue("invalid_config", message, field))
            if conversation is None:
                continue
            declared = conversation.get(target.name)
            if declared is None:
                message = f"대화 변수 {target.name}이(가) 없다"
                issues.append(_issue("unknown_variable", message, f"{field}.target"))
                continue
            if item.op == "append" and declared not in ARRAY_ITEM:
                message = f"append는 배열 대화 변수에만 쓴다({declared})"
                issues.append(_issue("invalid_operator", message, f"{field}.op"))
            elif item.op in ("+=", "-=") and declared != "number":
                message = f"{item.op}는 number 대화 변수에만 쓴다({declared})"
                issues.append(_issue("invalid_operator", message, f"{field}.op"))
            elif item.op == "overwrite":
                source: VarType | None = None
                if item.var is not None:
                    source = graph.output_type(item.var)
                elif has_value and item.value is not None:
                    source = _LITERAL_TYPES.get(describe(item.value))
                if assignable(source, declared) == "no":
                    message = f"{declared} 대화 변수에 {source} 값을 넣는다"
                    issues.append(_issue("type_mismatch", message, field, "warning"))
        return issues

    async def run(
        self, ctx: NodeContext, config: AssignerConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        updates: dict[str, Any] = {}
        applied: list[dict[str, Any]] = []
        for item in config.items:
            target = parse_selector(item.target)
            declared = ctx.pool.lookup(target).type
            current = updates[target.name] if target.name in updates else ctx.resolve(item.target)
            value = ctx.resolve(item.var) if item.var is not None else item.value
            updates[target.name] = apply(item.op, current, value, declared)
            applied.append({"target": item.target, "op": item.op, "value": value})
        return NodeResult(status="succeeded", conv_updates=updates, inputs={"items": applied})
