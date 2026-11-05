"""Text2SQL 데이터 소스 콘솔 API와 sql_source JSON 열의 모양(3단계 설계 4.1, 10장, 12.1).

files·schema·annotations·limits·materialized는 sql_source에 이 모양 그대로 저장한다. 검증·DDL·실행
코드(app/knowledge/sql)도 이 값을 읽어 쓴다. 데이터 정책은 지식 베이스와 같은 모델을 쓴다(14.4).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.common import Slug
from app.schemas.knowledge import DataPolicy, DataPolicyIn, KeyTier

SqlSourceKind = Literal["files", "sqlite"]
SqlApproval = Literal["none", "always"]
SqlDialect = Literal["duckdb", "sqlite"]
SqlFileFormat = Literal["csv", "tsv", "xlsx", "sqlite"]
# auto는 DuckDB의 기본 감지(UTF-8·UTF-16·Latin-1)에 맡기고, UTF-8로 읽지 못하면 cp949로 다시 읽는다.
CsvEncoding = Literal["auto", "utf-8", "utf-16", "latin-1", "cp949", "euc-kr"]
SqlSourceState = Literal["empty", "not_prepared", "stale", "ready"]
SqlQueryOrigin = Literal["node", "tool", "test", "api"]
SqlQueryStatus = Literal["ok", "rejected", "error", "timeout"]

SourceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
SourceDescription = Annotated[str, StringConstraints(strip_whitespace=True, max_length=400)]
# 질의 파일의 표 이름이 그대로 SQL 식별자가 된다(14.3).
TableName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
NoteText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]
Glossary = Annotated[str, StringConstraints(max_length=8000)]
SheetName = Annotated[str, StringConstraints(min_length=1, max_length=100)]
MAX_SOURCE_FILES = 20
# 노드 config와 데이터 소스 limits의 상한. max_rows는 SQL_MAX_ROWS가 더 낮으면 그 값을 쓴다.
SQL_TIMEOUT_CAP = 60
SQL_MEMORY_CAP = 8192
LLM_BYTES_CAP = 64 * 1024
DEFAULT_MAX_ROWS = 200
DEFAULT_LLM_ROWS = 50
DEFAULT_SCHEMA_TOKENS = 8000
LimitRows = Annotated[int, Field(ge=1, le=10_000)]
LimitTimeout = Annotated[int, Field(ge=1, le=SQL_TIMEOUT_CAP)]
LimitMemory = Annotated[int, Field(ge=128, le=SQL_MEMORY_CAP)]
LimitLlmBytes = Annotated[int, Field(ge=1024, le=LLM_BYTES_CAP)]


class SqlFileOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 첫 줄이 머리글인지. false면 열 이름이 column0, column1…이다.
    header: bool = True
    encoding: CsvEncoding = "auto"
    # 타입 추측을 끄고 모든 열을 글로 읽는다(DuckDB all_varchar).
    all_varchar: bool = False


class SqlSourceFile(BaseModel):
    """sql_source.files 항목. 파일 하나가 표 하나다. sqlite 소스는 파일 하나이고 표 이름이 없다."""

    file_id: uuid.UUID
    storage_key: str
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    format: SqlFileFormat
    table: TableName | None = None
    # xlsx 시트. 비우면 첫 시트다.
    sheet: SheetName | None = None
    options: SqlFileOptions = Field(default_factory=SqlFileOptions)


class SqlSourceFileOut(BaseModel):
    file_id: uuid.UUID
    filename: str
    mime: str
    size_bytes: int
    sha256: str
    format: SqlFileFormat
    table: str | None
    sheet: str | None
    options: SqlFileOptions


class SqlColumn(BaseModel):
    name: str
    type: str
    # 보이는 열만 3개까지. 숨긴 열은 샘플을 뜨지 않는다.
    samples: list[Any] = Field(default_factory=list[Any])


class SqlTable(BaseModel):
    name: str
    kind: Literal["table", "view"] = "table"
    columns: list[SqlColumn]
    row_count: int | None = None
    # files 소스에서 이 표를 만든 파일과 읽을 때 쓴 인코딩.
    file_id: uuid.UUID | None = None
    encoding: str | None = None
    # sqlite 뷰의 정의가 깨져 열을 읽지 못했거나 숨김을 뺀 사본에 다시 만들지 못한 이유. 있으면 이
    # 표는 질의·DDL에서 빠진다.
    error: str | None = None


class SqlSchemaWarning(BaseModel):
    # view_exposes_hidden(숨긴 표·열을 읽는 뷰를 자동으로 숨겼다), view_broken(깨진 뷰를 뺐다).
    code: str
    message: str
    table: str | None = None


class SqlSchemaSnapshot(BaseModel):
    """준비 단계가 뜬 스냅샷(10.2). 숨긴 열도 이름·타입은 남겨 화면에서 다시 보이게 할 수 있다."""

    tables: list[SqlTable]
    captured_at: datetime
    # 이 스냅샷을 만든 파일·옵션·숨김 열의 해시. 지금 값과 다르면 다시 준비해야 한다.
    source_hash: str
    duration_ms: int | None = None
    # 준비하며 알린 것. 주석 저장·준비 응답의 schema.warnings로 보인다.
    warnings: list[SqlSchemaWarning] = Field(default_factory=list[SqlSchemaWarning])


class ColumnNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: NoteText = ""
    # 숨긴 열은 질의 파일·DDL·결과에 없다(10.2). 개인정보 열을 모델과 결과에서 치운다.
    hidden: bool = False


class TableNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: NoteText = ""
    hidden: bool = False
    columns: dict[str, ColumnNote] = Field(default_factory=dict[str, ColumnNote])


class SqlAnnotations(BaseModel):
    """표·열 설명과 숨김. 키는 스냅샷의 표·열 이름 그대로다."""

    model_config = ConfigDict(extra="forbid")

    tables: dict[str, TableNote] = Field(default_factory=dict[str, TableNote])


class SqlLimits(BaseModel):
    """저장된 데이터 소스의 한도. 새 소스의 값은 설정에서 만든다(sql_sources.default_limits)."""

    model_config = ConfigDict(extra="forbid")

    max_rows: LimitRows
    timeout_sec: LimitTimeout
    memory_mb: LimitMemory
    # 모델에 보내는 결과 글(result_text)의 행 수와 바이트(10.6).
    llm_rows: LimitRows
    llm_bytes: LimitLlmBytes


class SqlLimitsIn(BaseModel):
    """만들기·고치기에서 받는 한도. 비운 칸은 만들 때 서버 설정값, 고칠 때 저장된 값을 쓴다."""

    model_config = ConfigDict(extra="forbid")

    max_rows: LimitRows | None = None
    timeout_sec: LimitTimeout | None = None
    memory_mb: LimitMemory | None = None
    llm_rows: LimitRows | None = None
    llm_bytes: LimitLlmBytes | None = None


class SqlMaterialized(BaseModel):
    """files 소스의 질의 파일(DATA_DIR/cache/kb-sql/<id>.duckdb)."""

    filename: str
    built_at: datetime
    source_hash: str
    bytes: int


class SqlSourceStatus(BaseModel):
    state: SqlSourceState
    # files 소스는 DuckDB 런타임이 있어야 준비·질의할 수 있다.
    runtime_required: bool
    message: str = ""


class SqlSourceOut(BaseModel):
    model_config = ConfigDict(
        validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    id: uuid.UUID
    slug: str
    name: str
    description: str
    kind: SqlSourceKind
    dialect: SqlDialect
    files: list[SqlSourceFileOut]
    table_schema: SqlSchemaSnapshot | None = Field(default=None, alias="schema")
    annotations: SqlAnnotations
    glossary: str
    limits: SqlLimits
    approval: SqlApproval
    data_policy: DataPolicy
    materialized: SqlMaterialized | None
    status: SqlSourceStatus
    created_at: datetime
    updated_at: datetime


class SqlSourceSummary(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str
    kind: SqlSourceKind
    dialect: SqlDialect
    file_count: int
    table_count: int
    state: SqlSourceState
    approval: SqlApproval
    key_tier: KeyTier
    updated_at: datetime


class SqlSourceList(BaseModel):
    items: list[SqlSourceSummary]


class SqlSourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: SourceName
    # 비우면 이름에서 만든다. 이름이 한글뿐이면 source, source-2처럼 붙인다.
    slug: Slug | None = None
    description: SourceDescription = ""
    # 만든 뒤에는 바꿀 수 없다.
    kind: SqlSourceKind = "files"
    approval: SqlApproval = "none"
    limits: SqlLimitsIn | None = None
    glossary: Glossary = ""
    data_policy: DataPolicyIn


class SqlFilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: uuid.UUID
    table: TableName | None = None
    # 보내지 않으면 그대로 두고 null을 보내면 첫 시트로 되돌린다.
    sheet: SheetName | None = None
    options: SqlFileOptions | None = None


class SqlSourcePatch(BaseModel):
    """바뀐 칸만 보낸다. 파일의 표 이름·시트·옵션을 바꾸면 다시 준비해야 한다."""

    model_config = ConfigDict(extra="forbid")

    name: SourceName | None = None
    slug: Slug | None = None
    description: SourceDescription | None = None
    approval: SqlApproval | None = None
    limits: SqlLimitsIn | None = None
    glossary: Glossary | None = None
    # key_tier를 바꿀 때 다시 확인받는다.
    data_policy: DataPolicyIn | None = None
    files: list[SqlFilePatch] | None = None


class SqlAnnotationsUpdate(BaseModel):
    """표·열 설명과 숨김 전체를 바꾼다. 숨김이 바뀌면 files 소스는 질의 파일을 다시 만든다."""

    model_config = ConfigDict(extra="forbid")

    tables: dict[str, TableNote]
    glossary: Glossary | None = None
    # false면 숨김이 바뀌어도 다시 준비하지 않고 stale로 둔다(질의는 준비 전까지 막힌다).
    prepare: bool = True


class SqlQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: Annotated[str, StringConstraints(min_length=1, max_length=32 * 1024)]
    # 비우면 데이터 소스 limits.max_rows. SQL_MAX_ROWS를 넘으면 그 값으로 줄인다.
    max_rows: int | None = Field(default=None, ge=1)


class SqlFailure(BaseModel):
    # 검증 거부(sql_invalid, not_select, table_not_allowed, function_not_allowed, column_hidden
    # 등)와 실행 실패(sql_error, sql_timeout, sql_memory_limit, sql_failed).
    code: str
    message: str
    stage: Literal["validate", "execute"]


class SqlQueryResult(BaseModel):
    ok: bool
    # LIMIT을 넣고 * 를 풀어 쓴 뒤 실제로 돌린 SQL. 검증에서 거부되면 null이다.
    sql_executed: str | None = None
    columns: list[str] = Field(default_factory=list[str])
    rows: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    # 받은 행 수(max_rows까지). truncated면 더 있다.
    row_count: int = 0
    truncated: bool = False
    # rows가 1MB 상한에 걸려 row_count보다 적다.
    rows_cut: bool = False
    result_text: str = ""
    elapsed_ms: int = 0
    error: SqlFailure | None = None
    log_id: uuid.UUID | None = None


class SqlQueryLogOut(BaseModel):
    id: uuid.UUID
    origin: SqlQueryOrigin
    sql: str
    status: SqlQueryStatus
    reject_code: str | None
    row_count: int | None
    duration_ms: int | None
    error: str | None
    created_at: datetime


class SqlQueryLogList(BaseModel):
    items: list[SqlQueryLogOut]
    total: int
    page: int
    limit: int


class SqlSchemaText(BaseModel):
    """sql_schema 노드·describe_sql_source 도구가 내는 것과 같은 글(10.2)."""

    ddl: str
    tables: list[dict[str, Any]]
    glossary: str
    # 토큰 상한을 넘어 표 목록만 담았다.
    truncated: bool
    tokens: int


class SqlExportSchemaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kb_id: uuid.UUID


class SqlExportedDocument(BaseModel):
    table: str
    document_id: uuid.UUID
    name: str


class SqlExportFailure(BaseModel):
    table: str
    code: str
    message: str


class SqlExportSchemaOut(BaseModel):
    kb_id: uuid.UUID
    # 새로 만든 문서. 같은 이름(소스 이름 · 표)으로 앞서 내보낸 문서는 만든 뒤 지운다.
    documents: list[SqlExportedDocument]
    # 지운 이전 내보내기 문서 id.
    removed: list[uuid.UUID] = Field(default_factory=list[uuid.UUID])
    # 문서를 만들지 못한 표. 그 표의 이전 문서는 그대로 남는다.
    failed: list[SqlExportFailure] = Field(default_factory=list[SqlExportFailure])


class SqlExampleCreate(BaseModel):
    """Q&A 지식 베이스에 (질문, SQL) 한 쌍을 문서로 넣는다(10.7)."""

    model_config = ConfigDict(extra="forbid")

    kb_id: uuid.UUID
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]
    sql: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8192)]


class SqlExampleOut(BaseModel):
    kb_id: uuid.UUID
    document_id: uuid.UUID
    name: str


class SqlRuntimeFailure(BaseModel):
    code: str
    message: str


class SqlRuntimeStatus(BaseModel):
    """DuckDB 런타임(10.2). sqlite 소스는 런타임 없이 돈다."""

    state: Literal["missing", "installing", "ready", "failed"]
    packages: list[str]
    extensions: list[str]
    packages_ready: bool
    extensions_ready: bool
    # 받은 패키지와 확장 폴더의 크기.
    bytes: int | None = None
    installed_at: datetime | None = None
    error: SqlRuntimeFailure | None = None


class SqlNodeOptions(BaseModel):
    """sql_schema·sql_query 노드 폼이 쓰는 기본값과 상한(10.3). 데이터 소스 목록은 따로 받는다."""

    max_rows_max: int
    timeout_max: int
    default_max_rows: int
    default_timeout_sec: int
    default_memory_mb: int
    default_llm_rows: int
    default_llm_bytes: int
    llm_bytes_max: int
    default_schema_tokens: int
    approvals: list[SqlApproval]
