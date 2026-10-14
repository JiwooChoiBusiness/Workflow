"""워크플로우 런·노드 런 콘솔 API(2단계 설계 6.1)의 모양."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.events import NodeError, NodeRunStatus, UsageTotal, WorkflowKind
from app.schemas.run import PendingInteractionOut, RunStatus

WfRunTrigger = Literal["preview", "webapp", "api", "single_node", "resume"]


class WfRunCreate(BaseModel):
    """POST /workflows/{id}/draft/runs. 초안으로 미리보기 런을 만든다."""

    model_config = ConfigDict(extra="forbid")

    # start 필드 이름 → 값. 파일 필드는 올린 파일의 id(문자열) 또는 {"file_id": id}, 파일 목록은
    # 그 배열이다.
    inputs: dict[str, Any] = Field(default_factory=dict[str, Any])
    # streaming(기본): SSE. blocking: 끝나거나 입력을 기다릴 때까지 기다린 WfRunOut.
    response_mode: Literal["streaming", "blocking"] = "streaming"


class WfNodeRunCreate(BaseModel):
    """POST /workflows/{id}/draft/nodes/{node_id}/runs. 초안의 노드 하나만 돌린다(6.1)."""

    model_config = ConfigDict(extra="forbid")

    # 노드가 참조하는 변수의 값. 키는 "<scope>.<name>"(노드 id 또는 sys·conv)이다. 파일 변수에는
    # 이 워크플로우에 올린 파일 id(문자열 또는 {"file_id": id}), 파일 배열은 그 배열을 준다.
    inputs: dict[str, Any] = Field(default_factory=dict[str, Any])
    response_mode: Literal["streaming", "blocking"] = "streaming"


class WfSingleNodeInputs(BaseModel):
    """단일 노드 실행 런의 wf_run.inputs. 재개할 때 실행기가 다시 읽는다."""

    node_id: str
    values: dict[str, Any] = Field(default_factory=dict[str, Any])


class WfRunErrorOut(BaseModel):
    code: str
    message: str
    node_id: str | None = None
    retryable: bool = False


class WfPendingOut(BaseModel):
    """wf_run.pending_interaction.

    승인·질문 카드는 interaction으로 그리고 응답은 /input으로 보낸다.
    """

    node_id: str
    iteration_index: int | None = None
    # 승인·질문을 기다리는 자식 런(1단계 run).
    child_run_id: uuid.UUID | None = None
    interaction: PendingInteractionOut


class WfRunOut(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    wf_version_id: uuid.UUID
    kind: WorkflowKind
    trigger: WfRunTrigger
    status: RunStatus
    # start 필드 이름 → 값. 단일 노드 실행(trigger=single_node)은 {node_id, values}다.
    inputs: dict[str, Any]
    # end 출력을 합친 전문. succeeded가 아니면 null.
    outputs: dict[str, Any] | None
    error: WfRunErrorOut | None
    usage: UsageTotal
    node_run_count: int
    pending_interaction: WfPendingOut | None
    # 런을 만들 때 적용한 한도.
    limits: dict[str, Any]
    conversation_id: uuid.UUID | None
    message_id: uuid.UUID | None
    external_user: str | None
    cancel_requested_at: datetime | None
    queued_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WfRunList(BaseModel):
    items: list[WfRunOut]
    next_offset: int | None = None


class WfNodeRunOut(BaseModel):
    id: uuid.UUID
    wf_run_id: uuid.UUID
    node_id: str
    node_type: str
    title: str
    # 런 안 실행 순서.
    index: int
    iteration_node_id: str | None
    iteration_index: int | None
    attempt: int
    status: NodeRunStatus
    # inputs·outputs를 64KB(iteration 하위 노드는 8KB)에서 잘랐다.
    truncated: bool
    error: NodeError | None
    usage: UsageTotal | None
    warnings: list[str]
    # agent 노드가 만든 자식 런(1단계 run).
    child_run_id: uuid.UUID | None
    branch: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None


class WfNodeRunDetail(WfNodeRunOut):
    # 보존 기간이 지난 iteration 하위 노드 런은 null이다.
    inputs: dict[str, Any] | None
    outputs: dict[str, Any] | None
    # 타입별 요약(모델 id, HTTP 상태, 종료 코드 등).
    process: dict[str, Any]


class WfNodeRunList(BaseModel):
    items: list[WfNodeRunOut]


class WfRunDetail(WfRunOut):
    # 루트 그래프의 노드 런 요약(실행 순서). iteration 하위 노드 런은
    # GET .../node-runs?iteration=<node_id>로 본다.
    node_runs: list[WfNodeRunOut]
