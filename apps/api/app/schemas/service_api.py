"""서비스 API(/api/v1) 요청·응답(설계 6.4).

대화(conversation)는 source=api 세션이고, 과업은 source=task 세션의 런이다. user는 모든 요청에서
선택이며 주면 session.external_user에 남고 그 사용자의 대화만 보인다.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.common import ApprovalMode, RunLimits
from app.schemas.events import UsageTotal
from app.schemas.run import PendingInteractionOut, RunErrorOut, RunOut, RunStatus
from app.schemas.session import (
    MAX_MESSAGE_CHARS,
    MAX_MESSAGE_FILES,
    MessageOut,
    SessionFileOut,
    SessionTitle,
)
from app.schemas.task import SessionMode

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
