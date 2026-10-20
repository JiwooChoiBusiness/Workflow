"""워크플로우 런·노드 런 콘솔 API(2단계 설계 6.1)의 모양."""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.schemas.events import (
    NodeError,
    NodeRunStatus,
    UsageTotal,
    WfPendingKind,
    WorkflowFormData,
    WorkflowKind,
)
from app.schemas.run import PendingInteractionOut, RunStatus

WfRunTrigger = Literal["preview", "webapp", "api", "single_node", "resume"]
MAX_FORM_VALUES = 100


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

    kind가 approval·question이면 agent 노드의 자식 런이 묻는 것이라 interaction으로 승인·질문 카드를
    그리고 /input에 answer·decision을 보낸다. form이면 human_input 노드의 폼이라 form으로 입력 칸을
    그리고 /input에 kind=form과 values를 보낸다.
    """

    node_id: str
    iteration_index: int | None = None
    # 승인·질문을 기다리는 자식 런(1단계 run).
    child_run_id: uuid.UUID | None = None
    interaction: PendingInteractionOut | None = None
    kind: WfPendingKind
    form: WorkflowFormData | None = None

    @model_validator(mode="before")
    @classmethod
    def _fill_kind(cls, data: Any) -> Any:
        # M19 전에 저장된 대기 런의 행에는 kind가 없다. 그때는 승인·질문뿐이었다.
        if isinstance(data, dict) and "kind" not in data:
            raw = cast(dict[str, Any], data)
            interaction = raw.get("interaction")
            if isinstance(interaction, dict):
                return {**raw, "kind": cast(dict[str, Any], interaction).get("kind")}
        return data


class WfRunInput(BaseModel):
    """POST /wf-runs/{id}/input. 승인·질문은 1단계 /runs/{id}/input과 같은 본문이고,
    human_input 폼은 kind=form과 values를 보낸다. 파일 칸 값은 이 워크플로우에 올린 파일 id(목록이면
    id 배열)다."""

    model_config = ConfigDict(extra="forbid")

    # 비우면 answer·decision 응답이다. values를 주면 form으로 본다.
    kind: Literal["approval", "question", "form"] | None = None
    answer: Annotated[str, StringConstraints(max_length=20_000)] | None = None
    decision: Literal["approve", "reject"] | None = None
    note: Annotated[str, StringConstraints(max_length=2000)] | None = None
    session_auto_approve: bool = False
    # 폼 필드 이름 → 값.
    values: dict[str, Any] | None = Field(default=None, max_length=MAX_FORM_VALUES)
    response_mode: Literal["none", "streaming", "blocking"] = "none"

    @property
    def is_form(self) -> bool:
        return self.kind == "form" or self.values is not None


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


class WfRunLogItem(WfRunOut):
    """워크스페이스 전체 런 목록(GET /wf-runs)의 항목. 워크플로우 종류는 kind와 같다."""

    workflow_name: str
    # 지운 워크플로우의 런도 로그로 남아 목록에 나온다.
    workflow_deleted: bool


class WfRunLogList(BaseModel):
    items: list[WfRunLogItem]
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
