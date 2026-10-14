import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.common import ApprovalMode
from app.schemas.events import UsageTotal, WorkflowKind
from app.schemas.run import RunStatus
from app.schemas.wf_run import WfPendingOut, WfRunErrorOut
from app.schemas.workflow import WebappSettings


class WebAppOut(BaseModel):
    """웹앱(/apps/<slug>)이 보여 주는 발행 에이전트 정보(6.3)."""

    agent_id: uuid.UUID
    slug: str
    name: str
    icon: str | None
    description: str
    version_id: uuid.UUID
    version_number: int
    published_at: datetime | None
    # 발행 버전의 승인 모드. 화면이 채팅 헤더의 칩으로 보여 준다.
    approval_mode: ApprovalMode


class FlowField(BaseModel):
    """발행 버전 start 노드의 입력 필드 하나. 웹앱이 이것으로 입력 폼을 만든다."""

    name: str
    type: Literal["string", "number", "boolean", "select", "file", "file_list", "paragraph"]
    label: str
    required: bool
    default: Any = None
    options: list[str] | None = None
    max_length: int | None = None


class FlowAppOut(BaseModel):
    """웹앱(/flows/<slug>)이 보여 주는 발행 워크플로우 정보(2단계 설계 6.2)."""

    workflow_id: uuid.UUID
    slug: str
    name: str
    icon: str | None
    description: str
    kind: WorkflowKind
    version_id: uuid.UUID
    version_number: int
    published_at: datetime | None
    fields: list[FlowField]
    # 발행 버전의 settings.webapp(제목·설명·시작 문구·추천 질문).
    webapp: WebappSettings


class FlowRunResult(BaseModel):
    """웹앱 런의 blocking 응답. 입력을 기다리며 멈췄으면 pending_interaction에 승인·질문이 있다."""

    run_id: uuid.UUID
    status: RunStatus
    # end 출력을 합친 전문. succeeded가 아니면 null.
    outputs: dict[str, Any] | None
    usage: UsageTotal
    error: WfRunErrorOut | None
    pending_interaction: WfPendingOut | None
