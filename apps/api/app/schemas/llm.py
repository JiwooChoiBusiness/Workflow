from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.llm.types import ReasoningEffort, Usage


class LlmKeyStatus(BaseModel):
    index: int
    # 키 값은 내보내지 않는다. 화면에서 어느 키인지 알아볼 수 있을 만큼만.
    last4: str
    # 429를 받아 쉬는 중이면 풀리는 시각. 쿨다운은 (키, 모델) 단위라 모델별 시각 중
    # 가장 늦은 값이다.
    cooldown_until: datetime | None = None
    cooldown_by_model: dict[str, datetime] = Field(default_factory=dict[str, datetime])
    # 401·403을 받아 서버가 다시 뜰 때까지 쓰지 않는 키.
    disabled: bool = False
    # 서버가 뜬 뒤 모델 호출에서 센 값. 연결 테스트의 모델 목록 조회는 세지 않는다.
    success_count: int = 0
    failure_count: int = 0
    rate_limited_count: int = 0
    last_429_at: datetime | None = None


class LlmStatus(BaseModel):
    provider: Literal["gemini"] = "gemini"
    fake: bool
    base_url: str
    key_count: int
    keys: list[LlmKeyStatus]
    fallback_models: list[str]
    key_cooldown_sec: int


class LlmTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 모델 목록 조회에 쓸 키. 비우면 0번 키부터 시도하고 429·401·403·5xx·연결 오류면 다음 키로
    # 넘어간다. 생성 호출은 이 값과 관계없이 어댑터의 키 순환을 따른다.
    key_index: int | None = Field(default=None, ge=0)


class LlmError(BaseModel):
    code: str
    message: str
    status_code: int | None = None


class LlmAttempt(BaseModel):
    key_index: int
    key_last4: str
    ok: bool
    status_code: int | None = None
    latency_ms: int
    error: LlmError | None = None


class LlmGeneration(BaseModel):
    ok: bool
    model: str | None = None
    key_index: int | None = None
    key_last4: str | None = None
    latency_ms: int
    stop_reason: str | None = None
    # 응답 앞부분. 연결 확인용이라 길게 두지 않는다.
    text: str = ""
    usage: Usage | None = None
    error: LlmError | None = None


class LlmTestResult(BaseModel):
    # 모델 목록 조회와 생성 호출이 모두 성공했을 때만 true.
    ok: bool
    fake: bool
    key_index: int | None = None
    key_last4: str | None = None
    latency_ms: int | None = None
    model_count: int
    models: list[str]
    attempts: list[LlmAttempt]
    # 가짜 모드에서는 생성 호출을 하지 않아 null이다.
    generation: LlmGeneration | None = None
    error: LlmError | None = None


class ModelCapabilityInfo(BaseModel):
    context_window: int
    max_output_tokens: int
    supports_tools: bool
    # 이 모델이 받는 reasoning_effort 값. 비어 있으면 지정할 수 없다.
    reasoning_efforts: list[ReasoningEffort]
    # false면 능력표(app/llm/catalog.py)에 없는 모델이라 보수적 기본값이다.
    known: bool


class ModelEntry(BaseModel):
    id: str
    # GEMINI_MODEL_FALLBACK에서의 순서. 목록에 없으면 null.
    fallback_index: int | None = None
    # 라이브 목록(GET /models)에 있는지. 라이브 목록을 받지 못했으면 null.
    live: bool | None = None
    # 폴백 목록의 모델에만 붙는다. 라이브 목록에만 있는 모델은 null.
    capabilities: ModelCapabilityInfo | None = None


class ModelList(BaseModel):
    fake: bool
    models: list[ModelEntry]
    live_ok: bool
    live_error: LlmError | None = None
    fetched_at: datetime | None = None
