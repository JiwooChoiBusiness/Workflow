"""지식 베이스 콘솔 API와 knowledge_base·kb_document JSON 열의 모양(3단계 설계 4.1, 6.3, 7장, 12.1).

process_rule·retrieval_model·embedding·data_policy는 knowledge_base(문서는 process_rule 스냅샷)에
이 모양 그대로 저장한다. 색인·검색 코드(app/knowledge)는 이 값을 읽어 쓴다.
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
    StrictStr,
    StringConstraints,
    model_validator,
)

from app.schemas.agent import Icon, Slug

DocForm = Literal["text_model", "hierarchical_model", "qa_model"]
IndexingTechnique = Literal["high_quality", "economy"]
KeyTier = Literal["free", "paid"]
IndexingStatus = Literal[
    "waiting", "parsing", "cleaning", "splitting", "indexing", "completed", "error", "paused"
]
# 목록 필터 값(Dify와 같은 이름, 4.4). 저장하지 않고 indexing_status·enabled·archived로 만든다.
DisplayStatus = Literal[
    "queuing", "indexing", "paused", "error", "available", "disabled", "archived"
]
DataSourceType = Literal["upload_file", "paste_text", "workflow", "api"]
ExtractMode = Literal["text", "model"]
DocumentStatusAction = Literal[
    "enable", "disable", "archive", "unarchive", "pause", "resume", "retry"
]
SegmentStatus = Literal["waiting", "indexing", "completed", "error"]
CreatedBy = Literal["automatic", "customized"]
MetadataType = Literal["string", "number", "time"]
SearchMethod = Literal["semantic_search", "full_text_search", "hybrid_search"]
RerankingMode = Literal["weighted_score", "rrf", "llm"]

KbName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
KbDescription = Annotated[str, StringConstraints(strip_whitespace=True, max_length=400)]
DocumentName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
# 구분자는 공백·개행 자체가 값이라 앞뒤를 자르지 않는다.
Separator = Annotated[str, StringConstraints(min_length=1, max_length=16)]
FieldName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)]
# 청크 하나의 본문 상한(14.3). 토큰 상한은 문서 규칙의 max_tokens로 따로 본다.
MAX_CHUNK_CHARS = 1024 * 1024
ChunkText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_CHUNK_CHARS)]
MAX_CHUNK_TOKENS = 4000


class EmbeddingSettingsIn(BaseModel):
    """색인 설정 중 사용자가 고르는 것. 1차는 gemini-embedding-2(OpenAI 호환 경로)만이다(Z4)."""

    model_config = ConfigDict(extra="forbid")

    model: Literal["gemini-embedding-2"] = "gemini-embedding-2"
    dimensions: Literal[768, 1536, 3072] = 768
    # gemini2: 문서 "title: none | text: …", 질의 "task: search result | query: …" 접두어(6.6).
    prefix: Literal["none", "gemini2"] = "gemini2"


class EmbeddingSettings(EmbeddingSettingsIn):
    """knowledge_base.embedding에 저장하는 값. version은 설정을 바꿀 때마다 서버가 올린다."""

    # 네이티브 전송 전용(001). 1차에서는 항상 null이다.
    task_type: str | None = None
    version: int = Field(default=1, ge=1)


class CleaningRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remove_extra_spaces: bool = True
    remove_urls_emails: bool = False


class GeneralChunking(BaseModel):
    """General(text_model) 청크 규칙(6.3). 단위는 로컬 추정 토큰이다."""

    model_config = ConfigDict(extra="forbid")

    separators: list[Separator] = Field(
        default_factory=lambda: ["\n\n", "\n", ". ", " "], min_length=1, max_length=10
    )
    max_tokens: int = Field(default=1024, ge=20, le=MAX_CHUNK_TOKENS)
    overlap_tokens: int = Field(default=50, ge=0)

    @model_validator(mode="after")
    def _overlap_below_max(self) -> "GeneralChunking":
        if self.overlap_tokens >= self.max_tokens:
            raise ValueError("overlap_tokens는 max_tokens보다 작아야 한다")
        return self


class ParentChunking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # full_doc이면 앞 10,000토큰이 부모 하나이고 max_tokens는 쓰지 않는다.
    mode: Literal["paragraph", "full_doc"] = "paragraph"
    separators: list[Separator] = Field(
        default_factory=lambda: ["\n\n"], min_length=1, max_length=10
    )
    max_tokens: int = Field(default=1024, ge=20, le=MAX_CHUNK_TOKENS)


class ChildChunking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    separators: list[Separator] = Field(
        default_factory=lambda: ["\n", ". "], min_length=1, max_length=10
    )
    max_tokens: int = Field(default=256, ge=20, le=MAX_CHUNK_TOKENS)


class ProcessRule(BaseModel):
    """정리·청크 규칙(6.3). 청크 모드(doc_form)에 맞는 칸만 쓰인다.

    text_model은 general, hierarchical_model은 parent·child를 쓰고 qa_model은 나누지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    cleaning: CleaningRules = Field(default_factory=CleaningRules)
    general: GeneralChunking = Field(default_factory=GeneralChunking)
    parent: ParentChunking = Field(default_factory=ParentChunking)
    child: ChildChunking = Field(default_factory=ChildChunking)

    @model_validator(mode="after")
    def _child_fits_parent(self) -> "ProcessRule":
        if self.parent.mode == "paragraph" and self.child.max_tokens > self.parent.max_tokens:
            raise ValueError("child.max_tokens는 parent.max_tokens 이하여야 한다")
        return self


class RetrievalWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vector_weight: float = Field(default=0.7, ge=0, le=1)
    keyword_weight: float = Field(default=0.3, ge=0, le=1)

    @model_validator(mode="after")
    def _normalize(self) -> "RetrievalWeights":
        # 합이 1이 되게 정규화해 저장한다(7.2).
        total = self.vector_weight + self.keyword_weight
        if total <= 0:
            raise ValueError("vector_weight와 keyword_weight 중 하나는 0보다 커야 한다")
        if abs(total - 1) > 1e-9:
            self.vector_weight = round(self.vector_weight / total, 6)
            self.keyword_weight = round(1 - self.vector_weight, 6)
        return self


class RerankSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 비우면 폴백 체인의 첫 모델.
    model_id: str | None = None
    candidates: int = Field(default=10, ge=1, le=20)


class RetrievalSettings(BaseModel):
    """검색 설정(7장). KB의 기본값이고 노드·도구·검색 시험이 같은 모양으로 덮어쓴다."""

    model_config = ConfigDict(extra="forbid")

    search_method: SearchMethod = "hybrid_search"
    # 상한은 KB_TOP_K_MAX(기본 20)이라 서비스가 따로 본다.
    top_k: int = Field(default=4, ge=1)
    # 결합 전에 코사인 원값에 거는 문턱(7.3).
    score_threshold_enabled: bool = False
    score_threshold: float = Field(default=0.5, ge=0, le=1)
    reranking_mode: RerankingMode = "weighted_score"
    weights: RetrievalWeights = Field(default_factory=RetrievalWeights)
    rerank: RerankSettings = Field(default_factory=RerankSettings)


class DataPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # paid는 GEMINI_PAID_API_KEY가 있어야 고를 수 있다.
    key_tier: KeyTier = "free"
    # 데이터 정책 안내(14.4)를 읽고 확인했는지. true여야 저장한다.
    acknowledged: bool = False


class DataPolicy(BaseModel):
    key_tier: KeyTier = "free"
    acknowledged_at: datetime | None = None


class KnowledgeBaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: KbName
    # 비우면 이름에서 만든다. 이름이 한글뿐이면 knowledge, knowledge-2처럼 붙인다.
    slug: Slug | None = None
    icon: Icon | None = None
    description: KbDescription = ""
    # 만든 뒤에는 바꿀 수 없다.
    doc_form: DocForm = "text_model"
    indexing_technique: IndexingTechnique = "high_quality"
    # high_quality에서만 준다. 비우면 기본값(gemini-embedding-2, 768, gemini2).
    embedding: EmbeddingSettingsIn | None = None
    process_rule: ProcessRule | None = None
    # 비우면 high_quality는 hybrid_search, economy는 full_text_search.
    retrieval_model: RetrievalSettings | None = None
    search_while_indexing: bool = True
    data_policy: DataPolicyIn


class KnowledgeBasePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: KbName | None = None
    slug: Slug | None = None
    # null을 보내면 아이콘을 지운다. 보내지 않으면 그대로 둔다.
    icon: Icon | None = None
    description: KbDescription | None = None
    # economy → high_quality만 된다. 문서가 모두 다시 벡터 색인을 거친다.
    indexing_technique: IndexingTechnique | None = None
    # 바꾸면 모든 청크를 다시 임베딩하므로 confirm_reembed가 참이어야 한다.
    embedding: EmbeddingSettingsIn | None = None
    confirm_reembed: bool = False
    process_rule: ProcessRule | None = None
    retrieval_model: RetrievalSettings | None = None
    search_while_indexing: bool | None = None
    data_policy: DataPolicyIn | None = None


class KnowledgeBaseOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    icon: str | None
    description: str
    doc_form: DocForm
    indexing_technique: IndexingTechnique
    embedding: EmbeddingSettings | None
    process_rule: ProcessRule
    retrieval_model: RetrievalSettings
    search_while_indexing: bool
    built_in_fields_enabled: bool
    data_policy: DataPolicy
    document_count: int
    segment_count: int
    # 대기·색인 중인 문서 수(waiting~indexing).
    indexing_document_count: int
    embedded_tokens_estimated: int
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseList(BaseModel):
    items: list[KnowledgeBaseOut]


class DocumentMetadataValue(BaseModel):
    field_id: uuid.UUID
    name: str
    type: MetadataType
    built_in: bool
    value: str | float | datetime | None


class KbDocumentOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    position: int
    name: str
    data_source_type: DataSourceType
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    indexing_status: IndexingStatus
    display_status: DisplayStatus
    enabled: bool
    archived: bool
    # {code, message, retryable, retry_after}.
    error: dict[str, Any] | None
    process_rule: ProcessRule
    # {mode, pages, chars, truncated}. 파싱 전에는 null.
    extract: dict[str, Any] | None
    total_segments: int
    completed_segments: int
    word_count: int
    tokens: int
    hit_count: int
    batch: str
    processing_started_at: datetime | None
    parsing_completed_at: datetime | None
    cleaning_completed_at: datetime | None
    splitting_completed_at: datetime | None
    completed_at: datetime | None
    paused_at: datetime | None
    stopped_at: datetime | None
    doc_metadata: list[DocumentMetadataValue] = Field(default_factory=list[DocumentMetadataValue])
    created_at: datetime
    updated_at: datetime


class KbDocumentList(BaseModel):
    items: list[KbDocumentOut]
    total: int
    page: int
    limit: int
    has_more: bool


class KbDocumentTextCreate(BaseModel):
    """붙여 넣은 글 하나를 문서로. 원본은 .txt로 저장해 재색인이 거기서 다시 돈다."""

    model_config = ConfigDict(extra="forbid")

    name: DocumentName
    text: Annotated[str, StringConstraints(min_length=1)]
    # 이 문서만 다른 규칙을 쓸 때. 비우면 KB의 규칙.
    process_rule: ProcessRule | None = None
    allow_duplicate: bool = False


class KbDocumentUploadOptions(BaseModel):
    """멀티파트 업로드의 data 칸(JSON 문자열)."""

    model_config = ConfigDict(extra="forbid")

    process_rule: ProcessRule | None = None
    # 같은 KB에 내용(sha256)이 같은 문서가 있어도 넣는다.
    allow_duplicate: bool = False
    # model이면 PDF를 채팅 모델로 읽는다(스캔 PDF, 6.2). PDF가 아닌 파일에는 걸리지 않는다.
    extract_mode: ExtractMode = "text"


class KbDocumentsCreated(BaseModel):
    batch: str
    documents: list[KbDocumentOut]


class KbDocumentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: DocumentName | None = None


class DocumentStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class DocumentStatusSkip(BaseModel):
    # 그 조작이 맞지 않는 상태라 건너뛴 문서. retry_after는 일일 한도가 풀리는 때다.
    document_id: uuid.UUID
    code: str
    message: str
    retry_after: datetime | None = None


class DocumentStatusResult(BaseModel):
    items: list[KbDocumentOut]
    skipped: list[DocumentStatusSkip]


class DocumentReindex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 비우면 지난번 방식 그대로. model은 PDF만 된다(스캔 PDF 모델 읽기).
    extract_mode: ExtractMode | None = None


class DocumentPreviewText(BaseModel):
    """붙여 넣은 글의 미리보기(6.4). 저장하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, StringConstraints(min_length=1)]
    # 비우면 KB의 규칙.
    process_rule: ProcessRule | None = None


class DocumentPreviewOptions(BaseModel):
    """파일 미리보기의 data 칸(JSON 문자열)."""

    model_config = ConfigDict(extra="forbid")

    process_rule: ProcessRule | None = None


class PreviewChildChunk(BaseModel):
    content: str
    tokens: int


class PreviewChunk(BaseModel):
    content: str
    tokens: int
    answer: str | None = None
    children: list[PreviewChildChunk] = Field(default_factory=list[PreviewChildChunk])


class DocumentPreview(BaseModel):
    # 앞 50개. Parent-child는 부모 10개와 그 자식이다.
    chunks: list[PreviewChunk]
    total_chunks: int
    # Parent-child의 자식 수(검색 유닛 수). 다른 모드는 0.
    total_child_chunks: int
    # 임베딩 입력 기준 추정 토큰 합.
    tokens: int
    # 분당 인제스트 몫으로 나눈 예상 소요(분). 일일 한도는 넣지 않은 값이고 Economical은 0이다.
    estimated_minutes: int
    # {mode, pages, chars, truncated, warnings}.
    extract: dict[str, Any]
    warnings: list[str]


class ChildChunkOut(BaseModel):
    id: uuid.UUID
    segment_id: uuid.UUID
    position: int
    unit_no: int
    content: str
    tokens: int
    index_state: dict[str, Any]
    created_by: CreatedBy
    created_at: datetime
    updated_at: datetime


class ChildChunkList(BaseModel):
    items: list[ChildChunkOut]


class SegmentOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    position: int
    # General·Q&A의 색인 행 번호. Parent-child의 부모는 null.
    unit_no: int | None
    content: str
    answer: str | None
    word_count: int
    tokens: int
    enabled: bool
    status: SegmentStatus
    # {fts, vector, embedding_version, embedded_at}. 부모 청크는 둘 다 skip.
    index_state: dict[str, Any]
    created_by: CreatedBy
    edited: bool
    hit_count: int
    error: dict[str, Any] | None
    # Parent-child 문서만 채운다.
    child_chunks: list[ChildChunkOut] = Field(default_factory=list[ChildChunkOut])
    created_at: datetime
    updated_at: datetime


class SegmentList(BaseModel):
    items: list[SegmentOut]
    total: int
    page: int
    limit: int
    has_more: bool


class SegmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Q&A면 질문.
    content: ChunkText
    # Q&A 문서에서만, 그리고 반드시 준다.
    answer: ChunkText | None = None


class SegmentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: ChunkText | None = None
    answer: ChunkText | None = None
    # 색인이 끝난(completed) 청크만 켜고 끌 수 있다.
    enabled: bool | None = None


class ChildChunkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: ChunkText


class ChildChunkPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: ChunkText


class MetadataFieldCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: FieldName
    type: MetadataType


class MetadataFieldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: FieldName


class MetadataFieldOut(BaseModel):
    id: uuid.UUID
    name: str
    type: MetadataType
    built_in: bool
    position: int
    # 값이 있는 문서 수.
    use_count: int
    created_at: datetime


class MetadataFieldList(BaseModel):
    items: list[MetadataFieldOut]
    built_in_fields_enabled: bool


class MetadataValueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: uuid.UUID
    # string은 글, number는 수, time은 시간대가 있는 ISO 8601 글. null이면 값을 지운다.
    value: StrictInt | StrictFloat | StrictStr | None


class DocumentMetadataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    values: list[MetadataValueIn] = Field(min_length=1, max_length=50)
    # 끄면 그 필드에 이미 값이 있는 문서만 바꾼다(Dify와 같다, 7.5).
    apply_to_all: bool = False


class DocumentMetadataOut(BaseModel):
    document_id: uuid.UUID
    doc_metadata: list[DocumentMetadataValue]


class DocumentMetadataList(BaseModel):
    items: list[DocumentMetadataOut]
