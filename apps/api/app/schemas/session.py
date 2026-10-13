import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.sandbox.base import RunningExec
from app.schemas.events import MessageFileData

SessionSource = Literal["preview", "webapp", "api", "task"]
SessionTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
# 사용자 메시지 상한. 긴 자료는 파일로 첨부한다.
MAX_MESSAGE_CHARS = 100_000
MAX_MESSAGE_FILES = 20


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: SessionTitle | None = None


class SessionOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    source: SessionSource
    title: str | None
    status: str
    # null이면 에이전트 버전의 승인 모드, "off"면 이 세션은 자동 승인(3.10).
    approval_override: Literal["off"] | None
    external_user: str | None
    api_key_id: uuid.UUID | None
    workspace_instance_id: uuid.UUID | None
    message_count: int
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SessionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: SessionTitle | None = None
    # "off"면 자동 승인을 켜고 null이면 끈다. 보내지 않으면 그대로 둔다.
    approval_override: Literal["off"] | None = None


class SessionList(BaseModel):
    items: list[SessionOut]
    next_offset: int | None = None


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(min_length=1, max_length=MAX_MESSAGE_CHARS)]
    # POST /sessions/{sid}/uploads로 올린 파일. 런 시작 때 작업공간 uploads/<message_id>/로
    # 복사된다.
    file_ids: list[uuid.UUID] = Field(default_factory=list[uuid.UUID], max_length=MAX_MESSAGE_FILES)
    response_mode: Literal["streaming", "blocking"] = "streaming"


class MessageOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID | None
    seq: int
    # user: 사용자 입력(blocks 끝의 kind=context text 블록은 실행 정보라 화면에 보이지 않는다),
    # assistant: 스텝 하나(text·tool_use 뒤에 그 도구들의 tool_result가 붙는다),
    # system_summary: 컨텍스트 압축 요약(4.7).
    role: Literal["user", "assistant", "system_summary"]
    blocks: list[dict[str, Any]]
    text: str
    stop_reason: str | None
    # user 메시지는 첨부(upload), 런의 마지막 assistant 메시지는 그 런이 바꾼 파일(output).
    files: list[MessageFileData]
    usage: dict[str, Any] | None
    created_at: datetime


class MessageList(BaseModel):
    items: list[MessageOut]
    # 더 앞의 메시지가 있으면 before_seq=items[0].seq로 다시 부른다.
    has_more: bool


class SessionFileOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    run_id: uuid.UUID | None
    message_id: uuid.UUID | None
    kind: Literal["upload", "output"]
    # 작업공간 상대 경로.
    path: str
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    downloadable: bool
    # 보존하지 않은 이유(too_large, run_limit, copy_failed).
    reason: str | None
    created_at: datetime


class SessionFileList(BaseModel):
    items: list[SessionFileOut]


class WorkspaceInstanceOut(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    backend: str
    root_path: str
    status: Literal["ready", "busy", "archived"]
    disk_bytes: int
    disk_estimated: bool
    disk_measured_at: datetime | None
    last_activity_at: datetime
    created_at: datetime
    session_title: str | None = None
    agent_id: uuid.UUID | None = None
    session_source: SessionSource | None = None


class WorkspaceList(BaseModel):
    items: list[WorkspaceInstanceOut]


class WorkspaceDirUsage(BaseModel):
    path: str
    bytes: int


class WorkspaceStatusOut(BaseModel):
    session_id: uuid.UUID
    instance: WorkspaceInstanceOut | None
    # 작업공간 폴더가 지금 있는지. 없으면 다음 런이 새로 만든다.
    exists: bool
    disk_bytes: int | None = None
    disk_estimated: bool = False
    disk_limit_bytes: int
    # 디스크를 많이 쓰는 폴더 상위 10개(두 단계까지).
    largest_dirs: list[WorkspaceDirUsage] = Field(default_factory=list[WorkspaceDirUsage])
    running: list[RunningExec] = Field(default_factory=list[RunningExec])
    last_exec_at: datetime | None = None


class WorkspaceKillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 비우면 실행 중인 도구 프로세스를 모두 끝낸다.
    exec_id: str | None = None


class WorkspaceKillOut(BaseModel):
    killed: list[str]
