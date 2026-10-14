# pyright: strict
"""question_classifier 노드(설계 2.3, 11.2, 11.3).

질의를 모델에 보내 {"class_id": ...} JSON으로 분류를 받고 그 클래스의 class_<id> 핸들을 연다.
응답을 읽지 못하거나 모르는 id면 1회 다시 묻는다. 그래도 안 되면 on_parse_failure가
first_class(기본)일 때 첫 클래스로 보내고 classifier_fallback 경고를 남기며, fail이면 노드
오류다. Dify 문서가 이 경우를 적지 않아 설계가 정한 동작이다.

출력 이름은 Dify 문서를 따른다. class_name은 클래스 설명(비었으면 이름), class_label은 이름이다.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.llm.types import ChatRequest
from app.schemas.agent_version import ModelConfig
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
    duplicate_names,
)
from app.workflow.nodes.llm import (
    MODEL_TIMEOUT_SEC,
    ModelMeter,
    assistant_message,
    check_context,
    extract_json_object,
    model_params,
    prompt_text,
    stop_warnings,
    user_message,
)


class ClassItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_]{1,32}$")
    # 제목(class_label). 핸들과 캔버스 카드에 보인다.
    name: str = Field(min_length=1, max_length=200)
    # 분류 기준 설명(class_name). 모델이 이 글을 보고 고른다.
    description: str = Field(default="", max_length=2000)


class ClassifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ModelConfig = Field(default_factory=ModelConfig)
    query_var: str
    classes: list[ClassItem] = Field(min_length=1, max_length=50)
    # 템플릿(3.3). 분류 기준을 덧붙이는 지시.
    instruction: str = ""
    on_parse_failure: Literal["first_class", "fail"] = "first_class"


def class_handle(class_id: str) -> str:
    return f"class_{class_id}"


def _pick(text: str, ids: list[str]) -> tuple[str | None, str]:
    """(고른 id, 못 골랐을 때 이유)."""
    value = extract_json_object(text)
    if value is not None:
        chosen = value.get("class_id")
        if isinstance(chosen, str | int) and str(chosen) in ids:
            return str(chosen), ""
        return None, f"모르는 class_id: {str(chosen)[:80]}"
    bare = text.strip().strip("\"'`").strip()
    if bare in ids:
        return bare, ""
    return None, "JSON 객체가 없다"


class ClassifierRunner(NodeRunner[ClassifierConfig]):
    type = "question_classifier"
    Config = ClassifierConfig
    error_strategies = ALL_STRATEGIES
    branching = True
    fixed_outputs = (
        OutputSpec("class_name", "string"),
        OutputSpec("class_label", "string"),
        OutputSpec("class_id", "string"),
    )

    def handles(self, config: ClassifierConfig) -> list[str]:
        return [class_handle(item.id) for item in config.classes]

    def var_refs(self, config: ClassifierConfig) -> list[VarRef]:
        return [VarRef("query_var", config.query_var, "string")]

    def template_fields(self, config: ClassifierConfig) -> list[TemplateField]:
        return [TemplateField("instruction", config.instruction)]

    def validate(self, config: ClassifierConfig, graph: GraphView) -> list[Issue]:
        return duplicate_names((item.id for item in config.classes), "classes[{}].id")

    def timeout_sec(self, config: ClassifierConfig) -> float | None:
        return MODEL_TIMEOUT_SEC

    async def run(
        self, ctx: NodeContext, config: ClassifierConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        meter = ModelMeter(ctx)
        try:
            return await self._run(ctx, config, meter)
        except NodeFailure as exc:
            if not meter.calls:
                raise
            return meter.failed(exc, config.model.model_id)

    async def _run(
        self, ctx: NodeContext, config: ClassifierConfig, meter: ModelMeter
    ) -> NodeResult:
        query = ctx.resolve(config.query_var, "string")
        query_text = query if isinstance(query, str) else ""
        instruction = ctx.render(config.instruction) if config.instruction else ""
        classes = [item.model_dump() for item in config.classes]
        system = prompt_text("classifier", {"classes": classes, "instruction": instruction})
        messages = [user_message(query_text or "(빈 글)")]
        request = ChatRequest(
            model=config.model.model_id,
            system=system,
            messages=messages,
            params=model_params(config.model),
            metadata={"run_id": str(ctx.run_id)},
        )
        check_context(ctx, request)
        ids = [item.id for item in config.classes]
        reply = await meter.call(request)
        warnings = stop_warnings(reply)
        chosen, problem = _pick(reply.text, ids)
        if chosen is None:
            retry = request.model_copy(
                update={
                    "messages": [
                        *messages,
                        assistant_message(reply.text),
                        user_message(
                            f"앞 응답에서 분류를 읽지 못했다({problem}). 분류 id는 "
                            f"{', '.join(ids)} 중 하나다. "
                            '{"class_id": "<분류 id>"} JSON 객체 하나만 다시 출력하라.'
                        ),
                    ]
                }
            )
            reply = await meter.call(retry)
            warnings.extend(stop_warnings(reply))
            chosen, problem = _pick(reply.text, ids)
        fallback = chosen is None
        if chosen is None:
            if config.on_parse_failure == "fail":
                raise NodeFailure("classifier_parse_failed", f"분류를 읽지 못했다: {problem}")
            chosen = ids[0]
            warnings.append(f"classifier_fallback: {problem}. 첫 클래스 {chosen}(으)로 보냈다")
        item = next(entry for entry in config.classes if entry.id == chosen)
        return NodeResult(
            status="succeeded",
            outputs={
                "class_name": item.description or item.name,
                "class_label": item.name,
                "class_id": item.id,
            },
            branch=class_handle(item.id),
            usage=meter.total,
            inputs={"query": query_text, "instruction": instruction},
            process={**meter.process(config.model.model_id), "fallback": fallback},
            warnings=warnings,
        )
