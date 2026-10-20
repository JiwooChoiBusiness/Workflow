# pyright: strict
"""human_input 노드(2단계 설계 2.4, 3.7). 폼을 띄우고 사람이 채워 보낼 때까지 런을 멈춘다.

처음 돌면 안내문을 렌더하고 폼 id를 만들어 waiting_input으로 끝난다(pending은 WorkflowFormData).
응답은 /input의 kind=form, values로 오고, 서비스가 필드 정의로 검사·변환(파일 칸은 올린 wf_file id를
파일 참조로)한 값을 wf_run.resume_payload에 담아 같은 런을 다시 넣는다. 엔진은 이 노드를 저장해 둔
state로 다시 돌리며 응답을 state["reply"]로 넘긴다. 값은 start 필드와 같은 규칙으로 한 번 더 맞춰
필드마다 출력한다.

응답 없이 WAITING_INPUT_TTL_HOURS가 지나면 서비스가 timeout 응답으로 다시 넣고 이 노드는
input_timeout으로 실패한다. 병렬 가지에서 다른 노드가 먼저 답을 받아 재개되면 이 노드는 같은 폼으로
다시 멈춘다(pending은 하나씩, 3.7).
"""

import uuid
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.events import WorkflowFormData, WorkflowFormField
from app.schemas.workflow import Issue
from app.workflow.nodes.base import (
    REPLY_STATE_KEY,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    TemplateField,
)
from app.workflow.nodes.start import (
    FIELD_TYPES,
    InputError,
    StartConfig,
    StartField,
    StartRunner,
    coerce_inputs,
)


class HumanInputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 폼 위에 보이는 안내문(마크다운). 템플릿(3.3)이라 앞 노드의 값을 넣어 보여 줄 수 있다.
    description: str = ""
    fields: list[StartField] = Field(min_length=1)


class _Waiting(BaseModel):
    """멈춘 동안의 state. 재개해도 같은 폼 id·안내문·시각으로 다시 묻는다."""

    form_id: str
    description: str = ""
    created_at: datetime


def form_of(config: HumanInputConfig, waiting: _Waiting) -> WorkflowFormData:
    return WorkflowFormData(
        form_id=waiting.form_id,
        description=waiting.description,
        fields=[WorkflowFormField.model_validate(field.model_dump()) for field in config.fields],
        created_at=waiting.created_at,
    )


class HumanInputRunner(NodeRunner[HumanInputConfig]):
    type = "human_input"
    Config = HumanInputConfig
    dynamic_outputs = True

    def outputs(self, config: HumanInputConfig) -> list[OutputSpec]:
        return [OutputSpec(field.name, FIELD_TYPES[field.type]) for field in config.fields]

    def template_fields(self, config: HumanInputConfig) -> list[TemplateField]:
        return [TemplateField("description", config.description)]

    def validate(self, config: HumanInputConfig, graph: GraphView) -> list[Issue]:
        # 필드 규칙(이름 중복, select 선택지, default 타입)은 start 노드와 같다.
        return StartRunner().validate(StartConfig(fields=config.fields), graph)

    async def run(
        self, ctx: NodeContext, config: HumanInputConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        saved = dict(state or {})
        reply = saved.pop(REPLY_STATE_KEY, None)
        if saved:
            try:
                waiting = _Waiting.model_validate(saved)
            except ValidationError:
                raise NodeFailure("invalid_state", "기다리던 폼을 다시 읽지 못했다") from None
        else:
            waiting = _Waiting(
                form_id=f"form_{uuid.uuid4().hex}",
                description=ctx.render(config.description),
                created_at=ctx.ports.now(),
            )
        shown = {"form_id": waiting.form_id, "description": waiting.description}
        answer = cast(dict[str, Any], reply) if isinstance(reply, dict) else None
        if answer is None or answer.get("form_id") != waiting.form_id:
            if reply is not None:
                ctx.warn("reply_mismatch: 다른 폼에 보낸 응답이라 다시 묻는다")
            return NodeResult(
                status="waiting_input",
                pending=form_of(config, waiting),
                state=waiting.model_dump(mode="json"),
                inputs=shown,
            )
        if answer.get("timeout") is True:
            raise NodeFailure("input_timeout", "정한 시간 안에 폼 응답이 없었다")
        raw = answer.get("values")
        given = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        try:
            values = coerce_inputs(config.fields, given)
        except InputError as exc:
            raise NodeFailure("invalid_inputs", f"폼 응답이 올바르지 않다: {exc}") from None
        return NodeResult(status="succeeded", outputs=values, inputs={**shown, "values": values})
