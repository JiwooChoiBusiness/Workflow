"""서비스 API(/api/v1) 요청·응답(설계 6.4, 2단계 설계 6.3).

에이전트 키에서 대화(conversation)는 source=api 세션이고, 과업은 source=task 세션의 런이다. user는
모든 요청에서 선택이며 주면 session.external_user에 남고 그 사용자의 대화만 보인다.

워크플로우 키는 같은 경로를 쓰되 대상에 맞는 모양(Flow*)으로 답한다. 챗플로우 대화는
wf_conversation, 메시지는 wf_message이고 블록 대신 text다.

지식 베이스 키는 datasets 경로만 쓴다(3단계 8.5). 목록은 Dify처럼 {data, has_more, limit, total,
page}로 감싼다.

인용(retriever_resources, 3단계 12.2)은 Dify와 같은 자리에 싣는다. chat-messages blocking 응답은
metadata.retriever_resources, 메시지 목록은 항목의 retriever_resources, workflows/run은 결과의
retriever_resources다.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)

from app.schemas.citations import RetrieverResourceData
from app.schemas.common import ApprovalMode, RunLimits
from app.schemas.events import UsageTotal, WorkflowKind
from app.schemas.knowledge import (
    ChildChunkOut,
    DocForm,
    DocumentName,
    DocumentStatusSkip,
    IndexingStatus,
    IndexingTechnique,
    KbDocumentOut,
    ProcessRule,
    RetrievalSettings,
    SegmentCreate,
    SegmentOut,
    SegmentPatch,
)
from app.schemas.retrieval import MetadataFilter, QueryText
from app.schemas.run import PendingInteractionOut, RunErrorOut, RunOut, RunStatus
from app.schemas.session import (
    MAX_MESSAGE_CHARS,
    MAX_MESSAGE_FILES,
    MessageOut,
    SessionFileOut,
    SessionTitle,
)
from app.schemas.task import SessionMode
from app.schemas.webapp import FlowField
from app.schemas.wf_conversation import WfMessageOut
from app.schemas.wf_run import WfPendingOut, WfRunErrorOut, WfRunOut
from app.schemas.workflow import WorkflowLimits

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


class MessageMetadataOut(BaseModel):
    """chat-messages blocking 응답의 metadata(Dify와 같은 자리)."""

    retriever_resources: list[RetrieverResourceData] = Field(
        default_factory=list[RetrieverResourceData]
    )


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
    # 에이전트가 search_knowledge로 찾은 인용.
    metadata: MessageMetadataOut = Field(default_factory=MessageMetadataOut)


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


class ApiFlowRunInput(ApiRunInput):
    """워크플로우·챗플로우 런의 /input. 승인·질문 응답에 더해 human_input 폼 응답(kind=form,
    values)을 받는다. 파일 칸 값은 /files/upload로 올린 id다. 에이전트 키로 form을 보내면 422다."""

    kind: Literal["approval", "question", "form"] | None = None
    values: dict[str, Any] | None = Field(default=None, max_length=MAX_INPUTS)

    @property
    def is_form(self) -> bool:
        return self.kind == "form" or self.values is not None


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


class WorkflowRunRequest(BaseModel):
    """POST /workflows/run. 파일 입력은 start의 file 필드에 /files/upload로 올린 id를 준다."""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(default_factory=dict[str, Any], max_length=MAX_INPUTS)
    user: ExternalUser | None = None
    response_mode: Literal["streaming", "blocking"] = "streaming"


class WorkflowRunResult(BaseModel):
    """워크플로우 런의 blocking 응답이자 폴링 결과. 입력을 기다리면 pending_interaction이 있다."""

    workflow_run_id: uuid.UUID
    status: RunStatus
    # end 출력을 합친 전문. succeeded가 아니면 null.
    outputs: dict[str, Any] | None
    usage: UsageTotal
    # 시작부터 끝(아직이면 지금)까지의 벽시계 시간. 입력 대기 시간도 들어간다.
    elapsed_ms: int
    error: WfRunErrorOut | None = None
    pending_interaction: WfPendingOut | None = None
    # 챗플로우 런이면 대화와 그 턴의 assistant 메시지.
    conversation_id: uuid.UUID | None = None
    message_id: uuid.UUID | None = None
    created_at: datetime
    ended_at: datetime | None = None
    # context를 쓴 llm 노드의 인용(3단계 8.3). 런 목록(/logs)에는 싣지 않는다.
    retriever_resources: list[RetrieverResourceData] = Field(
        default_factory=list[RetrieverResourceData]
    )


class WorkflowRunList(BaseModel):
    # 최근 것부터. 다음 쪽이 있으면 cursor로 다시 부른다.
    items: list[WorkflowRunResult]
    next_cursor: str | None = None


class FlowChatMessageResult(BaseModel):
    """챗플로우 키의 chat-messages blocking 응답. 1단계 ChatMessageResult와 같은 이름을 쓴다."""

    conversation_id: uuid.UUID
    # 이 턴의 assistant 메시지.
    message_id: uuid.UUID | None
    # 워크플로우 런. 중지·응답·이벤트는 /chat-messages/{run_id}/...로 부른다.
    run_id: uuid.UUID
    answer: str
    usage: UsageTotal
    status: RunStatus
    pending_interaction: WfPendingOut | None = None
    error: WfRunErrorOut | None = None
    # context를 쓴 llm 노드의 인용.
    metadata: MessageMetadataOut = Field(default_factory=MessageMetadataOut)


class FlowConversationOut(BaseModel):
    id: uuid.UUID
    name: str | None
    external_user: str | None
    wf_version_id: uuid.UUID
    message_count: int
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FlowConversationDetail(FlowConversationOut):
    # 있으면 GET /chat-messages/{active_run.id}/events로 다시 붙는다.
    active_run: WfRunOut | None
    last_run: WfRunOut | None


class FlowConversationList(BaseModel):
    items: list[FlowConversationOut]
    next_cursor: str | None = None


class FlowApiMessageList(BaseModel):
    # seq 오름차순. 더 앞의 메시지는 cursor=next_cursor로 다시 부른다.
    items: list[WfMessageOut]
    has_more: bool
    next_cursor: str | None = None


class FlowFileUploadOut(BaseModel):
    file_id: uuid.UUID
    filename: str
    size: int
    mime: str
    # conversation_id를 주고 올린 챗플로우 파일만 그 대화를 가리킨다.
    conversation_id: uuid.UUID | None


class FlowInfoOut(BaseModel):
    workflow_id: uuid.UUID
    slug: str
    name: str
    icon: str | None
    description: str
    kind: WorkflowKind
    published: bool
    version_id: uuid.UUID | None
    version_number: int | None
    published_at: datetime | None
    webapp_enabled: bool


class FlowParametersOut(BaseModel):
    kind: WorkflowKind
    # 발행 버전 start 노드의 입력 필드. inputs에 이 이름으로 준다. 발행 전이면 빈 목록이다.
    user_input_form: list[FlowField]
    limits: WorkflowLimits | None
    file_upload: FileUploadLimits
    response_modes: list[str]
    scopes: list[str]
    # 챗플로우 웹앱 설정(settings.webapp)의 시작 문구와 추천 질문.
    opening_statement: str | None = None
    suggested_questions: list[str] = Field(default_factory=list[str])


class DatasetOut(BaseModel):
    """지식 베이스 하나(Dify datasets 필드명)."""

    id: uuid.UUID
    name: str
    description: str
    indexing_technique: IndexingTechnique
    doc_form: DocForm
    # economy면 null.
    embedding_model: str | None
    embedding_model_provider: str | None
    retrieval_model: RetrievalSettings
    document_count: int
    # 지우지 않은 문서의 글자 수 합.
    word_count: int
    created_at: datetime
    updated_at: datetime


class DatasetList(BaseModel):
    data: list[DatasetOut]
    has_more: bool
    limit: int
    total: int
    page: int


DocMetadataValues = Annotated[
    dict[str, StrictInt | StrictFloat | StrictStr | None], Field(max_length=50)
]


class DatasetTextCreate(BaseModel):
    # Dify 요청의 indexing_technique·doc_form·embedding_model은 KB 설정을 따르므로 받기만 한다.
    model_config = ConfigDict(extra="ignore")

    name: DocumentName
    text: Annotated[str, StringConstraints(min_length=1)]
    # 비우면 KB의 규칙.
    process_rule: ProcessRule | None = None
    # 메타데이터 필드 이름 → 값. 필드는 KB에 먼저 있어야 한다.
    doc_metadata: DocMetadataValues | None = None
    allow_duplicate: bool = False


class DatasetFileOptions(BaseModel):
    """create-by-file 멀티파트의 data 칸(JSON 글)."""

    model_config = ConfigDict(extra="ignore")

    process_rule: ProcessRule | None = None
    doc_metadata: DocMetadataValues | None = None
    allow_duplicate: bool = False


class DatasetTextUpdate(BaseModel):
    """update-by-text. 원본을 새 글로 바꾸고 다시 색인한다. 이름을 비우면 지금 이름 그대로다."""

    model_config = ConfigDict(extra="ignore")

    name: DocumentName | None = None
    text: Annotated[str, StringConstraints(min_length=1)]
    process_rule: ProcessRule | None = None
    allow_duplicate: bool = False


class DatasetFileUpdateOptions(BaseModel):
    """update-by-file 멀티파트의 data 칸(JSON 글)."""

    model_config = ConfigDict(extra="ignore")

    name: DocumentName | None = None
    process_rule: ProcessRule | None = None
    allow_duplicate: bool = False


class DatasetDocumentCreated(BaseModel):
    # update-by-*도 같은 모양이다. batch는 문서가 처음 들어온 묶음 그대로다.
    document: KbDocumentOut
    batch: str


class DatasetDocumentList(BaseModel):
    data: list[KbDocumentOut]
    has_more: bool
    limit: int
    total: int
    page: int


class DatasetIndexingStatusItem(BaseModel):
    id: uuid.UUID
    indexing_status: IndexingStatus
    processing_started_at: datetime | None
    parsing_completed_at: datetime | None
    cleaning_completed_at: datetime | None
    splitting_completed_at: datetime | None
    completed_at: datetime | None
    paused_at: datetime | None
    stopped_at: datetime | None
    # {code, message, retryable, retry_after}.
    error: dict[str, Any] | None
    completed_segments: int
    total_segments: int


class DatasetIndexingStatus(BaseModel):
    data: list[DatasetIndexingStatusItem]


class DatasetStatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class DatasetStatusResult(BaseModel):
    result: Literal["success"] = "success"
    # 맞지 않는 상태라 건너뛴 문서와 사유.
    skipped: list[DocumentStatusSkip] = Field(default_factory=list[DocumentStatusSkip])


class DatasetSegmentList(BaseModel):
    data: list[SegmentOut]
    doc_form: DocForm
    has_more: bool
    limit: int
    total: int
    page: int


class DatasetSegmentsCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[SegmentCreate] = Field(min_length=1, max_length=1000)


class DatasetSegmentsCreated(BaseModel):
    data: list[SegmentOut]
    doc_form: DocForm


class DatasetSegmentOut(BaseModel):
    data: SegmentOut
    doc_form: DocForm


class DatasetSegmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment: SegmentPatch


class DatasetChildChunkList(BaseModel):
    data: list[ChildChunkOut]
    total: int


class DatasetChildChunkOut(BaseModel):
    data: ChildChunkOut


class DifyMetadataValue(BaseModel):
    """Dify operation_data의 metadata_list 항목. id가 없으면 name으로 필드를 찾는다."""

    model_config = ConfigDict(extra="ignore")

    id: uuid.UUID | None = None
    name: str | None = None
    value: StrictInt | StrictFloat | StrictStr | None = None


class DifyDocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    document_id: uuid.UUID
    metadata_list: list[DifyMetadataValue] = Field(
        default_factory=list[DifyMetadataValue], max_length=50
    )
    # 거짓(기본)이면 Dify처럼 목록에 없는 사용자 필드 값을 지운다. 참이면 준 필드만 바꾼다.
    partial_update: bool = False


class DifyDocumentMetadataUpdate(BaseModel):
    """POST .../documents/metadata의 Dify 본문({operation_data: [...]}). 기본 필드는 건너뛴다."""

    model_config = ConfigDict(extra="forbid")

    operation_data: list[DifyDocumentMetadata] = Field(min_length=1, max_length=100)


def dify_retrieval(raw: dict[str, Any]) -> dict[str, Any]:
    """Dify의 retrieval_model을 우리 모양으로. 우리 모양이면 그대로 둔다.

    reranking_model(재순위 모델)은 reranking_enable이 true일 때만 우리 LLM 재순위로 읽는다. KB
    키 하나로 채팅 호출을 쓰는 길이라 명시했을 때만 연다. weights.vector_setting·keyword_setting은
    vector_weight·keyword_weight로, keyword_search는 전문 검색으로, score_threshold null은 문턱
    끔으로 읽는다. 쓰지 않는 칸(weight_type, 임베딩 이름 등)은 버린다.
    """
    keys = ("search_method", "top_k", "score_threshold_enabled", "score_threshold", "rerank")
    out = {key: raw[key] for key in keys if raw.get(key) is not None}
    if out.get("search_method") == "keyword_search":
        out["search_method"] = "full_text_search"
    if "score_threshold" in raw and raw["score_threshold"] is None:
        out["score_threshold_enabled"] = False
    mode = raw.get("reranking_mode")
    if mode == "reranking_model":
        mode = "llm" if raw.get("reranking_enable") is True else None
    if mode is not None:
        out["reranking_mode"] = mode
    weights = raw.get("weights")
    if isinstance(weights, dict):
        found = cast(dict[str, Any], weights)
        vector = found.get("vector_setting")
        keyword = found.get("keyword_setting")
        if isinstance(vector, dict) or isinstance(keyword, dict):
            vector_weight = cast(dict[str, Any], vector or {}).get("vector_weight")
            keyword_weight = cast(dict[str, Any], keyword or {}).get("keyword_weight")
            out["weights"] = {
                "vector_weight": 0.7 if vector_weight is None else vector_weight,
                "keyword_weight": 0.3 if keyword_weight is None else keyword_weight,
            }
        else:
            out["weights"] = found
    return out


class DatasetRetrieveRequest(BaseModel):
    """Dify retrieve 요청. retrieval_model은 우리 모양과 Dify 모양을 다 받는다."""

    model_config = ConfigDict(extra="forbid")

    query: QueryText
    retrieval_model: RetrievalSettings | None = None
    metadata_filtering_conditions: MetadataFilter | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_dify(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(cast(dict[str, Any], value))
        raw = data.get("retrieval_model")
        if isinstance(raw, dict):
            model = dict(cast(dict[str, Any], raw))
            # Dify는 메타데이터 조건을 retrieval_model 안에 둔다.
            conditions = model.pop("metadata_filtering_conditions", None)
            if conditions is not None and data.get("metadata_filtering_conditions") is None:
                data["metadata_filtering_conditions"] = conditions
            data["retrieval_model"] = dify_retrieval(model)
        return data
