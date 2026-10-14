import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.schemas.events import InteractionKind, RunEventOut, UsageTotal
from app.schemas.session import SessionFileOut, SessionOut, WorkspaceInstanceOut

RunStatus = Literal["queued", "running", "waiting_input", "succeeded", "failed", "cancelled"]


class RunErrorOut(BaseModel):
    code: str
    message: str
    retryable: bool = False


class PendingInteractionOut(BaseModel):
    """run.waiting_input 이벤트와 같은 내용. 새로고침 뒤 승인·질문 카드를 다시 띄울 때 쓴다."""

    interaction_id: str
    kind: InteractionKind
    tool_call_id: str | None = None
    tool_name: str | None = None
    question: str | None = None
    options: list[str] | None = None
    allow_free_text: bool | None = None
    input: dict[str, Any] | None = None
    cwd: str | None = None
    timeout_sec: int | None = None
    created_at: datetime


class RunChildOut(BaseModel):
    """call_agent로 부른 자식 런을 기다리는 중. 화면은 이 값으로 하위 에이전트 카드를 다시 그리고
    session_id의 이벤트 스트림에 붙는다(오케스트레이션 6.4, 8.1)."""

    run_id: uuid.UUID
    session_id: uuid.UUID
    agent_id: uuid.UUID
    agent_slug: str
    agent_name: str
    tool_call_id: str
    version: Literal["draft", "published"]
    since: datetime


class RunOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    agent_version_id: uuid.UUID
    trigger: str
    # 하위 에이전트를 기다리는 런은 DB에서 waiting_input이지만 여기서는 running이고 child가
    # 채워진다.
    status: RunStatus
    user_message_id: uuid.UUID
    # 이 런의 마지막 assistant 메시지(스텝마다 메시지가 하나씩 생긴다).
    assistant_message_id: uuid.UUID | None
    # {model_id(요청), used(마지막으로 쓴 모델), fallback_used}
    model: dict[str, Any]
    limits: dict[str, Any]
    approval_mode: str
    steps: int
    usage: UsageTotal
    error: RunErrorOut | None
    pending_interaction: PendingInteractionOut | None
    output: dict[str, Any] | None
    cancel_requested_at: datetime | None
    queued_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # call_agent가 만든 자식 런이면 부모 런과 깊이(지휘자 0, 자식 1, 손자 2).
    parent_run_id: uuid.UUID | None = None
    depth: int = 0
    child: RunChildOut | None = None


class RunList(BaseModel):
    items: list[RunOut]
    next_offset: int | None = None


class RunEventList(BaseModel):
    # 영속 이벤트만 있다. 델타(message.delta 등)는 저장하지 않는다.
    items: list[RunEventOut]
    next_after: int | None = None


class RunResult(BaseModel):
    """blocking 응답. waiting_input이면 run.pending_interaction에 승인·질문 내용이 있다."""

    run: RunOut
    # 마지막 assistant 메시지의 글.
    answer: str
    message_id: uuid.UUID | None
    # 이 런이 바꾼 파일.
    files: list[SessionFileOut]


class RunInput(BaseModel):
    """POST /runs/{id}/input. 질문에는 answer, 승인에는 decision을 보낸다."""

    model_config = ConfigDict(extra="forbid")

    answer: Annotated[str, StringConstraints(max_length=20_000)] | None = None
    decision: Literal["approve", "reject"] | None = None
    # 거절 사유. 모델에 그대로 전달된다.
    note: Annotated[str, StringConstraints(max_length=2000)] | None = None
    # 승인하면서 켜면 이 세션의 이후 도구 호출은 승인 없이 실행된다(session.approval_override=off).
    session_auto_approve: bool = False
    # none(기본): 바로 RunOut(202). 화면은 GET /sessions/{sid}/events?after=<마지막 seq>로 다시
    # 붙는다. streaming: 재개된 런의 SSE(첫 이벤트 interaction.resolved), blocking: 끝날 때까지
    # 기다린 결과.
    response_mode: Literal["none", "streaming", "blocking"] = "none"


class SessionDetail(SessionOut):
    # 세션 스키마가 런 스키마를 쓰므로 순환 import를 피해 여기에 둔다.
    # active_run이 있으면 화면은 GET /sessions/{sid}/events?after=0으로 다시 구독한다(7.4).
    active_run: RunOut | None
    last_run: RunOut | None
    workspace: WorkspaceInstanceOut | None
