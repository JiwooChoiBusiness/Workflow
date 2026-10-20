# pyright: strict
"""tool 노드의 내장 도구 계약(2단계 설계 2.4).

도구는 이름, 매개변수 선언, 출력 선언, 실행을 가진다. tool 노드가 매개변수를 템플릿·변수·고정값에서
읽어 선언 타입으로 맞춘 뒤 run()에 넘기고, run()이 돌려준 값을 그대로 노드 출력으로 낸다(엔진이 출력
선언 타입으로 한 번 더 맞춘다). 실패는 NodeFailure로 던지고 오류 정책·재시도는 엔진이 맡는다.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.schemas.workflow import VarType
from app.workflow.nodes.base import NodeContext, OutputSpec

_JSON_TYPES: dict[str, str] = {
    "string": "string",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
}


@dataclass(frozen=True, slots=True)
class ToolParam:
    name: str
    type: VarType
    label: str
    description: str = ""
    required: bool = False
    # 값이 비었을 때 쓴다. None이면 null이다.
    default: Any = None
    # 고를 수 있는 값. 비어 있으면 자유 입력이다.
    options: tuple[str, ...] = ()
    max_length: int | None = None


@dataclass
class ToolResult:
    outputs: dict[str, Any]
    # 노드 런 process 열에 남길 요약.
    process: dict[str, Any] = field(default_factory=dict[str, Any])


class Tool:
    name: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    params: ClassVar[tuple[ToolParam, ...]]
    outputs: ClassVar[tuple[OutputSpec, ...]]
    # 노드 시간 상한(초). 네트워크를 쓰는 도구만 길다.
    timeout_sec: ClassVar[float] = 10

    def param(self, name: str) -> ToolParam | None:
        return next((item for item in self.params if item.name == name), None)

    # 고정값(또는 변수 없는 템플릿)으로 준 매개변수를 저장·검증 때 미리 본다. 문제가 있으면 그 설명.
    def check(self, name: str, value: Any) -> str | None:
        return None

    async def run(self, ctx: NodeContext, args: Mapping[str, Any]) -> ToolResult:
        raise NotImplementedError


def params_schema(tool: Tool) -> dict[str, Any]:
    """매개변수 선언을 JSON Schema로. 폼이 입력 칸을 만들 때 쓴다(GET /node-types의 tools)."""
    properties: dict[str, Any] = {}
    for param in tool.params:
        prop: dict[str, Any] = {"title": param.label}
        if param.description:
            prop["description"] = param.description
        json_type = _JSON_TYPES.get(param.type)
        if json_type is not None:
            prop["type"] = json_type
        if param.default is not None:
            prop["default"] = param.default
        if param.options:
            prop["enum"] = list(param.options)
        if param.max_length is not None:
            prop["maxLength"] = param.max_length
        properties[param.name] = prop
    return {
        "type": "object",
        "properties": properties,
        "required": [param.name for param in tool.params if param.required],
        "additionalProperties": False,
    }
