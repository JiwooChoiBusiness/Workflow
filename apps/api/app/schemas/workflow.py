"""워크플로우 그래프(설계 3.1)와 콘솔 API(6.1)의 모양.

그래프는 wf_version.graph에 그대로 저장되고 엔진의 입력이 된다. 초안은 깨진 채로도 저장되어야
하므로 여기서는 구조만 고정한다. 노드 id 형식, 참조, 타입별 config는 app/workflow/graph.py의
검증이 Issue 목록으로 알린다.

엔진이 내는 이벤트의 data 모델은 app/schemas/events.py에 1단계 이벤트와 함께 있다(스키마 v2).
엔진 코드가 쓰는 이름은 여기서도 그대로 가져올 수 있게 다시 내보낸다.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.schemas.agent import Description, Icon, Slug
from app.schemas.agent_version import MAX_ENV_VARS, EnvVarIn
from app.schemas.events import WF_EVENT_DATA_MODELS as WF_EVENT_DATA_MODELS
from app.schemas.events import NodeError as NodeError
from app.schemas.events import NodeRunStatus as NodeRunStatus
from app.schemas.events import WorkflowEndStatus as WorkflowEndStatus
from app.schemas.events import WorkflowKind as WorkflowKind

ErrorStrategy = Literal["fail", "default", "branch"]
IssueLevel = Literal["error", "warning"]
VarType = Literal[
    "string",
    "number",
    "boolean",
    "object",
    "file",
    "array[string]",
    "array[number]",
    "array[boolean]",
    "array[object]",
    "array[file]",
    "array[any]",
    "any",
]

OUT_HANDLE = "out"
IN_HANDLE = "in"
# error.strategy=branch인 노드가 실패하면 이 핸들의 엣지만 활성이 된다.
FAIL_HANDLE = "fail"


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float = 0
    y: float = 0


class ErrorPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: ErrorStrategy = "fail"
    # strategy=default일 때 실패한 노드의 출력 대신 쓰는 값. 키는 그 노드의 출력 이름이다.
    default_outputs: dict[str, Any] = Field(default_factory=dict[str, Any])


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=0, ge=0, le=5)
    interval_ms: int = Field(default=1000, ge=0, le=10_000)


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 형식([a-z][a-z0-9_]{0,31})은 검증이 본다. 여기서 막으면 깨진 초안을 저장할 수 없다.
    id: str
    type: str
    # config 형식이 바뀌면 올리고 가져오기에 변환기를 둔다.
    version: int = Field(default=1, ge=1)
    title: str = ""
    config: dict[str, Any] = Field(default_factory=dict[str, Any])
    error: ErrorPolicy = Field(default_factory=ErrorPolicy)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    # iteration 하위 그래프에 속하면 그 iteration 노드 id.
    parent_id: str | None = None
    position: Position = Field(default_factory=Position)
    # 접힘, prestream_sources 등 화면 값. 엔진은 읽지 않는다.
    ui: dict[str, Any] = Field(default_factory=dict[str, Any])


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    # if_else는 case_<id>/else, question_classifier는 class_<id>, 오류 분기는 fail.
    source_handle: str = OUT_HANDLE
    target: str
    target_handle: str = IN_HANDLE


class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[Node] = Field(default_factory=list[Node])
    edges: list[Edge] = Field(default_factory=list[Edge])
    viewport: dict[str, Any] = Field(default_factory=dict[str, Any])


class Issue(BaseModel):
    level: IssueLevel
    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    # 노드 안의 위치. config 필드는 "cases[0].conditions[1].var"처럼, 그 밖은 "error.strategy"처럼.
    field: str | None = None


class OutputSpecOut(BaseModel):
    name: str
    type: VarType


class NodeType(BaseModel):
    """GET /api/console/node-types 항목. 캔버스 팔레트·속성 패널·변수 선택기가 쓴다."""

    type: str
    version: int
    kinds: list[WorkflowKind]
    # False면 레지스트리에 자리만 있다. 검증은 통과하고 실행하면 node_not_implemented로 끝난다.
    implemented: bool
    error_strategies: list[ErrorStrategy]
    config_schema: dict[str, Any]
    # config에 따라 출력이 달라지는 타입(start 필드, code 출력 등)은 비어 있고
    # dynamic_outputs가 참이다.
    outputs: list[OutputSpecOut]
    dynamic_outputs: bool
    # 출발 핸들이 config에서 나오는 분기 노드(if_else의 case_<id>/else).
    branching: bool
    # kinds 밖이어도 loop 하위 그래프 안에서는 둘 수 있는 앱 종류. variable_assigner는 워크플로우의
    # loop 안에서 반복 변수(<loop id>.<이름>)를 바꾼다.
    loop_kinds: list[WorkflowKind] = Field(default_factory=list[WorkflowKind])


class ToolParamOut(BaseModel):
    name: str
    # string·number·boolean. tool 노드 params의 값이 이 타입으로 맞춰진다.
    type: VarType
    label: str
    description: str
    required: bool
    default: Any = None
    # 고를 수 있는 값. null이면 자유 입력이다.
    options: list[str] | None = None
    max_length: int | None = None


class ToolSpecOut(BaseModel):
    """tool 노드가 부를 수 있는 내장 도구. config.tool에 name을 쓴다."""

    name: str
    label: str
    description: str
    params: list[ToolParamOut]
    # params를 JSON Schema로 옮긴 것(title·description·type·default·enum·maxLength·required).
    params_schema: dict[str, Any]
    # 이 도구를 고른 tool 노드의 출력.
    outputs: list[OutputSpecOut]


WorkflowName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
# blank: workflow는 start→end, chatflow는 start→answer. chat_basic은 chatflow의 start→llm→answer.
WorkflowTemplate = Literal["blank", "chat_basic"]
VarName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
MAX_CONVERSATION_VARS = 100


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: WorkflowName
    # 만든 뒤에는 바꿀 수 없다.
    kind: WorkflowKind = "workflow"
    # 비우면 이름에서 만든다. 이름이 한글뿐이면 workflow, workflow-2처럼 붙인다.
    slug: Slug | None = None
    icon: Icon | None = None
    description: Description = ""
    template: WorkflowTemplate = "blank"


class WorkflowPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: WorkflowName | None = None
    slug: Slug | None = None
    # null을 보내면 아이콘을 지운다. 보내지 않으면 그대로 둔다.
    icon: Icon | None = None
    description: Description | None = None
    webapp_enabled: bool | None = None


class WorkflowOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    icon: str | None
    description: str
    kind: WorkflowKind
    webapp_enabled: bool
    draft_version_id: uuid.UUID | None
    published_version_id: uuid.UUID | None
    published_version_number: int | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class WorkflowList(BaseModel):
    items: list[WorkflowOut]


class ConversationVar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: VarName
    type: VarType = "string"
    default: Any = None


class WorkflowLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_run_seconds: int = Field(default=1800, ge=60, le=7200)
    # 자식 런(agent 노드)을 포함한 입력+출력 토큰.
    max_total_tokens: int = Field(default=2_000_000, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)


class WebappSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    description: Description | None = None
    opening_statement: Annotated[str, StringConstraints(max_length=4000)] | None = None
    suggested_questions: list[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default_factory=list[str], max_length=10
    )


class WorkflowSettings(BaseModel):
    """wf_version.settings(4.1)."""

    model_config = ConfigDict(extra="forbid")

    limits: WorkflowLimits = Field(default_factory=WorkflowLimits)
    # inherit: agent 노드가 부르는 에이전트 발행 버전의 승인 모드. off: 전부 자동 승인(9.1).
    approval_mode: Literal["inherit", "off"] = "inherit"
    # end 출력·answer 글에도 비밀 env 값 마스킹을 건다(9.4).
    mask_secrets_in_outputs: bool = True
    webapp: WebappSettings = Field(default_factory=WebappSettings)


class StoredWfEnvVar(BaseModel):
    # wf_version.env_vars JSON 항목(4.1). 일반 값은 value, 비밀값은 value_encrypted에만 있다.
    name: str
    secret: bool = False
    value: str | None = None
    value_encrypted: str | None = None


class WfEnvVarOut(BaseModel):
    name: str
    secret: bool
    # 비밀값은 항상 null이다. 값이 저장되어 있는지는 has_value로 본다.
    value: str | None
    has_value: bool


def _unique_names(names: list[str], what: str) -> None:
    if len(set(names)) != len(names):
        raise ValueError(f"{what}에 같은 이름이 두 번 들어 있다")


class WorkflowDraftUpdate(BaseModel):
    """PUT .../draft 본문. 초안 전체를 한 번에 바꾼다. 검증 오류가 있어도 저장한다(3.1)."""

    model_config = ConfigDict(extra="forbid")

    graph: Graph
    # value가 null이면 같은 이름으로 저장된 값을 그대로 둔다. 비밀값은 응답에 없으므로 화면은
    # 보내지 않는다.
    env_vars: list[EnvVarIn] = Field(default_factory=list[EnvVarIn], max_length=MAX_ENV_VARS)
    # 챗플로우만. 워크플로우는 비워 둔다.
    conversation_vars: list[ConversationVar] = Field(
        default_factory=list[ConversationVar], max_length=MAX_CONVERSATION_VARS
    )
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)
    # 낙관적 잠금. GET .../draft에서 받은 updated_at을 그대로 보낸다. 다르면 409 draft_conflict.
    updated_at: AwareDatetime

    @field_validator("env_vars")
    @classmethod
    def _unique_env(cls, value: list[EnvVarIn]) -> list[EnvVarIn]:
        _unique_names([item.name for item in value], "env_vars")
        return value

    @field_validator("conversation_vars")
    @classmethod
    def _unique_conversation(cls, value: list[ConversationVar]) -> list[ConversationVar]:
        _unique_names([item.name for item in value], "conversation_vars")
        return value


class WorkflowDraftCheck(BaseModel):
    """POST .../draft/validate 본문. 저장하지 않은 편집 상태를 검증한다.

    env_vars·conversation_vars를 보내지 않으면 저장된 초안의 이름을 쓴다.
    """

    model_config = ConfigDict(extra="forbid")

    graph: Graph
    env_vars: list[EnvVarIn] | None = Field(default=None, max_length=MAX_ENV_VARS)
    conversation_vars: list[ConversationVar] | None = Field(
        default=None, max_length=MAX_CONVERSATION_VARS
    )


class ValidationOut(BaseModel):
    # 오류(level=error)가 없으면 참이다. 경고만 있으면 실행·발행할 수 있다.
    ok: bool
    issues: list[Issue]


class WorkflowVersionOut(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    kind: WorkflowKind
    # 초안은 null, 발행 버전은 1부터.
    version_number: int | None
    status: Literal["draft", "published"]
    graph: Graph
    env_vars: list[WfEnvVarOut]
    conversation_vars: list[ConversationVar]
    settings: WorkflowSettings
    graph_hash: str
    published_at: datetime | None
    change_note: str | None
    # 지금 레지스트리로 다시 검증한 결과.
    issues: list[Issue]
    created_at: datetime
    # 초안 저장(PUT)의 낙관적 잠금 값.
    updated_at: datetime


class WorkflowVersionSummary(BaseModel):
    id: uuid.UUID
    version_number: int
    graph_hash: str
    published_at: datetime | None
    change_note: str | None
    # workflow.published_version_id가 이 버전인지.
    is_current: bool
    created_at: datetime


class WorkflowVersionList(BaseModel):
    items: list[WorkflowVersionSummary]


class WorkflowPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_note: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = None


class WorkflowPublishOut(BaseModel):
    workflow: WorkflowOut
    version: WorkflowVersionOut


class NodeTypeList(BaseModel):
    items: list[NodeType]
    # tool 노드의 내장 도구.
    tools: list[ToolSpecOut] = Field(default_factory=list[ToolSpecOut])


class WorkflowImportRequest(BaseModel):
    """POST /api/console/workflows/import의 JSON 본문. 파일은 multipart의 file로도 보낼 수 있다."""

    model_config = ConfigDict(extra="forbid")

    yaml: str


class DslWarning(BaseModel):
    """가져오면서 바꾸거나 버린 것. 검증 오류(issues)와 달리 실행을 막지 않는다."""

    code: str
    message: str
    node_id: str | None = None


class DslImportSummary(BaseModel):
    # dsl은 우리 형식, dify는 Dify DSL을 바꿔 들인 것이다.
    source: Literal["dsl", "dify"]
    nodes: int
    # 옮기지 못해 unsupported 자리로 들어온 노드 id. issues에도 unsupported_node 오류로 나온다.
    unsupported: list[str]
    warnings: list[DslWarning]


class WorkflowImportOut(BaseModel):
    workflow_id: uuid.UUID
    # 저장한 초안을 검증한 결과. GET .../draft의 issues와 같다.
    issues: list[Issue]
    converted: DslImportSummary


class WfFileOut(BaseModel):
    id: uuid.UUID
    kind: Literal["upload", "output"]
    workflow_id: uuid.UUID | None
    wf_run_id: uuid.UUID | None
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    created_at: datetime
