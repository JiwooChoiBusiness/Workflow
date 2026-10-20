"""런 이벤트(1단계 설계 4.5, 2단계 설계 3.9)의 단일 출처.

SSE, run_event·wf_run_event 영속, 화면 타입이 모두 이 모델을 따른다.
packages/event-schema/run-events.schema.json은 scripts/gen-event-schema.py가 여기서 만든다. 필드를
지우거나 뜻을 바꾸면 SCHEMA_VERSION을 올리고 스키마 파일을 다시 만든다(13.2). 필드를 더하는 것은
같은 버전 안에서 한다. 받는 쪽은 모르는 필드를 무시해야 하므로 스키마에 additionalProperties:
false를 두지 않는다.

v2는 워크플로우 런 이벤트(workflow.*, node.*, iteration.*, answer.*)를 더하고 봉투의 session_id를
nullable로 바꿨다. 워크플로우 런은 session_id가 null이고 챗플로우만 conversation_id가 있다. 같은 v2
안에서 하위 에이전트 호출의 child_run.*과 tool_call.completed.child_run_id를 더했다. human_input
노드(M19)의 폼 대기는 workflow.waiting_input에 kind=form과 form으로 싣고 그때 interaction은
null이다. loop 노드도 iteration.* 이벤트를 쓰고 total은 max_iterations다.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ApprovalMode

SCHEMA_VERSION = 2

ToolCallStatus = Literal["ok", "error", "timeout", "cancelled", "rejected"]
InteractionKind = Literal["question", "approval"]
# 워크플로우 런의 입력 대기. approval·question은 agent 노드의 자식 런, form은 human_input 노드다.
WfPendingKind = Literal["approval", "question", "form"]
FormFieldType = Literal["string", "paragraph", "number", "boolean", "select", "file", "file_list"]
RunEndStatus = Literal["succeeded", "failed", "cancelled", "waiting_input"]
WorkflowKind = Literal["workflow", "chatflow"]
WorkflowEndStatus = Literal["succeeded", "failed", "cancelled", "waiting_input"]
NodeRunStatus = Literal["running", "succeeded", "failed", "skipped", "cancelled", "waiting_input"]


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
    # call_agent 호출이면 그 자식 런. duration_ms는 자식의 벽시계 시간이다(오케스트레이션 3.4).
    child_run_id: uuid.UUID | None = None


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


class ChildRunStartedData(BaseModel):
    """call_agent가 자식 런을 만들고 부모가 그 결과를 기다리기 시작했다(오케스트레이션 6.1).

    부모 런은 run.completed 없이 열린 채로 남는다. 자식의 진행과 승인은 child_session_id 세션의
    이벤트 스트림으로 따로 받는다.
    """

    tool_call_id: str
    child_run_id: uuid.UUID
    child_session_id: uuid.UUID
    agent_id: uuid.UUID
    agent_slug: str
    agent_name: str
    # 자식이 쓰는 버전. 초안이면 발행 스냅샷 없이 빈 작업공간에서 시작한다.
    version: Literal["draft", "published"]
    depth: int
    result_format: Literal["text", "json"]
    # 모델이 쓴 task의 앞 2KB.
    task_preview: str


class ChildRunCompletedData(BaseModel):
    """자식 런이 끝나 부모가 다시 큐에 들어간다. 결과는 뒤이은 tool_call.completed에 있다."""

    tool_call_id: str
    child_run_id: uuid.UUID
    status: Literal["succeeded", "failed", "cancelled"]
    error: RunErrorData | None = None
    steps: int
    usage_total: UsageTotal
    duration_ms: int
    # 자식의 결과 파일 수.
    files: int


class PingData(BaseModel):
    pass


class WorkflowQueuedData(BaseModel):
    # 워크플로우 레인 대기열에서 몇 번째인지. 1이면 다음 차례다.
    position: int


class WorkflowStartedData(BaseModel):
    workflow_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    kind: WorkflowKind
    # 런 입력의 4KB 미리보기(마스킹). 전문은 wf_run.inputs.
    inputs_preview: dict[str, Any]
    truncated: bool
    resumed: bool
    node_count: int


class NodeStartedData(BaseModel):
    node_run_id: uuid.UUID
    node_id: str
    node_type: str
    title: str
    # 런 안 실행 순서. 재시도와 skipped 기록도 번호를 하나씩 받는다.
    index: int
    iteration_id: str | None = None
    iteration_index: int | None = None
    attempt: int
    # agent 노드를 재개할 때 그 자식 런과 세션. 화면은 세션 스트림
    # (/sessions/{sid}/events?run_id=)으로 자식의 진행·승인을 본다(3.8). 첫 시도에는 아직 자식이
    # 없어 node.progress로 알린다.
    child_run_id: uuid.UUID | None = None
    child_session_id: uuid.UUID | None = None


class NodeTextDeltaData(BaseModel):
    node_id: str
    node_run_id: uuid.UUID
    text: str


class NodeProgressData(BaseModel):
    """agent 노드의 자식 런 진행. 자식을 만들자마자 한 번, 그 뒤 10초마다 온다."""

    node_id: str
    node_run_id: uuid.UUID
    steps: int | None = None
    usage: UsageTotal | None = None
    # 자식 런이 에이전트 레인 대기열에서 몇 번째인지. 돌고 있으면 null.
    position: int | None = None
    child_run_id: uuid.UUID | None = None
    child_session_id: uuid.UUID | None = None


class NodeWaitingData(BaseModel):
    node_id: str
    node_run_id: uuid.UUID | None = None
    reason: Literal["queue", "rate_limit"]
    position: int | None = None


class NodeRetryingData(BaseModel):
    node_id: str
    # 방금 실패한 시도의 노드 런. attempt는 이어서 시작할 시도 번호다(첫 시도가 1).
    node_run_id: uuid.UUID
    attempt: int
    reason: str


class NodeUsageData(BaseModel):
    """모델을 부른 노드(llm 등)의 호출 한 번. 1단계 usage와 같은 값에 노드가 붙고 step이 없다."""

    node_id: str
    node_run_id: uuid.UUID
    model: str
    key_index: int | None = None
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    estimated: bool
    cost_usd: float | None = None


class NodeError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class NodeCompletedData(BaseModel):
    node_run_id: uuid.UUID
    node_id: str
    # waiting_input은 agent 노드가 승인·질문을 기다리며 멈춘 것이다. 재개하면 같은 node_run_id로
    # 다시 시작한다.
    status: Literal["succeeded", "failed", "skipped", "cancelled", "waiting_input"]
    # 4KB 미리보기(마스킹). 전문은 노드 런 상세 API로 받는다.
    outputs_preview: dict[str, Any] = Field(default_factory=dict[str, Any])
    truncated: bool = False
    error: NodeError | None = None
    usage: UsageTotal | None = None
    duration_ms: int
    # 분기 노드가 고른 핸들. 오류 분기로 갔으면 fail.
    branch: str | None = None
    warnings: list[str] = Field(default_factory=list[str])


class IterationStartedData(BaseModel):
    node_id: str
    # iteration은 원소 수, loop는 max_iterations(실제로 몇 번 돌지는 끝나야 안다).
    total: int


class IterationNextData(BaseModel):
    node_id: str
    index: int


class IterationCompletedData(BaseModel):
    node_id: str
    count: int
    failed_count: int
    duration_ms: int


class AnswerDeltaData(BaseModel):
    text: str
    node_id: str
    node_run_id: uuid.UUID | None = None


class AnswerCompletedData(BaseModel):
    text: str
    message_id: uuid.UUID | None = None


class WorkflowFormField(BaseModel):
    """human_input 폼의 입력 칸. start 필드와 같은 모양이고 값도 같은 규칙으로 검사·변환한다."""

    name: str
    type: FormFieldType
    label: str = ""
    required: bool = False
    default: Any = None
    # select의 선택지.
    options: list[str] | None = None
    # string·paragraph의 글자 수 상한.
    max_length: int | None = None


class WorkflowFormData(BaseModel):
    """human_input 노드가 기다리는 폼. 답은 /input에 kind=form과 values({필드 이름: 값})로
    보낸다."""

    form_id: str
    # 렌더한 안내문(마크다운).
    description: str = ""
    fields: list[WorkflowFormField]
    # 기다리기 시작한 때. WAITING_INPUT_TTL_HOURS가 지나면 노드가 input_timeout으로 실패한다.
    created_at: datetime


class WorkflowWaitingInputData(BaseModel):
    node_id: str
    iteration_index: int | None = None
    child_run_id: uuid.UUID | None = None
    # approval·question이면 agent 노드의 자식 런이 묻는 것이고 form이면 null이다.
    interaction: RunWaitingInputData | None = None
    # M19 전에 남은 이벤트에는 없다. 그때는 interaction.kind를 본다.
    kind: WfPendingKind | None = None
    # kind=form(human_input 노드)일 때만 있다.
    form: WorkflowFormData | None = None


class WorkflowResumedData(BaseModel):
    node_id: str


class WorkflowErrorData(BaseModel):
    code: str
    message: str
    node_id: str | None = None
    retryable: bool = False


class WorkflowCompletedData(BaseModel):
    status: WorkflowEndStatus
    # end 노드 출력을 완료 순서로 합친 값의 4KB 미리보기(마스킹). 같은 이름은 나중 값이 이긴다.
    # 전문은 wf_run.outputs(GET /wf-runs/{rid}). succeeded가 아니면 null.
    outputs: dict[str, Any] | None = None
    truncated: bool = False
    usage_total: UsageTotal
    node_runs: int
    duration_ms: int
    error: WorkflowErrorData | None = None


# 1단계 에이전트 런의 이벤트. run_event에 남는다.
AGENT_RUN_EVENT_DATA_MODELS: dict[str, type[BaseModel]] = {
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
    # 오케스트레이션(하위 에이전트 호출). 부모 런의 스트림에 온다.
    "child_run.started": ChildRunStartedData,
    "child_run.completed": ChildRunCompletedData,
}

# 워크플로우 런의 이벤트(2단계 3.9). wf_run_event에 남는다. workflow.queued는 서비스가, 나머지는
# 엔진(app/workflow/engine.py)이 낸다.
WF_EVENT_DATA_MODELS: dict[str, type[BaseModel]] = {
    "workflow.queued": WorkflowQueuedData,
    "workflow.started": WorkflowStartedData,
    "node.started": NodeStartedData,
    "node.text_delta": NodeTextDeltaData,
    "node.progress": NodeProgressData,
    "node.waiting": NodeWaitingData,
    "node.retrying": NodeRetryingData,
    "node.usage": NodeUsageData,
    "node.completed": NodeCompletedData,
    "iteration.started": IterationStartedData,
    "iteration.next": IterationNextData,
    "iteration.completed": IterationCompletedData,
    "answer.delta": AnswerDeltaData,
    "answer.completed": AnswerCompletedData,
    "workflow.waiting_input": WorkflowWaitingInputData,
    "workflow.resumed": WorkflowResumedData,
    "workflow.error": WorkflowErrorData,
    "workflow.completed": WorkflowCompletedData,
}

EVENT_DATA_MODELS: dict[str, type[BaseModel]] = {
    **AGENT_RUN_EVENT_DATA_MODELS,
    **WF_EVENT_DATA_MODELS,
    "ping": PingData,
}

# SQLite(run_event·wf_run_event)에 남기지 않는 이벤트. 재접속 때 링버퍼가 없으면 다시 볼 수 없으므로
# 화면은 message.completed·tool_call.completed·node.completed의 전체 내용으로 다시 그린다(2.4).
TRANSIENT_EVENT_TYPES = frozenset(
    {
        "message.delta",
        "thinking.delta",
        "tool_call.input_delta",
        "tool_call.output_delta",
        "node.text_delta",
        "node.progress",
        "node.waiting",
        "answer.delta",
        "ping",
    }
)
# 이벤트 하나가 4KB를 넘지 않게 나눠 보내는 델타의 글자 필드(2.4).
DELTA_TEXT_FIELDS: dict[str, str] = {
    "message.delta": "text",
    "thinking.delta": "text",
    "tool_call.input_delta": "json_fragment",
    "tool_call.output_delta": "text",
    "node.text_delta": "text",
    "answer.delta": "text",
}
# 스트림을 닫는 이벤트. waiting_input으로 멈출 때도 run.completed·workflow.completed가 온다.
# run.error·workflow.error는 언제나 바로 뒤에 완료 이벤트(status=failed)가 따라오므로 서버는 완료
# 이벤트에서 연결을 닫는다.
TERMINAL_EVENT_TYPES = frozenset({"run.completed", "workflow.completed"})


class RunEventOut(BaseModel):
    """SSE data 줄과 영속 이벤트 조회 응답의 공통 모양."""

    run_id: uuid.UUID
    # 워크플로우 런은 null이다.
    session_id: uuid.UUID | None
    # 챗플로우 런에만 있다.
    conversation_id: uuid.UUID | None = None
    seq: int
    ts: datetime
    type: str
    data: dict[str, Any]


def run_events_json_schema() -> dict[str, Any]:
    """packages/event-schema/run-events.schema.json의 내용."""
    defs: dict[str, Any] = {}
    variants: list[dict[str, Any]] = []
    uuid_or_null = {"anyOf": [{"type": "string", "format": "uuid"}, {"type": "null"}]}
    envelope = {
        "run_id": {"type": "string", "format": "uuid"},
        "session_id": uuid_or_null,
        "conversation_id": uuid_or_null,
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
            "x-transient-types의 이벤트는 저장되지 않아 재접속 때 다시 오지 않을 수 있다. "
            "x-terminal-types의 이벤트 뒤에 서버가 스트림을 닫는다. 워크플로우 런은 session_id가 "
            "null이고 챗플로우 런만 conversation_id가 있다."
        ),
        "x-schema-version": SCHEMA_VERSION,
        "x-transient-types": sorted(TRANSIENT_EVENT_TYPES),
        "x-terminal-types": sorted(TERMINAL_EVENT_TYPES),
        "oneOf": variants,
        "$defs": dict(sorted(defs.items())),
    }
