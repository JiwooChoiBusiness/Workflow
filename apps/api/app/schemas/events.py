"""런 이벤트(설계 4.5)의 단일 출처.

SSE, run_event 영속, 화면 타입이 모두 이 모델을 따른다.
packages/event-schema/run-events.schema.json은 scripts/gen-event-schema.py가 여기서 만든다. 필드를
지우거나 뜻을 바꾸면 SCHEMA_VERSION을 올리고 스키마 파일을 다시 만든다(13.2). 필드를 더하는 것은
같은 버전 안에서 한다. 받는 쪽은 모르는 필드를 무시해야 하므로 스키마에 additionalProperties:
false를 두지 않는다.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ApprovalMode

SCHEMA_VERSION = 1

ToolCallStatus = Literal["ok", "error", "timeout", "cancelled", "rejected"]
InteractionKind = Literal["question", "approval"]
RunEndStatus = Literal["succeeded", "failed", "cancelled", "waiting_input"]


class RunQueuedData(BaseModel):
    # 1이면 다음 차례다.
    position: int


class RunStartedData(BaseModel):
    step_limit: int
    # 처음 시도하는 모델(지정이 없으면 GEMINI_MODEL_FALLBACK의 첫 모델). 실제로 쓴 모델은 usage에
    # 있다.
    model: str | None
    workspace_instance_id: uuid.UUID | None
    resumed: bool
    # 세션 자동 승인까지 반영한 값.
    approval_mode: ApprovalMode


class WorkspaceEventData(BaseModel):
    snapshot_id: str | None = None
    packages: int | None = None
    message: str | None = None


class StepStartedData(BaseModel):
    step: int


class MessageDeltaData(BaseModel):
    message_id: uuid.UUID
    text: str


class ThinkingDeltaData(BaseModel):
    text: str


class ToolCallStartedData(BaseModel):
    tool_call_id: str
    name: str


class ToolCallInputDeltaData(BaseModel):
    tool_call_id: str
    json_fragment: str


class ToolCallReadyData(BaseModel):
    tool_call_id: str
    name: str
    # 민감한 HTTP 헤더 값은 ***로 가린다.
    input: dict[str, Any]


class ToolCallOutputDeltaData(BaseModel):
    tool_call_id: str
    stream: Literal["stdout", "stderr"]
    text: str


class ToolCallCompletedData(BaseModel):
    tool_call_id: str
    status: ToolCallStatus
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int
    # 거부된 할당 요청도 포함한 Job 메모리 최고치라 "최고 요청치"로 읽는다(12.2 M3).
    peak_memory_bytes: int | None = None
    # 출력이 256KB 이하면 전문, 넘으면 앞 24KB + 뒤 8KB 발췌. 전문은 도구 출력 다운로드로 받는다.
    output_excerpt: str
    output_bytes: int
    output_storage_key: str | None = None


class FileChangedData(BaseModel):
    path: str
    size: int
    session_file_id: uuid.UUID | None = None
    downloadable: bool
    # 보존하지 않은 이유(too_large, run_limit, copy_failed). 내려받을 수 있으면 null.
    reason: str | None = None


class RunWaitingInputData(BaseModel):
    interaction_id: str
    kind: InteractionKind
    question: str | None = None
    options: list[str] | None = None
    allow_free_text: bool | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    input: dict[str, Any] | None = None
    cwd: str | None = None
    timeout_sec: int | None = None


class InteractionResolvedData(BaseModel):
    interaction_id: str
    kind: InteractionKind
    decision: Literal["approve", "reject"] | None = None
    note: str | None = None
    answer: str | None = None
    session_auto_approve: bool | None = None


class RunResumedData(BaseModel):
    interaction_id: str


class RunRetryingData(BaseModel):
    step: int
    # model_stream_interrupted(스트림 끊김), max_tokens(도구 호출이 출력 상한에 잘림),
    # tool_choice_rejected(과업 독촉의 강제 tool_choice를 모델이 받지 않아 강제 없이 다시 부름)
    reason: str
    attempt: int


class ContextCompactedData(BaseModel):
    before_tokens: int
    after_tokens: int
    summary_message_id: uuid.UUID


class ContextPromptChangedData(BaseModel):
    # 초안을 고친 뒤 같은 미리보기 세션에서 이어 갈 때 생긴다(4.8). 히스토리는 그대로 둔다.
    previous_hash: str | None = None
    prompt_hash: str


class UsageData(BaseModel):
    step: int
    model: str
    key_index: int | None = None
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    estimated: bool
    # 가격표(workspace.settings.model_prices)에 그 모델이 있을 때만 값이 있다.
    cost_usd: float | None = None


class MessageFileData(BaseModel):
    session_file_id: uuid.UUID | None = None
    path: str
    size: int
    downloadable: bool
    filename: str | None = None
    mime: str | None = None


class MessageCompletedData(BaseModel):
    message_id: uuid.UUID
    text: str
    # end_turn, tool_use, max_tokens, refusal 외에 cancelled(중지로 끊긴 부분 응답),
    # max_steps(스텝 한도에 닿아 플랫폼이 붙인 안내)가 있다.
    stop_reason: str
    # 런이 끝나는 마지막 메시지에만 그 런이 바꾼 파일이 들어 있다.
    files: list[MessageFileData] = Field(default_factory=list[MessageFileData])


class UsageTotal(BaseModel):
    """run.usage 열과 run.completed.usage_total의 구조."""

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float | None = None
    estimated: bool = False


class RunErrorData(BaseModel):
    code: str
    message: str
    retryable: bool = False


class RunCompletedData(BaseModel):
    status: RunEndStatus
    usage_total: UsageTotal
    steps: int
    changes_truncated: bool | None = None
    # 과업(4.10) 결과. 채팅 런은 null.
    output: Any = None
    # 스텝 한도에 닿아 끝났으면 "steps".
    limit: Literal["steps"] | None = None
    # failed면 바로 앞 run.error와 같은 내용. cancelled에도 이유가 있으면(input_timeout) 여기에
    # 온다.
    error: RunErrorData | None = None


class PingData(BaseModel):
    pass


EVENT_DATA_MODELS: dict[str, type[BaseModel]] = {
    "run.queued": RunQueuedData,
    "run.started": RunStartedData,
    "workspace.restoring": WorkspaceEventData,
    "workspace.restored": WorkspaceEventData,
    "workspace.restore_warning": WorkspaceEventData,
    "workspace.recreated": WorkspaceEventData,
    "step.started": StepStartedData,
    "message.delta": MessageDeltaData,
    "thinking.delta": ThinkingDeltaData,
    "tool_call.started": ToolCallStartedData,
    "tool_call.input_delta": ToolCallInputDeltaData,
    "tool_call.ready": ToolCallReadyData,
    "tool_call.output_delta": ToolCallOutputDeltaData,
    "tool_call.completed": ToolCallCompletedData,
    "file.changed": FileChangedData,
    "run.waiting_input": RunWaitingInputData,
    "interaction.resolved": InteractionResolvedData,
    "run.resumed": RunResumedData,
    "run.retrying": RunRetryingData,
    "context.compacted": ContextCompactedData,
    "context.prompt_changed": ContextPromptChangedData,
    "usage": UsageData,
    "message.completed": MessageCompletedData,
    "run.completed": RunCompletedData,
    "run.error": RunErrorData,
    "ping": PingData,
}

# SQLite run_event에 남기지 않는 이벤트. 재접속 때 링버퍼가 없으면 다시 볼 수 없으므로 화면은
# message.completed·tool_call.completed의 전체 내용으로 다시 그린다(2.4).
TRANSIENT_EVENT_TYPES = frozenset(
    {"message.delta", "thinking.delta", "tool_call.input_delta", "tool_call.output_delta", "ping"}
)
# 이벤트 하나가 4KB를 넘지 않게 나눠 보내는 델타의 글자 필드(2.4).
DELTA_TEXT_FIELDS: dict[str, str] = {
    "message.delta": "text",
    "thinking.delta": "text",
    "tool_call.input_delta": "json_fragment",
    "tool_call.output_delta": "text",
}
# 스트림을 닫는 이벤트. waiting_input으로 멈출 때도 run.completed가 온다. run.error는 언제나 바로
# 뒤에 run.completed(status=failed)가 따라오므로 서버는 run.completed에서 연결을 닫는다.
TERMINAL_EVENT_TYPE = "run.completed"


class RunEventOut(BaseModel):
    """SSE data 줄과 영속 이벤트 조회 응답의 공통 모양."""

    run_id: uuid.UUID
    session_id: uuid.UUID
    seq: int
    ts: datetime
    type: str
    data: dict[str, Any]


def run_events_json_schema() -> dict[str, Any]:
    """packages/event-schema/run-events.schema.json의 내용."""
    defs: dict[str, Any] = {}
    variants: list[dict[str, Any]] = []
    envelope = {
        "run_id": {"type": "string", "format": "uuid"},
        "session_id": {"type": "string", "format": "uuid"},
        "seq": {"type": "integer", "minimum": 1},
        "ts": {"type": "string", "format": "date-time"},
    }
    for event_type, model in EVENT_DATA_MODELS.items():
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(schema.pop("$defs", {}))
        defs[model.__name__] = schema
        if event_type == "ping":
            # ping은 런에 속하지 않는 생존 신호라 seq가 없다.
            variants.append(
                {
                    "title": event_type,
                    "type": "object",
                    "required": ["type"],
                    "properties": {
                        "type": {"const": event_type},
                        "ts": envelope["ts"],
                        "data": {"$ref": f"#/$defs/{model.__name__}"},
                    },
                }
            )
            continue
        variants.append(
            {
                "title": event_type,
                "type": "object",
                "required": ["run_id", "session_id", "seq", "ts", "type", "data"],
                "properties": {
                    **envelope,
                    "type": {"const": event_type},
                    "data": {"$ref": f"#/$defs/{model.__name__}"},
                },
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "RunEvent",
        "description": (
            "런 이벤트(SSE data 줄 한 개). SSE의 id는 seq, event는 type과 같다. "
            "x-transient-types의 이벤트는 저장되지 않아 재접속 때 다시 오지 않을 수 있다."
        ),
        "x-schema-version": SCHEMA_VERSION,
        "x-transient-types": sorted(TRANSIENT_EVENT_TYPES),
        "x-terminal-type": TERMINAL_EVENT_TYPE,
        "oneOf": variants,
        "$defs": dict(sorted(defs.items())),
    }
