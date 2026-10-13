from pydantic import BaseModel, ConfigDict

from app.llm.types import REASONING_EFFORTS, ReasoningEffort

# 목록에 없는 모델에 쓰는 보수적 기본값(설계 4.3).
DEFAULT_CONTEXT_WINDOW = 1_000_000
DEFAULT_MAX_OUTPUT_TOKENS = 65_536


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: str
    context_window: int = DEFAULT_CONTEXT_WINDOW
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    supports_tools: bool = True
    supports_reasoning_effort: bool = True
    supports_images: bool = True
    reasoning_efforts: tuple[ReasoningEffort, ...] = REASONING_EFFORTS
    # False면 능력표에 없는 모델이라 기본값을 돌려준 것이다.
    known: bool = False


# 2026-10-13 라이브 확인.
# - 컨텍스트·출력 상한: GET /v1beta/models/<id>의 inputTokenLimit 1,048,576, outputTokenLimit 65,536
#   (세 모델 같음). OpenAI 호환 /models 응답에는 상한이 없다.
# - reasoning_effort="minimal": 3.8·3.7은 400 "Thinking level MINIMAL is not supported", 3.6은 정상.
#   "low"는 세 모델 모두 정상. 지정하지 않으면 모델 기본 수준(공식 문서상 medium)이다.
# - 도구 호출: 세 모델 모두 확인(3.7은 3.8이 503을 내 폴백된 라이브 테스트에서).
# - 이미지 입력은 확인하지 않았다(설계 기본값 True).
_GEMINI_3_FLASH = ModelCapabilities(
    model_id="",
    context_window=1_048_576,
    max_output_tokens=65_536,
    reasoning_efforts=("low", "medium", "high"),
    known=True,
)

CATALOG: dict[str, ModelCapabilities] = {
    "gemini-3.8-flash": _GEMINI_3_FLASH.model_copy(update={"model_id": "gemini-3.8-flash"}),
    "gemini-3.7-flash": _GEMINI_3_FLASH.model_copy(update={"model_id": "gemini-3.7-flash"}),
    "gemini-3.6-flash": _GEMINI_3_FLASH.model_copy(
        update={"model_id": "gemini-3.6-flash", "reasoning_efforts": REASONING_EFFORTS}
    ),
}


def get_capabilities(model_id: str) -> ModelCapabilities:
    caps = CATALOG.get(model_id)
    if caps is not None:
        return caps
    return ModelCapabilities(model_id=model_id)


def list_capabilities() -> list[ModelCapabilities]:
    return list(CATALOG.values())


def resolve_reasoning_effort(
    caps: ModelCapabilities, requested: ReasoningEffort | None
) -> ReasoningEffort | None:
    if requested is None or not caps.supports_reasoning_effort or not caps.reasoning_efforts:
        return None
    if requested in caps.reasoning_efforts:
        return requested
    # 폴백 모델마다 허용값이 달라서(3.8은 minimal 불가) 400 대신 가장 가까운 상위 단계로 올린다.
    position = REASONING_EFFORTS.index(requested)
    for candidate in REASONING_EFFORTS[position:]:
        if candidate in caps.reasoning_efforts:
            return candidate
    for candidate in reversed(REASONING_EFFORTS[:position]):
        if candidate in caps.reasoning_efforts:
            return candidate
    return None
