"""서비스 API(/api/v1) 요청·응답(설계 6.4, 2단계 설계 6.3).

에이전트 키에서 대화(conversation)는 source=api 세션이고, 과업은 source=task 세션의 런이다. user는
모든 요청에서 선택이며 주면 session.external_user에 남고 그 사용자의 대화만 보인다.

워크플로우 키는 같은 경로를 쓰되 대상에 맞는 모양(Flow*)으로 답한다. 챗플로우 대화는
wf_conversation, 메시지는 wf_message이고 블록 대신 text다.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.common import ApprovalMode, RunLimits
from app.schemas.events import UsageTotal, WorkflowKind
from app.schemas.run import PendingInteractionOut, RunErrorOut, RunOut, RunStatus
from app.schemas.session import (
    MAX_MESSAGE_CHARS,
    MAX_MESSAGE_FILES,
    MessageOut,
    SessionFileOut,
    SessionTitle,
)
from app.schemas.task import SessionMode
from app.schemas.webapp import FlowField
from app.schemas.wf_conversation import WfMessageOut
from app.schemas.wf_run import WfPendingOut, WfRunErrorOut, WfRunOut
from app.schemas.workflow import WorkflowLimits

ExternalUser = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
MAX_INPUTS = 100
MAX_TASK_FILES = 20


class ChatMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_CHARS)]
    user: ExternalUser | None = None
    # 비우면 새 대화. files만 주면 그 파일을 올린 대화를 이어 쓴다(/files/upload).
    conversation_id: uuid.UUID | None = None
    # 이 메시지의 <context>에 그대로 들어간다. 에이전트가 입력 변수를 정의하지는 않는다.
    inputs: dict[str, Any] = Field(default_factory=dict[str, Any], max_length=MAX_INPUTS)
    # /files/upload로 올린 파일. 작업공간 uploads/<message_id>/로 간다.
    files: list[uuid.UUID] = Field(default_factory=list[uuid.UUID], max_length=MAX_MESSAGE_FILES)
    response_mode: Literal["streaming", "blocking"] = "streaming"
    # 새 대화의 session.metadata에 남는다. 모델에는 보내지 않는다.
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])


class ChatMessageResult(BaseModel):
    """blocking 응답. 승인·질문을 기다리면 status=waiting_input이고 pending_interaction이 있다."""

    conversation_id: uuid.UUID
    message_id: uuid.UUID | None
    run_id: uuid.UUID
    answer: str
    files: list[SessionFileOut]
    usage: UsageTotal
    status: RunStatus
    pending_interaction: PendingInteractionOut | None = None
    error: RunErrorOut | None = None


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: ExternalUser | None = None


class StopResult(BaseModel):
    result: Literal["success"] = "success"
    # 중지 요청 직후의 상태. 실행 중이던 런은 run.completed(cancelled)로 끝난다.
    status: RunStatus


class ApiRunInput(BaseModel):
    """질문(answer)·승인(decision) 응답. 콘솔의 /runs/{id}/input과 같다."""

    model_config = ConfigDict(extra="forbid")

    user: ExternalUser | None = None
    answer: Annotated[str, StringConstraints(max_length=20_000)] | None = None
    decision: Literal["approve", "reject"] | None = None
    note: Annotated[str, StringConstraints(max_length=2000)] | None = None
    session_auto_approve: bool = False
    # none: 바로 RunOut(202). streaming: 재개된 런의 SSE, blocking: 끝날 때까지 기다린 결과.
    response_mode: Literal["none", "streaming", "blocking"] = "none"


class ApiFlowRunInput(ApiRunInput):
    """워크플로우·챗플로우 런의 /input. 승인·질문 응답에 더해 human_input 폼 응답(kind=form,
    values)을 받는다. 파일 칸 값은 /files/upload로 올린 id다. 에이전트 키로 form을 보내면 422다."""

    kind: Literal["approval", "question", "form"] | None = None
    values: dict[str, Any] | None = Field(default=None, max_length=MAX_INPUTS)

    @property
    def is_form(self) -> bool:
        return self.kind == "form" or self.values is not None


class ConversationOut(BaseModel):
    id: uuid.UUID
    name: str | None
    external_user: str | None
    agent_version_id: uuid.UUID
    message_count: int
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    # 있으면 GET /chat-messages/{active_run.id}/events로 다시 붙는다.
    active_run: RunOut | None
    last_run: RunOut | None


class ConversationList(BaseModel):
    items: list[ConversationOut]
    # 다음 쪽이 있으면 cursor로 다시 부른다.
    next_cursor: str | None = None


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: SessionTitle
    user: ExternalUser | None = None


class ApiMessageList(BaseModel):
    # seq 오름차순. 더 앞의 메시지는 cursor=next_cursor로 다시 부른다.
    items: list[MessageOut]
    has_more: bool
    next_cursor: str | None = None


class FileUploadOut(BaseModel):
    file_id: uuid.UUID
    filename: str
    size: int
    mime: str
    # 파일이 속한 대화. conversation_id 없이 올리면 새 대화가 생긴다.
    conversation_id: uuid.UUID


class AgentInfoOut(BaseModel):
    agent_id: uuid.UUID
    slug: str
    name: str
    icon: str | None
    description: str
    published: bool
    version_id: uuid.UUID | None
    version_number: int | None
    published_at: datetime | None
    approval_mode: ApprovalMode | None
    webapp_enabled: bool


class FileUploadLimits(BaseModel):
    max_bytes: int
    max_files_per_message: int


class AgentParametersOut(BaseModel):
    # 1단계 에이전트는 입력 변수를 정의하지 않는다. inputs는 메시지 <context>로만 간다.
    user_input_form: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    approval_mode: ApprovalMode | None
    limits: RunLimits | None
    tools: list[str]
    file_upload: FileUploadLimits
    response_modes: list[str]
    scopes: list[str]


class AgentTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: ExternalUser | None = None
    # 변수명 → 값. 파일은 {"file_id": "<id>"}로 준다(task/files/로 복사되고 경로로 바뀐다).
    inputs: dict[str, Any] = Field(default_factory=dict[str, Any], max_length=MAX_INPUTS)
    # {{변수}} 템플릿.
    instruction: Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_CHARS)]
    # JSON Schema(type: object, additionalProperties: false). 있으면 결과를 이 모양으로 받는다.
    output_schema: dict[str, Any] | None = None
    files: list[uuid.UUID] = Field(default_factory=list[uuid.UUID], max_length=MAX_TASK_FILES)
    # 준 필드만 발행 버전의 한도를 덮어쓴다.
    limits: RunLimits | None = None
    # ephemeral: 끝나면 작업공간을 지운다(결과 파일은 남는다). reuse: 작업공간을 남긴다.
    session_mode: SessionMode = "ephemeral"
    # reuse일 때 이어 쓸 과업 세션(앞 과업 응답의 session_id). 비우면 새 세션.
    session_id: uuid.UUID | None = None
    response_mode: Literal["streaming", "blocking"] = "blocking"


class WorkflowRunRequest(BaseModel):
    """POST /workflows/run. 파일 입력은 start의 file 필드에 /files/upload로 올린 id를 준다."""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(default_factory=dict[str, Any], max_length=MAX_INPUTS)
    user: ExternalUser | None = None
    response_mode: Literal["streaming", "blocking"] = "streaming"


class WorkflowRunResult(BaseModel):
    """워크플로우 런의 blocking 응답이자 폴링 결과. 입력을 기다리면 pending_interaction이 있다."""

    workflow_run_id: uuid.UUID
    status: RunStatus
    # end 출력을 합친 전문. succeeded가 아니면 null.
    outputs: dict[str, Any] | None
    usage: UsageTotal
    # 시작부터 끝(아직이면 지금)까지의 벽시계 시간. 입력 대기 시간도 들어간다.
    elapsed_ms: int
    error: WfRunErrorOut | None = None
    pending_interaction: WfPendingOut | None = None
    # 챗플로우 런이면 대화와 그 턴의 assistant 메시지.
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    created_at: datetime
    ended_at: datetime | None = None


class WorkflowRunList(BaseModel):
    # 최근 것부터. 다음 쪽이 있으면 cursor로 다시 부른다.
    items: list[WorkflowRunResult]
    next_cursor: str | None = None


class FlowChatMessageResult(BaseModel):
    """챗플로우 키의 chat-messages blocking 응답. 1단계 ChatMessageResult와 같은 이름을 쓴다."""

    conversation_id: uuid.UUID
    # 이 턴의 assistant 메시지.
    message_id: uuid.UUID | None
    # 워크플로우 런. 중지·응답·이벤트는 /chat-messages/{run_id}/...로 부른다.
    run_id: uuid.UUID
    answer: str
    usage: UsageTotal
    status: RunStatus
    pending_interaction: WfPendingOut | None = None
    error: WfRunErrorOut | None = None


class FlowConversationOut(BaseModel):
    id: uuid.UUID
    name: str | None
    external_user: str | None
    wf_version_id: uuid.UUID
    message_count: int
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FlowConversationDetail(FlowConversationOut):
    # 있으면 GET /chat-messages/{active_run.id}/events로 다시 붙는다.
    active_run: WfRunOut | None
    last_run: WfRunOut | None


class FlowConversationList(BaseModel):
    items: list[FlowConversationOut]
    next_cursor: str | None = None


class FlowApiMessageList(BaseModel):
    # seq 오름차순. 더 앞의 메시지는 cursor=next_cursor로 다시 부른다.
    items: list[WfMessageOut]
    has_more: bool
    next_cursor: str | None = None


class FlowFileUploadOut(BaseModel):
    file_id: uuid.UUID
    filename: str
    size: int
    mime: str
    # conversation_id를 주고 올린 챗플로우 파일만 그 대화를 가리킨다.
    conversation_id: uuid.UUID | None


class FlowInfoOut(BaseModel):
    workflow_id: uuid.UUID
    slug: str
    name: str
    icon: str | None
    description: str
    kind: WorkflowKind
    published: bool
    version_id: uuid.UUID | None
    version_number: int | None
    published_at: datetime | None
    webapp_enabled: bool


class FlowParametersOut(BaseModel):
    kind: WorkflowKind
    # 발행 버전 start 노드의 입력 필드. inputs에 이 이름으로 준다. 발행 전이면 빈 목록이다.
    user_input_form: list[FlowField]
    limits: WorkflowLimits | None
    file_upload: FileUploadLimits
    response_modes: list[str]
    scopes: list[str]
    # 챗플로우 웹앱 설정(settings.webapp)의 시작 문구와 추천 질문.
    opening_statement: str | None = None
    suggested_questions: list[str] = Field(default_factory=list[str])
