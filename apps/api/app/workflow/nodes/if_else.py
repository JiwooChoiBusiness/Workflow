# pyright: strict
"""if_else 노드(설계 2.3). 케이스를 위에서부터 평가해 처음 맞는 case_<id> 핸들, 없으면 else가
활성이다.

변수 타입에 맞지 않는 연산자는 검증 오류다. 런타임에 null인 변수(건너뛴 노드 등)는 is_empty만 참이고
≠·not_contains·not_in은 참, 나머지는 거짓이다. 조건 평가(evaluate)와 연산자·값 검사는 loop의 종료
조건과 list_operator의 필터가 같이 쓴다.
"""

import re
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Issue, VarType
from app.workflow.nodes.base import (
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    VarRef,
    duplicate_names,
)
from app.workflow.variables import ARRAY_ITEM, parse_number

Op = Literal[
    "=",
    "≠",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "is_empty",
    "is_not_empty",
    ">",
    "<",
    "≥",
    "≤",
    "in",
    "not_in",
    "regex",
]
UNARY_OPS = frozenset({"is_empty", "is_not_empty"})
ORDER_OPS = frozenset({">", "<", "≥", "≤"})
LIST_OPS = frozenset({"in", "not_in"})
TEXT_OPS = frozenset({"contains", "not_contains", "starts_with", "ends_with"})
MAX_REGEX_PATTERN_CHARS = 512
MAX_REGEX_INPUT_BYTES = 1024 * 1024
ELSE_HANDLE = "else"

_OPS_BY_GROUP: dict[str, frozenset[str]] = {
    "string": frozenset(
        {"=", "≠", "contains", "not_contains", "starts_with", "ends_with", "in", "not_in", "regex"}
    )
    | UNARY_OPS,
    "number": frozenset({"=", "≠", "in", "not_in"}) | ORDER_OPS | UNARY_OPS,
    "boolean": frozenset({"=", "≠"}) | UNARY_OPS,
    "object": UNARY_OPS,
    "file": UNARY_OPS,
    "array": frozenset({"contains", "not_contains"}) | UNARY_OPS,
}


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    var: str
    op: Op
    value: Any = None


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_]{1,32}$")
    logical: Literal["and", "or"] = "and"
    conditions: list[Condition] = Field(min_length=1)


class IfElseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[Case] = Field(min_length=1)


def case_handle(case_id: str) -> str:
    return f"case_{case_id}"


def type_group(var_type: VarType | None) -> str | None:
    if var_type is None or var_type == "any":
        return None
    return "array" if var_type in ARRAY_ITEM else var_type


def operator_issue(op: str, var_type: VarType | None) -> str | None:
    """var_type 값에 쓸 수 없는 연산자면 그 설명."""
    group = type_group(var_type)
    if group is not None and op not in _OPS_BY_GROUP[group]:
        return f"{var_type} 변수에는 {op} 연산자를 쓸 수 없다"
    return None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _number(value: object) -> int | float | None:
    if _is_number(value):
        return cast(int | float, value)
    if isinstance(value, str):
        return parse_number(value)
    return None


def _boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        left, right = _boolean(actual), _boolean(expected)
        return left is not None and left == right
    if _is_number(actual) or _is_number(expected):
        left, right = _number(actual), _number(expected)
        return left is not None and right is not None and left == right
    return actual == expected


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str | list | dict) and len(cast(Any, value)) == 0)


def _ordered(actual: Any, expected: Any) -> tuple[int | float, int | float]:
    left, right = _number(actual), _number(expected)
    if left is None or right is None:
        raise NodeFailure("type_mismatch", f"숫자로 비교할 수 없다: {actual!r}, {expected!r}")
    return left, right


def evaluate(op: str, actual: Any, expected: Any) -> bool:
    if op == "is_empty":
        return _empty(actual)
    if op == "is_not_empty":
        return not _empty(actual)
    if actual is None:
        return op in ("not_contains", "not_in") or (op == "≠" and expected is not None)
    if op == "=":
        return _same(actual, expected)
    if op == "≠":
        return not _same(actual, expected)
    if op in ("contains", "not_contains"):
        if isinstance(actual, list):
            found = any(_same(item, expected) for item in cast(list[Any], actual))
        elif isinstance(actual, dict):
            found = str(expected) in cast(dict[str, Any], actual)
        else:
            found = str(expected) in str(actual)
        return found if op == "contains" else not found
    if op == "starts_with":
        return str(actual).startswith(str(expected))
    if op == "ends_with":
        return str(actual).endswith(str(expected))
    if op in ORDER_OPS:
        left, right = _ordered(actual, expected)
        return {">": left > right, "<": left < right, "≥": left >= right, "≤": left <= right}[op]
    if op in LIST_OPS:
        items = cast(list[Any], expected) if isinstance(expected, list) else [expected]
        found = any(_same(actual, item) for item in items)
        return found if op == "in" else not found
    if op == "regex":
        text = str(actual)
        if len(text.encode("utf-8")) > MAX_REGEX_INPUT_BYTES:
            raise NodeFailure("variable_limit", "regex 비교 대상이 1MB를 넘는다")
        try:
            return re.search(str(expected), text) is not None
        except re.error as exc:
            raise NodeFailure("invalid_config", f"정규식 오류: {exc}") from None
    raise NodeFailure("invalid_config", f"모르는 연산자: {op}")


def value_issue(op: str, value: Any, var_type: VarType | None) -> str | None:
    """연산자에 맞지 않는 비교 값이면 그 설명."""
    group = type_group(var_type)
    if op in UNARY_OPS:
        return None
    if op in LIST_OPS:
        return None if isinstance(value, list) else "in·not_in의 value는 목록이어야 한다"
    if op == "regex":
        if not isinstance(value, str):
            return "regex의 value는 문자열이어야 한다"
        if len(value) > MAX_REGEX_PATTERN_CHARS:
            return f"정규식은 {MAX_REGEX_PATTERN_CHARS}자까지다"
        try:
            re.compile(value)
        except re.error as exc:
            return f"정규식 오류: {exc}"
        return None
    if value is None and (op in ORDER_OPS or op in TEXT_OPS):
        return "비교할 value가 없다"
    if (op in ORDER_OPS or group == "number") and value is not None and _number(value) is None:
        return "숫자 비교의 value는 숫자여야 한다"
    return None


def condition_issues(condition: Condition, var_type: VarType | None, field: str) -> list[Issue]:
    """조건 하나의 연산자·비교 값 검사. field는 "cases[0].conditions[1]"처럼 그 조건의 위치다."""
    problem = operator_issue(condition.op, var_type)
    if problem is not None:
        return [Issue(level="error", code="invalid_operator", message=problem, field=f"{field}.op")]
    problem = value_issue(condition.op, condition.value, var_type)
    if problem is not None:
        return [
            Issue(level="error", code="invalid_config", message=problem, field=f"{field}.value")
        ]
    return []


class IfElseRunner(NodeRunner[IfElseConfig]):
    type = "if_else"
    Config = IfElseConfig
    branching = True

    def handles(self, config: IfElseConfig) -> list[str]:
        return [*(case_handle(case.id) for case in config.cases), ELSE_HANDLE]

    def var_refs(self, config: IfElseConfig) -> list[VarRef]:
        return [
            VarRef(f"cases[{ci}].conditions[{ki}].var", condition.var)
            for ci, case in enumerate(config.cases)
            for ki, condition in enumerate(case.conditions)
        ]

    def validate(self, config: IfElseConfig, graph: GraphView) -> list[Issue]:
        issues = duplicate_names((case.id for case in config.cases), "cases[{}].id")
        for ci, case in enumerate(config.cases):
            for ki, condition in enumerate(case.conditions):
                field = f"cases[{ci}].conditions[{ki}]"
                issues += condition_issues(condition, graph.output_type(condition.var), field)
        return issues

    async def run(
        self, ctx: NodeContext, config: IfElseConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        seen: dict[str, Any] = {}

        def check(condition: Condition) -> bool:
            if condition.var not in seen:
                seen[condition.var] = ctx.resolve(condition.var)
            return evaluate(condition.op, seen[condition.var], condition.value)

        for case in config.cases:
            results = (check(condition) for condition in case.conditions)
            if all(results) if case.logical == "and" else any(results):
                return NodeResult(
                    status="succeeded",
                    branch=case_handle(case.id),
                    inputs=seen,
                    process={"case": case.id},
                )
        return NodeResult(
            status="succeeded", branch=ELSE_HANDLE, inputs=seen, process={"case": None}
        )
