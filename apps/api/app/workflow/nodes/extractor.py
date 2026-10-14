# pyright: strict
"""parameter_extractor 노드(설계 2.3, 11.2).

파라미터 목록으로 JSON Schema를 만들고 질의에서 값을 뽑는다. reasoning_mode=tool_call은 강제 도구
호출(extract_parameters)로 스키마를 지키게 하고, 도구 호출이 없거나 값이 스키마에 맞지 않거나 모델이
요청을 거부(4xx)하면 prompt 모드로 1회 다시 한다. prompt 모드는 응답 글에서 JSON 객체를 찾는다.

값을 얻지 못해도 노드는 성공으로 끝나고 __is_success=false, __reason에 이유를 남긴다(Dify와 같은
동작). 타입이 맞지 않는 값은 null로 두고 같은 방식으로 알린다.
"""

import json
from collections.abc import Mapping
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.llm.types import ChatRequest, ForcedTool, ToolSpec
from app.schemas.agent_version import ModelConfig
from app.schemas.workflow import Issue, VarType
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    NAME_PATTERN,
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
    ModelReply,
    check_context,
    extract_json_object,
    model_params,
    prompt_text,
    stop_warnings,
    user_message,
)
from app.workflow.variables import ARRAY_ITEM, VariableError, coerce

TOOL_NAME = "extract_parameters"
SUCCESS = "__is_success"
REASON = "__reason"
ParamType = Literal[
    "string",
    "number",
    "boolean",
    "object",
    "array[string]",
    "array[number]",
    "array[boolean]",
    "array[object]",
]
_JSON_TYPES: dict[str, str] = {
    "string": "string",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
}


class ExtractParam(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    type: ParamType = "string"
    description: str = Field(default="", max_length=2000)
    required: bool = False
    # string·number만. 값이 이 목록에 없으면 실패로 본다.
    enum: list[str | int | float] | None = None

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value.startswith("__"):
            raise ValueError("__로 시작하는 이름은 __is_success·__reason이 쓴다")
        return value


class ExtractorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ModelConfig = Field(default_factory=ModelConfig)
    query_var: str
    parameters: list[ExtractParam] = Field(min_length=1, max_length=50)
    # 템플릿(3.3).
    instruction: str = ""
    reasoning_mode: Literal["tool_call", "prompt"] = "tool_call"


def _property(param: ExtractParam) -> dict[str, Any]:
    item = ARRAY_ITEM.get(param.type)
    schema: dict[str, Any]
    if item is not None:
        schema = {"type": "array", "items": {"type": _JSON_TYPES[item]}}
    else:
        schema = {"type": _JSON_TYPES[param.type]}
    description = param.description
    if param.enum:
        if param.type == "string":
            schema["enum"] = [str(value) for value in param.enum]
        else:
            # Gemini 함수 선언은 문자열에만 enum을 받는다. 숫자 목록은 설명에 적고 받은 뒤 검사한다.
            allowed = ", ".join(str(value) for value in param.enum)
            description = f"{description} (허용 값: {allowed})".strip()
    if description:
        schema["description"] = description
    return schema


def parameters_schema(config: ExtractorConfig) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {param.name: _property(param) for param in config.parameters},
        "required": [param.name for param in config.parameters if param.required],
    }


def check_values(
    config: ExtractorConfig, raw: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """선언 타입으로 맞춘 값과 실패 이유. 실패한 값은 null이다."""
    values: dict[str, Any] = {}
    problems: list[str] = []
    for param in config.parameters:
        value = raw.get(param.name)
        if value is None:
            values[param.name] = None
            if param.required:
                problems.append(f"필수 파라미터 {param.name}을(를) 찾지 못했다")
            continue
        declared: VarType = param.type
        try:
            value = coerce(value, declared)
        except VariableError as exc:
            values[param.name] = None
            problems.append(f"{param.name}: {exc.message}")
            continue
        if param.enum and value not in param.enum:
            values[param.name] = None
            problems.append(f"{param.name}: 허용 값이 아니다({str(value)[:80]})")
            continue
        values[param.name] = value
    return values, problems


def _tool_arguments(reply: ModelReply) -> dict[str, Any] | None:
    for call in reply.tool_calls:
        if call.name != TOOL_NAME:
            continue
        try:
            parsed: object = json.loads(call.arguments or "{}")
        except ValueError:
            return None
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
    return None


class ExtractorRunner(NodeRunner[ExtractorConfig]):
    type = "parameter_extractor"
    Config = ExtractorConfig
    error_strategies = ALL_STRATEGIES
    dynamic_outputs = True

    def outputs(self, config: ExtractorConfig) -> list[OutputSpec]:
        return [
            *(OutputSpec(param.name, param.type) for param in config.parameters),
            OutputSpec(SUCCESS, "boolean"),
            OutputSpec(REASON, "string"),
        ]

    def var_refs(self, config: ExtractorConfig) -> list[VarRef]:
        return [VarRef("query_var", config.query_var, "string")]

    def template_fields(self, config: ExtractorConfig) -> list[TemplateField]:
        return [TemplateField("instruction", config.instruction)]

    def validate(self, config: ExtractorConfig, graph: GraphView) -> list[Issue]:
        issues = duplicate_names((param.name for param in config.parameters), "parameters[{}].name")
        for index, param in enumerate(config.parameters):
            if param.enum and param.type not in ("string", "number"):
                issues.append(
                    Issue(
                        level="error",
                        code="invalid_config",
                        message="enum은 string·number 파라미터에만 쓴다",
                        field=f"parameters[{index}].enum",
                    )
                )
        return issues

    def timeout_sec(self, config: ExtractorConfig) -> float | None:
        return MODEL_TIMEOUT_SEC

    async def run(
        self, ctx: NodeContext, config: ExtractorConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        meter = ModelMeter(ctx)
        try:
            return await self._run(ctx, config, meter)
        except NodeFailure as exc:
            if not meter.calls:
                raise
            return meter.failed(exc, config.model.model_id)

    async def _run(
        self, ctx: NodeContext, config: ExtractorConfig, meter: ModelMeter
    ) -> NodeResult:
        query = ctx.resolve(config.query_var, "string")
        query_text = query if isinstance(query, str) else ""
        instruction = ctx.render(config.instruction) if config.instruction else ""
        schema = parameters_schema(config)
        context: dict[str, Any] = {
            "parameters": [param.model_dump() for param in config.parameters],
            "instruction": instruction,
            "schema": json.dumps(schema, ensure_ascii=False),
        }
        messages = [user_message(query_text or "(빈 글)")]
        params = model_params(config.model)
        base = ChatRequest(
            model=config.model.model_id,
            system="",
            messages=messages,
            params=params,
            metadata={"run_id": str(ctx.run_id)},
        )
        warnings: list[str] = []
        attempts: list[dict[str, Any]] = []
        # 문제가 가장 적은 결과. 두 모드 모두 JSON을 못 내면 None이다.
        best: tuple[dict[str, Any], list[str]] | None = None
        modes: list[str] = ["prompt"]
        if config.reasoning_mode == "tool_call":
            modes.insert(0, "tool_call")
        for mode in modes:
            system = prompt_text("extractor", {**context, "tool_mode": mode != "prompt"})
            request = base.model_copy(update={"system": system})
            if mode == "tool_call":
                request = request.model_copy(
                    update={
                        "tools": [
                            ToolSpec(
                                name=TOOL_NAME,
                                description="글에서 찾은 파라미터 값을 넘긴다",
                                input_schema=schema,
                            )
                        ],
                        "params": params.model_copy(
                            update={"tool_choice": ForcedTool(name=TOOL_NAME)}
                        ),
                    }
                )
            check_context(ctx, request)
            try:
                reply = await meter.call(request)
            except NodeFailure as exc:
                if mode == "tool_call" and not exc.retryable:
                    attempts.append({"mode": mode, "error": exc.code})
                    continue
                raise
            warnings.extend(stop_warnings(reply))
            raw = _tool_arguments(reply) if mode == "tool_call" else extract_json_object(reply.text)
            if raw is None:
                attempts.append({"mode": mode, "error": "no_json"})
                continue
            checked = check_values(config, raw)
            attempts.append({"mode": mode, "problems": len(checked[1])})
            if best is None or len(checked[1]) <= len(best[1]):
                best = checked
            if not checked[1]:
                break
        if best is None:
            values: dict[str, Any] = {param.name: None for param in config.parameters}
            problems = ["모델 응답에서 파라미터 JSON을 찾지 못했다"]
        else:
            values, problems = best
        reason = "; ".join(problems)
        return NodeResult(
            status="succeeded",
            outputs={**values, SUCCESS: not problems, REASON: reason},
            usage=meter.total,
            inputs={"query": query_text, "instruction": instruction},
            process={**meter.process(config.model.model_id), "attempts": attempts},
            warnings=warnings,
        )
