"""과업 런의 run.input·run.output 구조(설계 4.10, 13.2).

run.input은 과업을 만들 때(app/services/tasks.py) 채우고, 러너가 작업공간 task/를 채우고
submit_result를 붙일 때(app/services/run_executor.py) 읽는다. correlation에는 2단계 워크플로우
런·노드 id가 들어간다.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.events import UsageTotal
from app.schemas.run import PendingInteractionOut, RunErrorOut, RunStatus

SessionMode = Literal["ephemeral", "reuse"]
TASK_INPUTS_PATH = "task/inputs.json"
TASK_FILES_DIR = "task/files"


class TaskFile(BaseModel):
    session_file_id: uuid.UUID
    # 작업공간 상대 경로(task/files/<이름>).
    path: str
    filename: str
    mime: str
    size_bytes: int
    storage_key: str


class TaskRunInput(BaseModel):
    # 템플릿 원문. 모델에는 채운 글이 첫 사용자 메시지로 간다.
    instruction: str
    # task/inputs.json의 내용. 파일 입력({"file_id": ...})은 작업공간 경로 문자열로 바뀌어 있다.
    inputs: dict[str, Any] = Field(default_factory=dict[str, Any])
    output_schema: dict[str, Any] | None = None
    files: list[TaskFile] = Field(default_factory=list[TaskFile])
    session_mode: SessionMode = "ephemeral"
    correlation: dict[str, Any] = Field(default_factory=dict[str, Any])


class SessionFileRef(BaseModel):
    """run.output.files 항목. 내려받기는 서비스 API /files/{id}/download."""

    id: uuid.UUID
    # 작업공간 상대 경로.
    path: str
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    downloadable: bool


class AgentTaskOut(BaseModel):
    """과업 런의 지금 상태(GET /api/v1/agent-tasks/{run_id}, blocking 응답).

    끝났으면 run.output 그대로이고, 진행 중·입력 대기면 지금까지의 글과 사용량이다.
    """

    run_id: uuid.UUID
    session_id: uuid.UUID
    status: RunStatus
    # 출력 스키마 검증을 통과한 결과. 스키마가 없거나 실패·취소면 null.
    result: dict[str, Any] | None = None
    text: str = ""
    files: list[SessionFileRef] = Field(default_factory=list[SessionFileRef])
    usage: UsageTotal = Field(default_factory=UsageTotal)
    error: RunErrorOut | None = None
    pending_interaction: PendingInteractionOut | None = None
