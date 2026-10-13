from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ReasoningEffort = Literal["minimal", "low", "medium", "high"]
REASONING_EFFORTS: tuple[ReasoningEffort, ...] = ("minimal", "low", "medium", "high")

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal"]

# ErrorEvent.code 값. 4.6의 런 실패 코드와 같은 이름을 쓴다.
ERR_MODEL_UNAVAILABLE = "model_unavailable"
ERR_MODEL_REQUEST_INVALID = "model_request_invalid"
ERR_MODEL_AUTH = "model_auth"
# 일부 출력을 이미 내보낸 뒤 스트림이 끊긴 경우. 폴백하지 않고 루프가 스텝을 다시 실행한다(4.6).
ERR_MODEL_STREAM_INTERRUPTED = "model_stream_interrupted"


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    media_type: str
    # 어댑터는 data_b64만 읽는다. storage_key만 있는 블록은 호출 전에 루프가 내용을 채워야 한다.
    data_b64: str | None = None
    storage_key: str | None = None
    filename: str | None = None


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]
    # 다음 요청에 원문 그대로 돌려줘야 하는 프로바이더 데이터(Gemini 사고 서명 등). 해석하지 않는다.
    provider_extra: dict[str, Any] | None = None


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


Block = Annotated[
    TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")
]


class CanonicalMessage(BaseModel):
    role: Literal["user", "assistant"]
    blocks: list[Block]
    provider_extra: dict[str, Any] | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ForcedTool(BaseModel):
    name: str


class ModelParams(BaseModel):
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int = Field(default=16000, ge=1)
    reasoning_effort: ReasoningEffort | None = None
    # None이면 도구가 있을 때 "auto". 과업 독촉 스텝만 ForcedTool(name="submit_result").
    tool_choice: Literal["auto"] | ForcedTool | None = None


class ChatRequest(BaseModel):
    model: str | None = None
    system: str
    messages: list[CanonicalMessage]
    tools: list[ToolSpec] = Field(default_factory=list[ToolSpec])
    params: ModelParams = Field(default_factory=ModelParams)
    # run_id 등 로깅용. 프로바이더에 보내지 않는다.
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])


class Usage(BaseModel):
    input_tokens: int = 0
    # 사고 토큰을 포함한 전체 출력. 비용과 토큰 예산은 이 값으로 계산한다.
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    estimated: bool = False


class _Event(BaseModel):
    model_config = ConfigDict(frozen=True)


class TextDelta(_Event):
    type: Literal["text_delta"] = "text_delta"
    text: str


class ThinkingDelta(_Event):
    type: Literal["thinking_delta"] = "thinking_delta"
    text: str


class ToolCallStart(_Event):
    type: Literal["tool_call_start"] = "tool_call_start"
    index: int
    id: str
    name: str


class ToolCallDelta(_Event):
    type: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    json_fragment: str


class ToolCallEnd(_Event):
    type: Literal["tool_call_end"] = "tool_call_end"
    index: int
    id: str
    name: str
    # 누적한 인자 JSON 원문. 파싱·스키마 검증은 루프가 한다.
    arguments: str
    provider_extra: dict[str, Any] | None = None


class MessageEnd(_Event):
    type: Literal["message_end"] = "message_end"
    stop_reason: StopReason
    usage: Usage
    raw_stop: str | None = None
    model: str
    key_index: int | None = None
    provider_extra: dict[str, Any] | None = None


class ErrorEvent(_Event):
    type: Literal["error"] = "error"
    code: str
    message: str
    retryable: bool
    model: str | None = None
    status: int | None = None


ModelEvent = Annotated[
    TextDelta
    | ThinkingDelta
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | MessageEnd
    | ErrorEvent,
    Field(discriminator="type"),
]


class LLMError(Exception):
    """스트림이 아닌 호출(list_models 등)의 프로바이더 오류. 메시지에 키 값이 없다."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
