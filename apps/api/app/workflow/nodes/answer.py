# pyright: strict
"""answer 노드(설계 2.3, 3.8). 챗플로우 전용.

노드가 ready가 되면 템플릿 전체를 렌더해 answer.text로 내고, 엔진이 answer 노드들의 글을 완료
순서로 이어 붙인다. 상류 llm 델타를 미리 흘리는 일(prestream)은 노드가 ready가 되기 전에 일어나야
하므로 엔진이 이 노드의 answer_template으로 한다(app/workflow/answer_stream.py). 노드는 엔진이 이미
흘린 앞부분(ctx.streamed)을 뺀 나머지만 answer.delta로 낸다.
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

    def answer_template(self, config: AnswerConfig) -> str | None:
        return config.text

    async def run(
        self, ctx: NodeContext, config: AnswerConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        text = ctx.render(config.text)
        sent = ctx.streamed
        if text.startswith(sent):
            rest = text[len(sent) :]
        else:
            # 미리 흘린 글은 되돌릴 수 없다. 답 글(answer.text, 메시지)은 렌더한 전문으로 둔다.
            rest = ""
            ctx.warn("answer_stream_mismatch: 미리 흘린 글과 최종 글이 다르다")
        if rest:
            await ctx.emit("answer.delta", {"text": rest})
        process: dict[str, Any] = {"prestreamed_chars": len(sent)} if sent else {}
        return NodeResult(
            status="succeeded", outputs={"text": text}, answer=text, inputs={}, process=process
        )
