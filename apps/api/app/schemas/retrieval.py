"""검색 요청과 결과의 모양(3단계 설계 7장, 8.1, 8.5, 12.1).

결과 항목은 Dify 인용 API의 RetrieverResource 필드명을 따르고, 검색 시험·datasets retrieve 응답은
Dify retrieve의 records 모양에 결합 내역(fusion)과 문서 색인 상태를 더한다. 검색 설정
(RetrievalSettings)은 KB 설정과 같은 모양이라 app/schemas/knowledge.py에 있다.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StringConstraints,
    field_validator,
)

from app.schemas.knowledge import FieldName, IndexingStatus, MetadataType, RetrievalSettings

# Dify API의 comparison_operator 이름이다. 설계 7.5와 Dify 화면의 이름(starts with, is empty 등)과
# ASCII 비교 기호는 OPERATOR_ALIASES로 받아 이 이름으로 바꾼다.
ComparisonOperator = Literal[
    "is",
    "is not",
    "contains",
    "not contains",
    "start with",
    "end with",
    "empty",
    "not empty",
    "in",
    "not in",
    "=",
    "≠",
    ">",
    "<",
    "≥",
    "≤",
    "before",
    "after",
]
OPERATOR_ALIASES: dict[str, ComparisonOperator] = {
    "starts with": "start with",
    "ends with": "end with",
    "is empty": "empty",
    "is not empty": "not empty",
    "==": "=",
    "!=": "≠",
    ">=": "≥",
    "<=": "≤",
}
LogicalOperator = Literal["and", "or"]
FilterMode = Literal["disabled", "manual", "automatic"]
QuerySource = Literal["hit_test", "node", "tool", "api", "eval"]
# 검색 경고. 노드는 "code: message" 글로 남긴다(8.1).
RetrievalWarningCode = Literal[
    "partial_index",
    "mixed_kb_rrf",
    "rerank_failed",
    "filter_ignored",
    "vector_unavailable",
    "query_truncated",
]
WARN_PARTIAL_INDEX: RetrievalWarningCode = "partial_index"
WARN_MIXED_KB_RRF: RetrievalWarningCode = "mixed_kb_rrf"
WARN_RERANK_FAILED: RetrievalWarningCode = "rerank_failed"
WARN_FILTER_IGNORED: RetrievalWarningCode = "filter_ignored"
WARN_VECTOR_UNAVAILABLE: RetrievalWarningCode = "vector_unavailable"
WARN_QUERY_TRUNCATED: RetrievalWarningCode = "query_truncated"

MAX_CONDITIONS = 20
MAX_LIST_VALUES = 100
MAX_VALUE_CHARS = 1000
# 검색 시험과 datasets retrieve의 질의 상한(Dify와 같다). 노드·도구는 서비스를 바로 부른다.
MAX_QUERY_CHARS = 250
MAX_EVAL_QUESTION_CHARS = 1000

ConditionText = Annotated[str, StringConstraints(strict=True, max_length=MAX_VALUE_CHARS)]
ConditionScalar = ConditionText | StrictInt | StrictFloat
QueryText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_QUERY_CHARS)
]


def canonical_operator(value: str) -> str:
    key = " ".join(value.strip().lower().split())
    return OPERATOR_ALIASES.get(key, key)


class MetadataCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: FieldName
    comparison_operator: ComparisonOperator
    # string은 글, number는 수, time은 시간대가 있는 ISO 8601 글이나 Unix 초. in·not in은 목록이나
    # 쉼표로 이은 글. empty·not empty는 값을 보지 않는다.
    value: ConditionScalar | list[ConditionScalar] | None = None

    @field_validator("comparison_operator", mode="before")
    @classmethod
    def _alias(cls, value: object) -> object:
        return canonical_operator(value) if isinstance(value, str) else value

    @field_validator("value")
    @classmethod
    def _list_size(cls, value: object) -> object:
        if isinstance(value, list) and len(value) > MAX_LIST_VALUES:
            raise ValueError(f"목록 값은 {MAX_LIST_VALUES}개까지다")
        return value


class FilterModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 비우면 폴백 체인의 첫 모델.
    model_id: str | None = None


class MetadataFilter(BaseModel):
    """메타데이터 필터(7.5). Dify의 metadata_filtering_conditions도 이 모양으로 읽는다.

    manual은 conditions를 그대로 쓰고, automatic은 모델이 질의에서 조건을 만든다(확장). 자동 필터가
    실패하면 필터 없이 검색하고 경고 filter_ignored를 남긴다.
    """

    model_config = ConfigDict(extra="forbid")

    mode: FilterMode = "manual"
    logical_operator: LogicalOperator = "and"
    conditions: list[MetadataCondition] = Field(
        default_factory=list[MetadataCondition], max_length=MAX_CONDITIONS
    )
    model: FilterModel = Field(default_factory=FilterModel)

    @field_validator("logical_operator", mode="before")
    @classmethod
    def _lower(cls, value: object) -> object:
        # 모델(자동 필터)과 Dify 클라이언트가 AND·Or처럼 보내기도 한다.
        return value.strip().lower() if isinstance(value, str) else value


class FusionDetail(BaseModel):
    """결합 점수의 내역. 검색 시험 화면이 벡터·전문 기여와 코사인 원값을 보여 준다."""

    # 벡터 후보에 없으면 null.
    cosine: float | None = None
    # 문턱을 넘은 벡터 후보 안의 min-max 점수.
    vector_score: float | None = None
    # bm25를 그 질의의 전문 후보 최대값으로 나눈 값.
    keyword_score: float | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None


class ChildChunkHit(BaseModel):
    id: uuid.UUID
    content: str
    position: int
    score: float


class RetrieverResource(BaseModel):
    """검색 결과 항목 하나. Dify 인용 API(RetrieverResource)의 필드명을 쓴다(8.1, 8.3).

    노드 출력의 metadata, 메시지의 retriever_resources, 도구 결과가 같은 모양이다.
    """

    # 1부터의 순위.
    position: int
    dataset_id: uuid.UUID
    dataset_name: str
    document_id: uuid.UUID
    document_name: str
    data_source_type: str
    segment_id: uuid.UUID
    segment_position: int
    score: float
    hit_count: int
    word_count: int
    # Parent-child는 부모 청크의 글이다.
    content: str
    # Q&A 문서의 답.
    answer: str | None = None
    # 필드 이름 → 값.
    doc_metadata: dict[str, Any] = Field(default_factory=dict[str, Any])
    # Parent-child에서 맞은 자식 청크.
    child_chunks: list[ChildChunkHit] | None = None
    fusion: FusionDetail | None = None
    # LLM 재순위나 KB 사이 RRF 병합으로 score가 바뀌었을 때 원래 결합 점수.
    fusion_score: float | None = None
    # 색인이 끝나지 않은 문서도 전문 검색에 잡힌다(6.7).
    indexing_status: IndexingStatus


class RetrievalWarning(BaseModel):
    code: RetrievalWarningCode
    message: str
    kb_ids: list[uuid.UUID] = Field(default_factory=list[uuid.UUID])
    # 일일 한도처럼 기다리면 풀리는 실패면 남은 초.
    retry_after: float | None = None


class RetrieveRequest(BaseModel):
    """검색 시험(7.7). 저장하지 않는 임시 설정으로 돈다."""

    model_config = ConfigDict(extra="forbid")

    query: QueryText
    # 비우면 KB의 검색 설정.
    retrieval_model: RetrievalSettings | None = None
    metadata_filter: MetadataFilter | None = None


class RetrieveQuery(BaseModel):
    content: str


class RetrieveDocument(BaseModel):
    id: uuid.UUID
    data_source_type: str
    name: str
    doc_metadata: dict[str, Any] = Field(default_factory=dict[str, Any])
    indexing_status: IndexingStatus


class RetrieveSegment(BaseModel):
    id: uuid.UUID
    position: int
    document_id: uuid.UUID
    content: str
    answer: str | None
    word_count: int
    tokens: int
    hit_count: int
    enabled: bool
    status: str
    document: RetrieveDocument


class RetrieveRecord(BaseModel):
    segment: RetrieveSegment
    child_chunks: list[ChildChunkHit] | None = None
    score: float
    fusion: FusionDetail | None = None
    fusion_score: float | None = None


class RetrieveResponse(BaseModel):
    """Dify retrieve 응답 모양(8.5)에 fusion·indexing_status·warnings를 더했다."""

    query: RetrieveQuery
    records: list[RetrieveRecord]
    warnings: list[RetrievalWarning] = Field(default_factory=list[RetrievalWarning])
    latency_ms: int = 0


class QueryLogResult(BaseModel):
    segment_id: uuid.UUID
    score: float


class QueryLogOut(BaseModel):
    id: uuid.UUID
    source: QuerySource
    query: str
    settings: dict[str, Any]
    # 상위 20개.
    results: list[QueryLogResult]
    result_count: int
    latency_ms: int
    created_at: datetime


class QueryLogList(BaseModel):
    items: list[QueryLogOut]
    total: int
    page: int
    limit: int
    has_more: bool


class EvalExpected(BaseModel):
    # 문서 단위 정답이면 그 문서의 어느 청크든 맞은 것으로 본다(7.7).
    document_ids: list[uuid.UUID] = Field(default_factory=list[uuid.UUID])
    segment_ids: list[uuid.UUID] = Field(default_factory=list[uuid.UUID])


class EvalCaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_EVAL_QUESTION_CHARS),
    ]
    expected: EvalExpected
    note: Annotated[str, StringConstraints(max_length=1000)] | None = None


class EvalCaseOut(BaseModel):
    id: uuid.UUID
    question: str
    expected: EvalExpected
    note: str | None
    created_at: datetime


class EvalCaseList(BaseModel):
    items: list[EvalCaseOut]
    total: int


class EvalImportRequest(BaseModel):
    """평가셋 CSV(question, expected_document_name 또는 expected_segment_id, note)."""

    model_config = ConfigDict(extra="forbid")

    csv: str
    # 참이면 지금 평가셋을 지우고 새로 넣는다.
    replace: bool = False


class EvalImportProblem(BaseModel):
    # CSV의 줄 번호(머리글이 1).
    line: int
    message: str


class EvalImportResult(BaseModel):
    created: int
    problems: list[EvalImportProblem]
    total: int


class EvalRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 비우면 KB의 검색 설정.
    retrieval_model: RetrievalSettings | None = None
    # 끄면 결과를 응답으로만 돌려준다.
    save: bool = True


class EvalMetrics(BaseModel):
    r1: float
    r3: float
    r5: float
    mrr10: float
    cases: int


class EvalHit(BaseModel):
    segment_id: uuid.UUID
    document_id: uuid.UUID
    score: float


class EvalCaseResult(BaseModel):
    case_id: uuid.UUID
    question: str
    # 정답이 처음 나온 순위(1부터). 상위 10개 안에 없으면 null.
    rank: int | None
    top: list[EvalHit]


class EvalRunOut(BaseModel):
    # save=false면 null.
    id: uuid.UUID | None
    settings: RetrievalSettings
    metrics: EvalMetrics
    cases: list[EvalCaseResult]
    duration_ms: int
    warnings: list[RetrievalWarning] = Field(default_factory=list[RetrievalWarning])
    created_at: datetime


class EvalRunSummary(BaseModel):
    id: uuid.UUID
    settings: RetrievalSettings
    metrics: EvalMetrics
    duration_ms: int
    created_at: datetime


class EvalRunList(BaseModel):
    items: list[EvalRunSummary]


class MetadataFieldSummary(BaseModel):
    # 여러 KB에 같은 이름·타입으로 있는 필드(자동 필터와 노드 폼이 쓴다).
    name: str
    type: MetadataType
