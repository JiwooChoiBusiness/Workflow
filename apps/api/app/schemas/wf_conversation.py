"""챗플로우 대화·메시지 API(2단계 설계 6.1~6.3)의 모양.

1단계 세션·메시지 API(app/schemas/session.py)와 같은 짜임이고 메시지는 블록 대신 text다. 한 턴은
user 메시지와 assistant 메시지 두 행이며 둘 다 그 턴의 워크플로우 런(run_id)을 가리킨다.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.run import RunStatus
from app.schemas.session import MAX_MESSAGE_CHARS, MAX_MESSAGE_FILES, SessionTitle
from app.schemas.wf_run import WfRunOut
from app.schemas.workflow import VarType, WfFileOut

ConversationSource = Literal["preview", "webapp", "api"]
MAX_INPUTS = 100


class WfConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: SessionTitle | None = None


class WfConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: SessionTitle


class WfConversationOut(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    # 만든 때의 발행 버전. 미리보기 대화는 초안이다.
    wf_version_id: uuid.UUID
    source: ConversationSource
    title: str | None
    status: str
    external_user: str | None
    api_key_id: uuid.UUID | None
    # user·assistant 메시지 수. 한 턴이 둘이다.
    message_count: int
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WfConversationDetail(WfConversationOut):
    # active_run이 있으면 화면은 GET .../wf-runs/{id}/events?after=0으로 다시 붙는다.
    active_run: WfRunOut | None
    last_run: WfRunOut | None


class WfConversationList(BaseModel):
    items: list[WfConversationOut]
    next_offset: int | None = None


class WfMessageCreate(BaseModel):
    """대화에 보내는 메시지. 런 하나가 이 턴의 답을 만든다."""

    model_config = ConfigDict(extra="forbid")

    # sys.query가 된다.
    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_CHARS)]
    # start 필드 이름 → 값. 비우면 이 대화의 바로 앞 메시지에 준 값을 다시 쓴다.
    inputs: dict[str, Any] = Field(default_factory=dict[str, Any], max_length=MAX_INPUTS)
    # 이 워크플로우에 올린 파일(wf_file) id. sys.files가 된다.
    files: list[uuid.UUID] = Field(default_factory=list[uuid.UUID], max_length=MAX_MESSAGE_FILES)
    response_mode: Literal["streaming", "blocking"] = "streaming"


class WfMessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    # 이 턴의 워크플로우 런.
    run_id: uuid.UUID | None
    seq: int
    role: Literal["user", "assistant"]
    # assistant는 answer 노드들의 글을 이은 것이다. 런이 끝나기 전에는 멈춘 데까지다.
    text: str
    # user 메시지에 준 start 필드 값.
    inputs: dict[str, Any] | None
    files: list[WfFileOut]
    usage: dict[str, Any] | None
    # assistant 메시지만. 런 상태를 따라간다.
    status: RunStatus | None
    created_at: datetime


class WfMessageList(BaseModel):
    items: list[WfMessageOut]
    # 더 앞의 메시지가 있으면 before_seq=items[0].seq로 다시 부른다.
    has_more: bool


class WfChatResult(BaseModel):
    """메시지 전송의 blocking 응답. 입력을 기다리면 run.pending_interaction에 승인·질문이 있다."""

    conversation_id: uuid.UUID
    # 이 턴의 assistant 메시지.
    message_id: uuid.UUID | None
    run: WfRunOut
    answer: str


class ConversationVariableOut(BaseModel):
    name: str
    type: VarType
    value: Any = None


class WfConversationVariables(BaseModel):
    # 대화가 쓰는 버전에 선언한 대화 변수의 지금 값. 아직 바뀐 적이 없으면 기본값이다.
    items: list[ConversationVariableOut]
