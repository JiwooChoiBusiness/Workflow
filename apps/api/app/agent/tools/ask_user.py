from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult


class AskUserTool(Tool):
    # 실행하지 않는다. 루프가 런을 waiting_input(question)으로 멈추고 답을 tool_result로
    # 넣는다(4.9).
    name = "ask_user"
    group = "ask_user"
    pause_kind = "question"
    description = (
        "사용자에게 질문하고 답을 기다린다. 요구가 모호하거나 되돌릴 수 없는 작업 전에 "
        "확인할 때 쓴다. 답은 이 도구의 결과로 돌아온다."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
                "description": "사용자에게 보여 줄 질문",
            },
            "options": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "description": "고를 수 있는 보기(선택)",
            },
            "allow_free_text": {
                "type": "boolean",
                "description": "보기 밖의 자유 입력을 받을지. 기본 true",
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        return ToolResult.error("ask_user는 실행하지 않고 사용자 답을 기다린다")
