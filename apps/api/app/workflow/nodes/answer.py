# pyright: strict
"""answer 노드(설계 2.3, 3.8). 챗플로우 전용.

M11은 엔진 계약만 둔다. 노드가 ready가 되면 템플릿을 한 번에 렌더해 answer.delta로 내고, 엔진이
answer 노드들의 텍스트를 완료 순서로 이어 붙인다. 상류 llm 델타를 미리 흘리는 것(prestream)은
M17에서 이 노드 안에 더한다.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.workflow.nodes.base import (
    CHATFLOW_ONLY,
    NodeContext,
    NodeResult,
    NodeRunner,
    OutputSpec,
    TemplateField,
)


class AnswerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""


class AnswerRunner(NodeRunner[AnswerConfig]):
    type = "answer"
    Config = AnswerConfig
    kinds = CHATFLOW_ONLY
    fixed_outputs = (OutputSpec("text", "string"),)

    def template_fields(self, config: AnswerConfig) -> list[TemplateField]:
        return [TemplateField("text", config.text)]

    async def run(
        self, ctx: NodeContext, config: AnswerConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        text = ctx.render(config.text)
        if text:
            await ctx.emit("answer.delta", {"text": text})
        return NodeResult(status="succeeded", outputs={"text": text}, answer=text, inputs={})
