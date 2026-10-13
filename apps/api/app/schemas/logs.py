"""로그 화면과 작업공간 목록(설계 7.2, 7.3)이 쓰는 응답.

런·작업공간 응답에 세션·에이전트 정보를 붙인다.
"""

import uuid

from pydantic import BaseModel, Field

from app.sandbox.base import RunningExec
from app.schemas.run import RunOut, RunStatus
from app.schemas.session import SessionSource, WorkspaceInstanceOut


class RunLogItem(RunOut):
    agent_id: uuid.UUID
    agent_name: str
    agent_deleted: bool
    session_source: SessionSource
    session_title: str | None
    # 지운 세션의 런도 로그로 남는다. 이때 세션 상세·메시지 API는 404다.
    session_deleted: bool


class RunLogList(BaseModel):
    items: list[RunLogItem]
    next_offset: int | None = None


class WorkspaceOverview(WorkspaceInstanceOut):
    agent_name: str | None = None
    agent_deleted: bool = False
    # 세션에서 끝나지 않은 런(queued·running·waiting_input).
    active_run_id: uuid.UUID | None = None
    active_run_status: RunStatus | None = None
    # 지금 도는 도구 프로세스. 런이 실행 중인 작업공간만 백엔드에 묻는다.
    running: list[RunningExec] = Field(default_factory=list[RunningExec])
    # 실행 중인 도구들의 Job 메모리 최고치 중 가장 큰 값. 거부된 할당 요청도 들어가므로 화면에는
    # "최고 요청치"로 보인다(12.2 M3).
    peak_memory_bytes: int | None = None
    # 마지막으로 잰 크기가 WORKSPACE_MAX_BYTES를 넘었다. 이 세션의 새 런은 409 workspace_disk_limit.
    over_limit: bool = False


class WorkspaceOverviewList(BaseModel):
    items: list[WorkspaceOverview]
    disk_limit_bytes: int
    # 목록의 disk_bytes 합. 각 값은 그 작업공간에서 런이 끝날 때 잰 크기다.
    total_disk_bytes: int
