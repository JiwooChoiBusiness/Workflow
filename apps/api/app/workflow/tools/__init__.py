# pyright: strict
"""tool 노드의 내장 도구 레지스트리(2단계 설계 2.4).

도구를 더하려면 Tool을 구현해 BUILTIN에 넣는다. 플러그인·MCP 도구는 범위 밖이다.
"""

from app.schemas.workflow import OutputSpecOut, ToolParamOut, ToolSpecOut
from app.workflow.tools.base import Tool, ToolParam, ToolResult, params_schema
from app.workflow.tools.current_time import CurrentTimeTool
from app.workflow.tools.json_parse import JsonParseTool
from app.workflow.tools.regex_extract import RegexExtractTool
from app.workflow.tools.web_fetch import WebFetchTool

__all__ = ["Tool", "ToolParam", "ToolResult", "describe_tools", "get_tool", "tool_names"]

BUILTIN: dict[str, Tool] = {
    tool.name: tool
    for tool in (CurrentTimeTool(), JsonParseTool(), RegexExtractTool(), WebFetchTool())
}


def get_tool(name: str) -> Tool | None:
    return BUILTIN.get(name)


def tool_names() -> list[str]:
    return list(BUILTIN)


def describe_tools() -> list[ToolSpecOut]:
    """GET /node-types의 tools. tool 노드 폼이 도구를 고르고 매개변수 칸을 만든다."""
    return [
        ToolSpecOut(
            name=tool.name,
            label=tool.label,
            description=tool.description,
            params=[
                ToolParamOut(
                    name=param.name,
                    type=param.type,
                    label=param.label,
                    description=param.description,
                    required=param.required,
                    default=param.default,
                    options=list(param.options) or None,
                    max_length=param.max_length,
                )
                for param in tool.params
            ],
            params_schema=params_schema(tool),
            outputs=[OutputSpecOut(name=spec.name, type=spec.type) for spec in tool.outputs],
        )
        for tool in BUILTIN.values()
    ]
