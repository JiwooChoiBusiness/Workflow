"""call_agent: 다른 에이전트에게 과업 하나를 맡기는 내장 도구(오케스트레이션 2.1).

실행하지 않고 런을 멈춘다(pause_kind=agent_call). 루프가 LoopPorts.start_child로 자식 과업 런을
만들고 부모는 그 끝을 기다린다. 부를 수 있는 에이전트가 런마다 달라 입력 스키마(agent의 enum)와
설명을 인스턴스마다 만든다. 그래서 검증기를 도구 이름으로 캐시하지 않는다.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from app.agent.child_runs import (
    CALL_AGENT,
    MAX_CHILD_FILES,
    MAX_TASK_CHARS,
    ChildCall,
    ChildStartError,
    SubAgentChoice,
)
from app.agent.tools.base import Tool, ToolContext, ToolResult, model_schema, schema_input_validator
from app.llm.types import ToolSpec

if TYPE_CHECKING:
    from jsonschema.protocols import Validator

_GUIDE = (
    "다른 에이전트에게 과업 하나를 맡기고 결과를 받는다. 하위 에이전트는 자기 작업공간에서 "
    "처음부터 시작하고 이 대화를 보지 못하므로 task에 목적, 필요한 입력, 원하는 결과의 모양과 "
    "길이를 자족적으로 쓴다. 넘길 파일은 files에 이 작업공간의 상대 경로로 준다. 결과(글 또는 "
    "JSON)는 이 도구의 결과로 돌아오고 결과 파일은 agents/<에이전트>/<런 id>/ 아래에 복사된다. "
    "한 번에 하나씩 부른다."
)


def agent_line(agent: SubAgentChoice) -> str:
    """<available_agents>의 한 줄(2.1). 도구 설명과 시스템 프롬프트가 같은 줄을 쓴다."""
    result = "글"
    if agent.result.format == "json":
        result = (
            "JSON, 호출할 때 output_schema로 스키마를 준다" if agent.schema_from_call else "JSON"
        )
    description = agent.description.strip().replace("\n", " ")
    head = f"- {agent.slug}: {agent.name}"
    return f"{head} — {description} (결과: {result})" if description else f"{head} (결과: {result})"


def available_agents_block(agents: Sequence[SubAgentChoice]) -> str:
    return "\n".join(
        ["<available_agents>", *(agent_line(item) for item in agents), "</available_agents>"]
    )


def call_agent_schema(agents: Sequence[SubAgentChoice]) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "agent": {
            "type": "string",
            "enum": [item.slug for item in agents],
            "description": "맡길 에이전트(<available_agents>의 이름)",
        },
        "task": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_TASK_CHARS,
            "description": "과업 지시. 하위 에이전트가 이것만 보고 일할 수 있게 자족적으로 쓴다",
        },
        "files": {
            "type": "array",
            "maxItems": MAX_CHILD_FILES,
            "items": {"type": "string", "minLength": 1, "maxLength": 1000},
            "description": "함께 넘길 이 작업공간의 파일(상대 경로)",
        },
    }
    callers = [item.slug for item in agents if item.schema_from_call]
    if callers:
        # 빌더에서 "모델이 정함"으로 둔 json 항목이 있을 때만 둔다(2.2).
        properties["output_schema"] = {
            "type": "object",
            "description": (
                "결과 JSON Schema(type: object, additionalProperties: false). "
                f"이 에이전트에만 준다: {', '.join(callers)}"
            ),
        }
    return {
        "type": "object",
        "properties": properties,
        "required": ["agent", "task"],
        "additionalProperties": False,
    }


class CallAgentTool(Tool):
    name = CALL_AGENT
    group = "system"
    pause_kind = "agent_call"
    # 실제 설명과 스키마는 인스턴스마다 만든다(spec, input_validator).
    description = _GUIDE
    input_schema = {"type": "object"}

    def __init__(self, agents: Sequence[SubAgentChoice]) -> None:
        self.agents: dict[str, SubAgentChoice] = {item.slug: item for item in agents}
        self.schema = call_agent_schema(agents)
        self.full_description = f"{_GUIDE}\n\n{available_agents_block(agents)}"
        self._validator: Validator | None = None

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.full_description,
            input_schema=model_schema(self.schema),
        )

    def input_validator(self) -> "Validator":
        if self._validator is None:
            self._validator = schema_input_validator(self.schema)
        return self._validator

    def child_call(
        self, tool_call_id: str, message_id: uuid.UUID, data: Mapping[str, Any]
    ) -> ChildCall | ChildStartError:
        """검증을 통과한 입력을 ChildCall로. 고른 에이전트가 목록에 없으면 agent_not_allowed."""
        slug = str(data.get("agent", ""))
        agent = self.agents.get(slug)
        if agent is None:
            return ChildStartError(
                code="agent_not_allowed",
                message=f"부를 수 있는 에이전트가 아니다: {slug!r}. "
                f"쓸 수 있는 에이전트: {', '.join(sorted(self.agents))}",
            )
        raw_files = data.get("files")
        files = (
            [str(item) for item in cast(list[Any], raw_files)]
            if isinstance(raw_files, list)
            else []
        )
        raw_schema = data.get("output_schema")
        schema = cast(dict[str, Any], raw_schema) if isinstance(raw_schema, dict) else None
        return ChildCall(
            tool_call_id=tool_call_id,
            message_id=message_id,
            agent=agent,
            task=str(data.get("task", "")),
            files=list(dict.fromkeys(files)),
            output_schema=schema,
        )

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        return ToolResult.error("call_agent는 실행하지 않고 하위 에이전트의 끝을 기다린다")
