# pyright: strict
"""tool 노드(2단계 설계 2.4). 내장 도구(app/workflow/tools) 하나를 부른다.

config는 도구 이름(tool)과 매개변수(params)다. 매개변수마다 값을 어떻게 얻는지 mode로 정한다:
template은 value가 템플릿 글(3.3), var는 value가 변수 선택자, const는 value를 그대로 쓴다. 값은
도구가 선언한 타입으로 맞추고(3.2의 변환 규칙), 비었으면 선언한 기본값을 쓴다. 출력은 도구마다
다르며 GET /node-types의 tools에 나온다. 승인 없이 돈다(9.1). 오류 처리는 default·branch까지 받는다.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Issue
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    TemplateField,
    VarRef,
)
from app.workflow.tools import get_tool, tool_names
from app.workflow.variables import VariableError, coerce

TIMEOUT_GRACE_SEC = 5
_TEMPLATE_MARKS = ("{{", "{%", "{#")


class ToolArg(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # template: value가 템플릿 글, var: value가 변수 선택자, const: value를 그대로 쓴다.
    mode: Literal["template", "var", "const"] = "template"
    value: Any = ""


class ToolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = ""
    params: dict[str, ToolArg] = Field(default_factory=dict[str, ToolArg])


def _empty(value: Any) -> bool:
    return value is None or value == ""


def _static(arg: ToolArg) -> bool:
    """저장 때 값을 알 수 있는 매개변수(고정값, 변수 없는 템플릿)."""
    if arg.mode == "const":
        return True
    return (
        arg.mode == "template"
        and isinstance(arg.value, str)
        and not any(mark in arg.value for mark in _TEMPLATE_MARKS)
    )


def _error(code: str, message: str, field: str) -> Issue:
    return Issue(level="error", code=code, message=message, field=field)


class ToolRunner(NodeRunner[ToolConfig]):
    type = "tool"
    Config = ToolConfig
    error_strategies = ALL_STRATEGIES
    dynamic_outputs = True

    def outputs(self, config: ToolConfig) -> list[OutputSpec]:
        tool = get_tool(config.tool)
        return list(tool.outputs) if tool is not None else []

    def var_refs(self, config: ToolConfig) -> list[VarRef]:
        tool = get_tool(config.tool)
        refs: list[VarRef] = []
        for name, arg in config.params.items():
            if arg.mode != "var" or not isinstance(arg.value, str) or not arg.value:
                continue
            param = tool.param(name) if tool is not None else None
            refs.append(
                VarRef(f"params.{name}.value", arg.value, param.type if param is not None else None)
            )
        return refs

    def template_fields(self, config: ToolConfig) -> list[TemplateField]:
        return [
            TemplateField(f"params.{name}.value", arg.value)
            for name, arg in config.params.items()
            if arg.mode == "template" and isinstance(arg.value, str)
        ]

    def validate(self, config: ToolConfig, graph: GraphView) -> list[Issue]:
        tool = get_tool(config.tool)
        if tool is None:
            known = ", ".join(tool_names())
            message = f"모르는 도구다: {config.tool or '(비어 있음)'}. 쓸 수 있는 도구: {known}"
            return [_error("unknown_tool", message, "tool")]
        issues: list[Issue] = []
        for name in config.params:
            if tool.param(name) is None:
                message = f"{tool.name} 도구에는 {name} 매개변수가 없다"
                issues.append(_error("unknown_param", message, f"params.{name}"))
        for param in tool.params:
            field = f"params.{param.name}.value"
            arg = config.params.get(param.name)
            if arg is None or _empty(arg.value):
                if param.required and param.default is None:
                    message = f"{param.label}({param.name})을(를) 정해야 한다"
                    issues.append(_error("param_required", message, field))
                continue
            if arg.mode != "const" and not isinstance(arg.value, str):
                message = f"{arg.mode} 매개변수의 value는 글이어야 한다"
                issues.append(_error("invalid_config", message, field))
                continue
            if not _static(arg):
                continue
            try:
                value = coerce(arg.value, param.type)
            except VariableError as exc:
                issues.append(_error("type_mismatch", exc.message, field))
                continue
            if param.max_length is not None and isinstance(value, str):
                if len(value) > param.max_length:
                    message = f"{param.max_length}자까지다"
                    issues.append(_error("invalid_config", message, field))
                    continue
            problem = tool.check(param.name, value)
            if problem is not None:
                issues.append(_error("invalid_config", problem, field))
        return issues

    def timeout_sec(self, config: ToolConfig) -> float | None:
        tool = get_tool(config.tool)
        return tool.timeout_sec + TIMEOUT_GRACE_SEC if tool is not None else None

    async def run(
        self, ctx: NodeContext, config: ToolConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        tool = get_tool(config.tool)
        if tool is None:
            raise NodeFailure("unknown_tool", f"모르는 도구다: {config.tool}")
        args: dict[str, Any] = {}
        for param in tool.params:
            arg = config.params.get(param.name)
            value: Any = None
            if arg is not None and not _empty(arg.value):
                if arg.mode == "template":
                    value = ctx.render(str(arg.value))
                elif arg.mode == "var":
                    value = ctx.resolve(str(arg.value))
                else:
                    value = arg.value
            # 빈 글은 기본값이 있을 때만 기본값으로 바꾼다. 앞 노드가 낸 빈 글도 값이다.
            if value is None or (value == "" and param.default is not None):
                value = param.default
            if value is None and param.required:
                raise NodeFailure("param_required", f"{param.label}({param.name}) 값이 비어 있다")
            try:
                value = coerce(value, param.type)
            except VariableError as exc:
                raise NodeFailure(exc.code, f"{param.name}: {exc.message}") from None
            if param.max_length is not None and isinstance(value, str):
                if len(value) > param.max_length:
                    message = f"{param.name}은(는) {param.max_length}자까지다"
                    raise NodeFailure("param_too_long", message)
            args[param.name] = value
        result = await tool.run(ctx, args)
        return NodeResult(
            status="succeeded",
            outputs=result.outputs,
            inputs={"tool": tool.name, "params": args},
            process={"tool": tool.name, **result.process},
        )
