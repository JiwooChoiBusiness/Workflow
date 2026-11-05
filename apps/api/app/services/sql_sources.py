"""Text2SQL 데이터 소스(3단계 설계 10장, 12.1).

데이터 소스 행·파일·준비·주석·질의 기록을 다루고, 콘솔 SQL 시험·sql_query 노드·query_sql 도구가
함께 쓰는 검증·실행 경로(SqlExecutor)를 둔다. 세 곳 모두 sqlglot 검증(10.4) → 샌드박스 실행(10.5) →
sql_query_log 기록을 거치고 origin만 다르다. 그래프 검증이 보는 살아 있는 데이터 소스(SqlSourceRef),
런 키 등급의 데이터 소스 몫, 노드·도구 포트도 여기서 만든다.

files 소스는 질의 파일이 지금 파일·옵션·숨김의 해시와 맞아야 질의한다. sqlite 소스는 숨긴 표·열이
없으면 원본을 그대로 읽고, 있으면 준비가 만든 사본(보이는 표·열과 그것만 읽는 뷰)을 읽는다. 숨긴
표·열을 읽는 뷰는 준비할 때 자동으로 숨긴다. 두 종류 모두 숨김을 바꾸면 옛 질의 파일·사본을 바로
지운다(새 숨김 열이 든 파일이 남아 읽히지 않게).
"""

import asyncio
import csv
import io
import uuid
from collections.abc import AsyncIterable, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

import structlog
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.base import SqlToolError, SqlToolResult
from app.agent.tools.query_sql import SqlSourceChoice
from app.config import Settings
from app.db.engine import Database
from app.db.models import SqlQueryLog, SqlSource
from app.db.models.enums import KbDataSourceType, KbDocForm, SqlQueryStatus, SqlSourceKind
from app.db.repositories.knowledge import KnowledgeBaseRepository
from app.db.repositories.sql_sources import SqlQueryLogRepository, SqlSourceRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.knowledge.sql.ddl import SchemaText, schema_text, table_document
from app.knowledge.sql.prepare import (
    CopyTable,
    Prepared,
    PrepareFailed,
    PrepareFile,
    build_sqlite_copy,
    prepare_files,
    snapshot_sqlite,
)
from app.knowledge.sql.results import (
    ROWS_MAX_BYTES,
    result_text,
    row_objects,
    unique_columns,
)
from app.knowledge.sql.runner import QueryOutput, QueryRequest, SqlRunFailed, run_query
from app.knowledge.sql.sources import (
    KIND_FORMATS,
    SourceView,
    default_table_name,
    dialect_of,
    file_format,
    hidden_columns,
    hidden_tables,
    needs_copy,
    source_hash,
    source_view,
    strip_hidden_samples,
)
from app.knowledge.sql.validate import (
    SqlRejected,
    ValidatedSql,
    exposed_views,
    extract_sql,
    is_view_definition,
    validate_sql,
)
from app.llm.key_pool import KeyTier
from app.sandbox.base import SandboxError, WorkspaceHandle
from app.sandbox.provisioning import WorkspaceProvisioner
from app.schemas.knowledge import (
    DataPolicy,
    DataPolicyIn,
    KbDocumentList,
    KbDocumentsCreated,
    KbDocumentTextCreate,
)
from app.schemas.sql_source import (
    DEFAULT_LLM_ROWS,
    DEFAULT_MAX_ROWS,
    DEFAULT_SCHEMA_TOKENS,
    LLM_BYTES_CAP,
    MAX_SOURCE_FILES,
    SQL_MEMORY_CAP,
    SQL_TIMEOUT_CAP,
    SqlAnnotations,
    SqlAnnotationsUpdate,
    SqlExampleCreate,
    SqlExampleOut,
    SqlExportedDocument,
    SqlExportFailure,
    SqlExportSchemaOut,
    SqlFailure,
    SqlLimits,
    SqlLimitsIn,
    SqlMaterialized,
    SqlNodeOptions,
    SqlQueryLogList,
    SqlQueryLogOut,
    SqlQueryRequest,
    SqlQueryResult,
    SqlSchemaSnapshot,
    SqlSchemaText,
    SqlSchemaWarning,
    SqlSourceCreate,
    SqlSourceFile,
    SqlSourceFileOut,
    SqlSourceList,
    SqlSourceOut,
    SqlSourcePatch,
    SqlSourceStatus,
    SqlSourceSummary,
    TableNote,
)
from app.schemas.workflow import Graph
from app.services.agents import slugify
from app.services.errors import Problems, ServiceError, not_found
from app.services.files import guess_mime, sanitize_filename
from app.services.run_tier import paid_key_configured, sql_key_tier_of
from app.services.sql_runtime import SqlRuntime, SqlRuntimeMissing
from app.storage.base import StorageBackend, StorageError, StorageLimitExceeded, sql_source_key
from app.workflow.graph import sql_references
from app.workflow.nodes.base import SqlSourceRef
from app.workflow.ports import CsvSaver, SqlPortError, SqlRun, SqlSourceInfo

log = structlog.get_logger("app.services.sql_sources")

_SLUG_MAX = 64
_SLUG_ATTEMPTS = 1000
LOG_SQL_CHARS = 8 * 1024
LOG_ERROR_CHARS = 2000
ERR_NOT_FOUND = "sql_source_not_found"
ERR_NOT_PREPARED = "sql_source_not_prepared"
Origin = Literal["node", "tool", "test", "api"]


def _slug_taken(slug: str | None) -> ServiceError:
    if slug is None:
        return ServiceError(409, "slug_taken", "slug가 다른 데이터 소스와 겹친다")
    return ServiceError(409, "slug_taken", f"이미 쓰는 slug다: {slug}", details={"slug": slug})


async def _free_slug(sources: SqlSourceRepository, name: str) -> str:
    base = slugify(name) or "source"
    candidate = base
    for number in range(2, _SLUG_ATTEMPTS):
        if not await sources.slug_taken(candidate):
            return candidate
        suffix = f"-{number}"
        candidate = base[: _SLUG_MAX - len(suffix)].rstrip("-") + suffix
    raise _slug_taken(candidate)


def parse_files(raw: Sequence[Any] | None) -> list[SqlSourceFile]:
    found: list[SqlSourceFile] = []
    for item in raw or []:
        try:
            found.append(SqlSourceFile.model_validate(item))
        except ValidationError:
            log.warning("sql.source_file_invalid")
    return found


def parse_snapshot(raw: Mapping[str, Any] | None) -> SqlSchemaSnapshot | None:
    if not raw:
        return None
    try:
        return SqlSchemaSnapshot.model_validate(raw)
    except ValidationError:
        log.warning("sql.snapshot_invalid")
        return None


def parse_annotations(raw: Mapping[str, Any] | None) -> SqlAnnotations:
    try:
        return SqlAnnotations.model_validate(raw or {})
    except ValidationError:
        log.warning("sql.annotations_invalid")
        return SqlAnnotations()


# limits가 깨진 데이터 소스. 같은 행을 읽을 때마다 경고를 다시 찍지 않는다(프로세스가 도는 동안).
_INVALID_LIMITS_WARNED: set[uuid.UUID] = set()


def parse_limits(
    raw: Mapping[str, Any] | None, defaults: SqlLimits, *, source_id: uuid.UUID | None = None
) -> SqlLimits:
    """저장된 limits. 빠진 칸(열 기본값 {} 포함)은 defaults로 채우고, 깨졌으면 defaults를 쓴다."""
    try:
        return SqlLimits.model_validate({**defaults.model_dump(), **(raw or {})})
    except ValidationError:
        if source_id is None or source_id not in _INVALID_LIMITS_WARNED:
            if source_id is not None:
                _INVALID_LIMITS_WARNED.add(source_id)
            log.warning("sql.limits_invalid", source_id=str(source_id) if source_id else None)
        return defaults


def default_limits(settings: Settings) -> SqlLimits:
    """새 데이터 소스의 한도. 노드 폼 기본값(sql_options)도 이 값이다.

    설정을 바꿔도 이미 만든 소스에는 소급하지 않는다(설정 탭에서 고친다).
    """
    return SqlLimits(
        max_rows=min(DEFAULT_MAX_ROWS, settings.sql_max_rows),
        timeout_sec=settings.sql_timeout_sec,
        memory_mb=min(settings.sql_memory_mb, SQL_MEMORY_CAP),
        llm_rows=DEFAULT_LLM_ROWS,
        llm_bytes=LLM_BYTES_CAP,
    )


def fill_limits(base: SqlLimits, given: SqlLimitsIn | None) -> SqlLimits:
    """given에서 비운 칸은 base(만들 때 설정값, 고칠 때 저장된 값)로 채운다."""
    if given is None:
        return base
    return base.model_copy(update=given.model_dump(exclude_none=True))


def parse_materialized(raw: Mapping[str, Any] | None) -> SqlMaterialized | None:
    if not raw:
        return None
    try:
        return SqlMaterialized.model_validate(raw)
    except ValidationError:
        return None


@dataclass(frozen=True)
class LoadedSource:
    """한 번 읽은 데이터 소스. 세션 밖에서 쓰도록 필요한 값만 옮겨 둔다."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    slug: str
    name: str
    description: str
    kind: str
    files: list[SqlSourceFile]
    snapshot: SqlSchemaSnapshot | None
    annotations: SqlAnnotations
    glossary: str
    limits: SqlLimits
    approval: str
    tier: KeyTier
    materialized: SqlMaterialized | None
    current_hash: str
    view: SourceView = field(repr=False)

    @property
    def dialect(self) -> Literal["duckdb", "sqlite"]:
        return dialect_of(self.kind)


def load(row: SqlSource, settings: Settings) -> LoadedSource:
    files = parse_files(row.files)
    snapshot = parse_snapshot(row.table_schema)
    annotations = parse_annotations(row.annotations)
    return LoadedSource(
        id=row.id,
        workspace_id=row.workspace_id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        kind=row.kind,
        files=files,
        snapshot=snapshot,
        annotations=annotations,
        glossary=row.glossary,
        limits=parse_limits(row.limits, default_limits(settings), source_id=row.id),
        approval=row.approval,
        tier=sql_key_tier_of(row),
        materialized=parse_materialized(row.materialized),
        current_hash=source_hash(row.kind, files, annotations),
        view=source_view(row.kind, snapshot, annotations),
    )


class SqlUnavailable(Exception):
    """데이터 소스를 지금 질의할 수 없다(없음, 준비 전, 런타임 없음). 콘솔은 404·409다."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def service_error(self) -> ServiceError:
        status = 404 if self.code == ERR_NOT_FOUND else 409
        return ServiceError(status, self.code, self.message)


def _state(source: LoadedSource, database_exists: bool) -> SqlSourceStatus:
    runtime_required = source.kind == SqlSourceKind.FILES
    if not source.files:
        return SqlSourceStatus(
            state="empty", runtime_required=runtime_required, message="파일을 올린다"
        )
    snapshot = source.snapshot
    if snapshot is None:
        return SqlSourceStatus(
            state="not_prepared", runtime_required=runtime_required, message="준비를 누른다"
        )
    if snapshot.source_hash != source.current_hash:
        return SqlSourceStatus(
            state="stale",
            runtime_required=runtime_required,
            message="파일·표 옵션·숨김이 바뀌어 다시 준비해야 한다",
        )
    if runtime_required or needs_copy(source.annotations):
        materialized = source.materialized
        if (
            materialized is None
            or materialized.source_hash != source.current_hash
            or not database_exists
        ):
            missing = "질의 파일" if runtime_required else "숨김을 뺀 사본"
            return SqlSourceStatus(
                state="stale",
                runtime_required=runtime_required,
                message=f"{missing}이 없어 다시 준비해야 한다",
            )
    return SqlSourceStatus(state="ready", runtime_required=runtime_required)


def _file_out(item: SqlSourceFile) -> SqlSourceFileOut:
    return SqlSourceFileOut(
        file_id=item.file_id,
        filename=item.filename,
        mime=item.mime,
        size_bytes=item.size_bytes,
        sha256=item.sha256,
        format=item.format,
        table=item.table,
        sheet=item.sheet,
        options=item.options,
    )


def source_out(row: SqlSource, status: SqlSourceStatus, settings: Settings) -> SqlSourceOut:
    source = load(row, settings)
    return SqlSourceOut(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        kind=row.kind,  # pyright: ignore[reportArgumentType]
        dialect=source.dialect,
        files=[_file_out(item) for item in source.files],
        schema=source.snapshot,
        annotations=source.annotations,
        glossary=row.glossary,
        limits=source.limits,
        approval=row.approval,  # pyright: ignore[reportArgumentType]
        data_policy=DataPolicy.model_validate(row.data_policy or {}),
        materialized=source.materialized,
        status=status,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@dataclass
class SqlUpload:
    filename: str | None
    content_type: str | None
    chunks: AsyncIterable[bytes]


@dataclass(frozen=True)
class ExecutedQuery:
    validated: ValidatedSql
    output: QueryOutput
    # 겹치는 이름을 고친 열과 그 열 이름을 키로 한 행(1MB 안).
    columns: list[str]
    rows: list[dict[str, Any]]
    rows_cut: bool
    log_id: uuid.UUID | None


CsvSink = Callable[[Path], Awaitable[None]]


class SqlExecutor:
    """검증·실행·기록의 한 경로(10.4, 10.5). 콘솔 시험·노드·도구가 origin만 바꿔 부른다."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        runtime: SqlRuntime | None,
        storage: StorageBackend | None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._runtime = runtime
        self._storage = storage
        self._clock = clock

    @property
    def settings(self) -> Settings:
        return self._settings

    def max_rows(self, wanted: int | None, source: LoadedSource) -> int:
        value = wanted if wanted is not None else source.limits.max_rows
        return max(1, min(value, self._settings.sql_max_rows))

    def memory_mb(self, source: LoadedSource) -> int:
        return max(128, min(source.limits.memory_mb, self._settings.sql_memory_mb))

    async def source(self, workspace_id: uuid.UUID, source_id: uuid.UUID) -> LoadedSource:
        async with self._db.read() as session:
            row = await SqlSourceRepository(session, workspace_id).get_active(source_id)
            if row is None:
                raise SqlUnavailable(
                    ERR_NOT_FOUND, "데이터 소스를 찾을 수 없다. 지웠거나 다른 PC의 id다"
                )
            return load(row, self._settings)

    def query_file(self, source: LoadedSource) -> Path | None:
        """질의가 여는 캐시 파일. files 소스는 질의 파일, 숨김이 있는 sqlite 소스는 사본이고 숨김이
        없는 sqlite 소스는 원본을 그대로 쓰므로 None이다."""
        if self._runtime is None:
            return None
        if source.kind == SqlSourceKind.FILES:
            return self._runtime.database_path(source.id)
        if needs_copy(source.annotations):
            return self._runtime.copy_path(source.id)
        return None

    async def database_exists(self, source: LoadedSource) -> bool:
        path = self.query_file(source)
        if path is None:
            return False
        return await asyncio.to_thread(path.is_file)

    async def status(self, source: LoadedSource) -> SqlSourceStatus:
        return _state(source, await self.database_exists(source))

    async def _check_ready(self, source: LoadedSource) -> Path:
        """질의할 파일(DuckDB 질의 파일, SQLite 사본, 숨김이 없으면 SQLite 원본)."""
        status = await self.status(source)
        if status.state != "ready":
            raise SqlUnavailable(
                ERR_NOT_PREPARED,
                f"데이터 소스 {source.name}을(를) 먼저 준비해야 한다({status.message})",
                retryable=False,
            )
        if self._runtime is None:
            raise SqlUnavailable("sql_failed", "SQL 실행 환경이 없다", retryable=True)
        cached = self.query_file(source)
        if cached is not None:
            return cached
        if self._storage is None:
            raise SqlUnavailable("sql_failed", "파일 저장소가 없다", retryable=True)
        try:
            path = self._storage.local_path(source.files[0].storage_key)
        except StorageError as exc:
            raise SqlUnavailable("sql_source_file_missing", exc.message) from None
        if not await asyncio.to_thread(path.is_file):
            raise SqlUnavailable(
                "sql_source_file_missing", "SQLite 원본 파일이 없다. 파일을 다시 올린다"
            )
        return path

    async def validate(
        self, source: LoadedSource, sql: str, *, max_rows: int, origin: Origin
    ) -> ValidatedSql:
        """검증만 한다(승인 폼에 채울 SQL). 거부는 기록하고 SqlRejected를 그대로 던진다."""
        text = extract_sql(sql)
        try:
            # sqlglot은 처음 쓸 때 불러오며(수백 ms) CPU만 쓰므로 스레드에서 돈다.
            return await asyncio.to_thread(validate_sql, text, source.view, max_rows=max_rows)
        except SqlRejected as exc:
            await self._log(
                source,
                origin=origin,
                sql=text,
                status=SqlQueryStatus.REJECTED,
                reject_code=exc.code,
                error=exc.message,
            )
            raise

    async def execute(
        self,
        source: LoadedSource,
        sql: str,
        *,
        origin: Origin,
        max_rows: int,
        timeout_sec: int,
        export_csv: bool = False,
        on_csv: CsvSink | None = None,
    ) -> ExecutedQuery:
        """검증해 실행하고 기록한다. SqlRejected·SqlRunFailed·SqlUnavailable을 던진다."""
        runtime = self._runtime
        if runtime is None:
            raise SqlUnavailable("sql_failed", "SQL 실행 환경이 없다", retryable=True)
        database = await self._check_ready(source)
        lib: Path | None = None
        if source.kind == SqlSourceKind.FILES:
            try:
                lib = await runtime.ready_lib()
            except SqlRuntimeMissing as exc:
                raise SqlUnavailable(exc.code, exc.message, retryable=True) from None
        validated = await self.validate(source, sql, max_rows=max_rows, origin=origin)
        timeout = max(1, min(timeout_sec, SQL_TIMEOUT_CAP))
        request = QueryRequest(
            engine=source.dialect,
            database=database,
            sql=validated.sql,
            max_rows=max_rows,
            timeout_sec=timeout,
            memory_mb=self.memory_mb(source),
            export_csv=export_csv,
            lib=lib,
            tables=[table.name for table in source.view.tables],
            ctes=validated.ctes,
            hidden={
                table.name: [column.name for column in table.hidden]
                for table in source.view.tables
                if table.hidden
            },
            hidden_tables=sorted(source.view.hidden_tables),
        )
        started = self._clock()
        try:
            async with runtime.slot(), runtime.work() as handle:
                output = await run_query(runtime.backend(), handle, request, exec_id="sql")
                if output.csv_path is not None and on_csv is not None:
                    await on_csv(output.csv_path)
        except SqlRunFailed as exc:
            elapsed = int((self._clock() - started).total_seconds() * 1000)
            status = SqlQueryStatus.TIMEOUT if exc.code == "sql_timeout" else SqlQueryStatus.ERROR
            await self._log(
                source,
                origin=origin,
                sql=validated.sql,
                status=status,
                duration_ms=elapsed,
                error=f"{exc.code}: {exc.message}",
            )
            raise
        columns = unique_columns(output.columns)
        rows, cut = row_objects(columns, output.rows, max_bytes=ROWS_MAX_BYTES)
        log_id = await self._log(
            source,
            origin=origin,
            sql=validated.sql,
            status=SqlQueryStatus.OK,
            row_count=output.row_count,
            duration_ms=output.elapsed_ms,
        )
        return ExecutedQuery(
            validated=validated,
            output=output,
            columns=columns,
            rows=rows,
            rows_cut=cut or output.rows_cut,
            log_id=log_id,
        )

    def text(
        self,
        source: LoadedSource,
        executed: ExecutedQuery,
        *,
        max_rows: int,
        llm_rows: int,
        llm_bytes: int,
    ) -> str:
        output = executed.output
        return result_text(
            executed.columns,
            output.rows,
            row_count=output.row_count,
            truncated=output.truncated,
            max_rows=max_rows,
            llm_rows=max(1, llm_rows),
            llm_bytes=max(1024, min(llm_bytes, LLM_BYTES_CAP)),
            source_name=source.name,
        )

    async def _log(
        self,
        source: LoadedSource,
        *,
        origin: Origin,
        sql: str,
        status: SqlQueryStatus,
        reject_code: str | None = None,
        row_count: int | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> uuid.UUID | None:
        # 0이면 기록하지 않는다(4.5). 기록이 실패해도 질의 결과는 돌려준다(지식 검색과 같다).
        if self._settings.kb_query_log_days <= 0:
            return None
        try:
            async with self._db.write() as session:
                logs = SqlQueryLogRepository(session, source.workspace_id)
                row = logs.add(
                    SqlQueryLog(
                        id=new_id(),
                        source_id=source.id,
                        origin=origin,
                        sql=sql[:LOG_SQL_CHARS],
                        status=status,
                        reject_code=reject_code,
                        row_count=row_count,
                        duration_ms=duration_ms,
                        error=error[:LOG_ERROR_CHARS] if error is not None else None,
                    )
                )
                await logs.flush()
                return row.id
        except Exception:
            log.exception("sql.query_log_failed", source_id=str(source.id))
            return None


def _runtime_error(exc: SqlRuntimeMissing) -> ServiceError:
    return ServiceError(409, exc.code, exc.message)


def _broken_views(prepared: Prepared) -> list[SqlSchemaWarning]:
    return [
        SqlSchemaWarning(
            code="view_broken",
            message=f"뷰 {table.name}의 정의를 읽지 못해 질의에서 뺐다: {table.error}",
            table=table.name,
        )
        for table in prepared.tables
        if table.error
    ]


async def _conceal_views(
    prepared: Prepared, annotations: SqlAnnotations
) -> tuple[SqlAnnotations, list[SqlSchemaWarning]]:
    """숨긴 표·열(또는 그런 뷰)을 읽는 뷰를 숨긴다. 숨김을 바꾸면 준비가 다시 돌므로 뷰는 늘 숨김을
    따라간다. 뷰를 다시 보이게 해도 숨긴 것을 읽는 동안은 다시 숨긴다."""
    concealed = hidden_tables(annotations)
    columns = hidden_columns(annotations)
    if not (concealed or columns) or not prepared.views:
        return annotations, []
    definitions = dict(prepared.views)
    exposed = await asyncio.to_thread(
        exposed_views, definitions, hidden_tables=concealed, hidden_columns=columns
    )
    newly = [name for name in exposed if name.lower() not in concealed]
    if not newly:
        return annotations, []
    tables = dict(annotations.tables)
    keys = {key.lower(): key for key in tables}
    warnings: list[SqlSchemaWarning] = []
    for name in newly:
        key = keys.get(name.lower(), name)
        note = tables.get(key) or TableNote()
        tables[key] = note.model_copy(update={"hidden": True})
        warnings.append(
            SqlSchemaWarning(
                code="view_exposes_hidden",
                message=(
                    f"뷰 {name}이(가) 숨긴 표·열을 읽어 함께 숨겼다. 보이게 하려면 그 표·열의 "
                    "숨김을 먼저 푼다"
                ),
                table=name,
            )
        )
    return SqlAnnotations(tables=tables), warnings


def _copy_tables(prepared: Prepared, annotations: SqlAnnotations) -> list[CopyTable]:
    concealed = hidden_tables(annotations)
    columns = hidden_columns(annotations)
    found: list[CopyTable] = []
    for table in prepared.tables:
        key = table.name.lower()
        if table.kind != "table" or table.error or key in concealed:
            continue
        hidden = columns.get(key, set())
        visible = [column.name for column in table.columns if column.name.lower() not in hidden]
        if visible:
            found.append(CopyTable(name=table.name, columns=visible))
    return found


async def _copy_views(prepared: Prepared, annotations: SqlAnnotations) -> list[tuple[str, str]]:
    """사본에 다시 만들 뷰(만든 순서). 숨긴 뷰·깨진 뷰는 빼고, 정의가 CREATE VIEW … SELECT 하나인
    것만 넣는다(원본 파일의 sqlite_master 글을 그대로 실행하기 전에 본다)."""
    broken = {table.name.lower() for table in prepared.tables if table.error}
    skip = hidden_tables(annotations) | broken
    candidates = [(name, sql) for name, sql in prepared.views if name.lower() not in skip]
    if not candidates:
        return []
    return await asyncio.to_thread(
        lambda: [(name, sql) for name, sql in candidates if is_view_definition(sql)]
    )


def _mark_skipped(prepared: Prepared, skipped: Mapping[str, str]) -> list[SqlSchemaWarning]:
    warnings: list[SqlSchemaWarning] = []
    for table in prepared.tables:
        reason = skipped.get(table.name)
        if reason is None:
            continue
        table.error = f"숨김을 뺀 사본에 다시 만들지 못했다: {reason}"
        warnings.append(
            SqlSchemaWarning(
                code="view_broken",
                message=(
                    f"뷰 {table.name}을(를) 숨김을 뺀 사본에 만들지 못해 질의에서 뺐다: {reason}"
                ),
                table=table.name,
            )
        )
    return warnings


class SqlSourceService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        storage: StorageBackend | None,
        runtime: SqlRuntime | None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._storage = storage
        self._runtime = runtime
        self._clock = clock
        self.executor = SqlExecutor(db, settings, runtime, storage, clock=clock)

    @property
    def max_upload_bytes(self) -> int:
        return self._settings.sql_upload_max_mb * 1024 * 1024

    def _check_policy(self, policy: DataPolicyIn, problems: Problems) -> dict[str, Any]:
        # 질의 결과의 일부가 채팅 모델로 간다는 안내를 확인해야 한다(14.4).
        if not policy.acknowledged:
            problems.add(
                ("data_policy", "acknowledged"),
                "data_policy_not_acknowledged",
                "데이터 정책 안내를 확인해야 데이터 소스를 저장한다",
            )
        if policy.key_tier == "paid" and not paid_key_configured(self._settings):
            raise ServiceError(
                422, "paid_key_missing", "GEMINI_PAID_API_KEY가 없어 유료 키 전용을 고를 수 없다"
            )
        return DataPolicy(key_tier=policy.key_tier, acknowledged_at=self._clock()).model_dump(
            mode="json"
        )

    async def _active(
        self, session: AsyncSession, workspace_id: uuid.UUID, source_id: uuid.UUID
    ) -> SqlSource:
        row = await SqlSourceRepository(session, workspace_id).get_active(source_id)
        if row is None:
            raise not_found("데이터 소스")
        return row

    async def _out(self, row: SqlSource) -> SqlSourceOut:
        return source_out(
            row, await self.executor.status(load(row, self._settings)), self._settings
        )

    async def list_sources(self, workspace_id: uuid.UUID) -> SqlSourceList:
        async with self._db.read() as session:
            rows = await SqlSourceRepository(session, workspace_id).list_active()
        items: list[SqlSourceSummary] = []
        for row in rows:
            source = load(row, self._settings)
            status = await self.executor.status(source)
            items.append(
                SqlSourceSummary(
                    id=row.id,
                    slug=row.slug,
                    name=row.name,
                    description=row.description,
                    kind=row.kind,  # pyright: ignore[reportArgumentType]
                    dialect=source.dialect,
                    file_count=len(source.files),
                    table_count=len(source.snapshot.tables) if source.snapshot else 0,
                    state=status.state,
                    approval=row.approval,  # pyright: ignore[reportArgumentType]
                    key_tier=source.tier,
                    updated_at=row.updated_at,
                )
            )
        return SqlSourceList(items=items)

    async def get_source(self, workspace_id: uuid.UUID, source_id: uuid.UUID) -> SqlSourceOut:
        async with self._db.read() as session:
            row = await self._active(session, workspace_id, source_id)
        return await self._out(row)

    async def create_source(self, workspace_id: uuid.UUID, body: SqlSourceCreate) -> SqlSourceOut:
        problems = Problems()
        policy = self._check_policy(body.data_policy, problems)
        problems.raise_if_any()
        try:
            async with self._db.write() as session:
                sources = SqlSourceRepository(session, workspace_id)
                if body.slug is not None:
                    if await sources.slug_taken(body.slug):
                        raise _slug_taken(body.slug)
                    slug = body.slug
                else:
                    slug = await _free_slug(sources, body.name)
                row = sources.add(
                    SqlSource(
                        id=new_id(),
                        slug=slug,
                        name=body.name,
                        description=body.description,
                        kind=body.kind,
                        files=[],
                        table_schema=None,
                        annotations=SqlAnnotations().model_dump(mode="json"),
                        glossary=body.glossary,
                        limits=fill_limits(default_limits(self._settings), body.limits).model_dump(
                            mode="json"
                        ),
                        approval=body.approval,
                        data_policy=policy,
                        materialized=None,
                    )
                )
                await sources.flush()
        except IntegrityError:
            raise _slug_taken(body.slug) from None
        log.info("sql.source_created", source_id=str(row.id), kind=row.kind)
        return await self._out(row)

    async def update_source(
        self, workspace_id: uuid.UUID, source_id: uuid.UUID, patch: SqlSourcePatch
    ) -> SqlSourceOut:
        problems = Problems()
        try:
            async with self._db.write() as session:
                sources = SqlSourceRepository(session, workspace_id)
                row = await self._active(session, workspace_id, source_id)
                if patch.slug is not None and patch.slug != row.slug:
                    if await sources.slug_taken(patch.slug, exclude_id=row.id):
                        raise _slug_taken(patch.slug)
                    row.slug = patch.slug
                if patch.name is not None:
                    row.name = patch.name
                if patch.description is not None:
                    row.description = patch.description
                if patch.approval is not None:
                    row.approval = patch.approval
                if patch.limits is not None:
                    stored = parse_limits(
                        row.limits, default_limits(self._settings), source_id=row.id
                    )
                    row.limits = fill_limits(stored, patch.limits).model_dump(mode="json")
                if patch.glossary is not None:
                    row.glossary = patch.glossary
                if patch.data_policy is not None:
                    row.data_policy = self._check_policy(patch.data_policy, problems)
                if patch.files is not None:
                    row.files = self._patch_files(row, patch, problems)
                problems.raise_if_any()
                await sources.flush()
        except IntegrityError:
            raise _slug_taken(patch.slug) from None
        return await self._out(row)

    def _patch_files(
        self, row: SqlSource, patch: SqlSourcePatch, problems: Problems
    ) -> list[dict[str, Any]]:
        files = parse_files(row.files)
        by_id = {item.file_id: item for item in files}
        for index, change in enumerate(patch.files or []):
            item = by_id.get(change.file_id)
            if item is None:
                problems.add(
                    ("files", index, "file_id"), "file_not_found", "이 소스의 파일이 아니다"
                )
                continue
            if change.table is not None and item.format != "sqlite":
                item.table = change.table
            if "sheet" in change.model_fields_set and item.format == "xlsx":
                item.sheet = change.sheet
            if change.options is not None:
                item.options = change.options
        tables = [item.table for item in files if item.table is not None]
        if len(set(tables)) != len(tables):
            problems.add(
                ("files",), "table_name_taken", "표 이름이 겹친다. 파일마다 다른 이름을 쓴다"
            )
        return [item.model_dump(mode="json") for item in files]

    async def delete_source(self, workspace_id: uuid.UUID, source_id: uuid.UUID) -> None:
        async with self._db.write() as session:
            row = await self._active(session, workspace_id, source_id)
            row.deleted_at = self._clock()
        # 질의 파일은 캐시라 바로 지운다. 원본은 매시 정리가 7일 뒤 지운다(4.5).
        if self._runtime is not None:
            await self._runtime.remove_database(source_id)
        log.info("sql.source_deleted", source_id=str(source_id))

    async def add_files(
        self, workspace_id: uuid.UUID, source_id: uuid.UUID, uploads: Sequence[SqlUpload]
    ) -> SqlSourceOut:
        """파일을 원본 저장소(sql-sources/)에 두고 표 이름을 붙인다. sqlite 소스는 파일 하나를
        바꾼다."""
        if self._storage is None:
            raise ServiceError(503, "storage_unavailable", "파일 저장소가 없다")
        if not uploads:
            raise ServiceError(
                422,
                "validation_error",
                "요청 값이 올바르지 않다",
                details=[{"loc": ["body", "files"], "msg": "파일이 없다", "type": "missing"}],
            )
        async with self._db.read() as session:
            row = await self._active(session, workspace_id, source_id)
            kind = row.kind
            existing = parse_files(row.files)
        allowed = KIND_FORMATS["sqlite" if kind == SqlSourceKind.SQLITE else "files"]
        if kind == SqlSourceKind.SQLITE and len(uploads) > 1:
            raise ServiceError(422, "too_many_files", "SQLite 데이터 소스는 파일 하나만 쓴다")
        if kind != SqlSourceKind.SQLITE and len(existing) + len(uploads) > MAX_SOURCE_FILES:
            raise ServiceError(
                422, "too_many_files", f"데이터 소스 하나에 파일은 {MAX_SOURCE_FILES}개까지다"
            )
        names: list[tuple[str, str]] = []
        for upload in uploads:
            filename = sanitize_filename(upload.filename)
            found = file_format(filename)
            if found is None or found not in allowed:
                kinds = ", ".join(sorted(allowed))
                raise ServiceError(
                    415,
                    "unsupported_file_type",
                    f"{filename}은(는) 이 데이터 소스에 넣을 수 없다({kinds}만 받는다)",
                )
            names.append((filename, found))
        stored: list[SqlSourceFile] = []
        try:
            taken = {item.table for item in existing if item.table is not None}
            for upload, (filename, found) in zip(uploads, names, strict=True):
                file_id = new_id()
                key = sql_source_key(workspace_id, file_id)
                result = await self._storage.write_stream(
                    key, upload.chunks, max_bytes=self.max_upload_bytes
                )
                table = None if found == "sqlite" else default_table_name(filename, taken)
                if table is not None:
                    taken.add(table)
                stored.append(
                    SqlSourceFile(
                        file_id=file_id,
                        storage_key=key,
                        filename=filename,
                        mime=guess_mime(filename, upload.content_type),
                        size_bytes=result.size_bytes,
                        sha256=result.sha256,
                        format=found,  # pyright: ignore[reportArgumentType]
                        table=table,
                    )
                )
        except StorageLimitExceeded:
            await self._discard(stored)
            raise ServiceError(
                413,
                "file_too_large",
                f"파일은 {self._settings.sql_upload_max_mb}MB 이하여야 한다",
                details={"max_bytes": self.max_upload_bytes},
            ) from None
        except BaseException:
            await self._discard(stored)
            raise
        replaced: list[SqlSourceFile] = []
        async with self._db.write() as session:
            row = await self._active(session, workspace_id, source_id)
            current = parse_files(row.files)
            if kind == SqlSourceKind.SQLITE:
                replaced = current
                current = []
            row.files = [item.model_dump(mode="json") for item in [*current, *stored]]
        await self._discard(replaced)
        log.info("sql.source_files_added", source_id=str(source_id), files=len(stored))
        return await self._out(row)

    async def _discard(self, files: Sequence[SqlSourceFile]) -> None:
        if self._storage is None:
            return
        for item in files:
            try:
                await self._storage.delete(item.storage_key)
            except (StorageError, OSError) as exc:
                log.warning("sql.source_file_delete_failed", error=str(exc))

    async def delete_file(
        self, workspace_id: uuid.UUID, source_id: uuid.UUID, file_id: uuid.UUID
    ) -> SqlSourceOut:
        async with self._db.write() as session:
            row = await self._active(session, workspace_id, source_id)
            files = parse_files(row.files)
            removed = [item for item in files if item.file_id == file_id]
            if not removed:
                raise not_found("데이터 소스 파일")
            row.files = [item.model_dump(mode="json") for item in files if item.file_id != file_id]
        await self._discard(removed)
        return await self._out(row)

    async def prepare(self, workspace_id: uuid.UUID, source_id: uuid.UUID) -> SqlSourceOut:
        """스냅샷을 뜨고 files 소스는 질의 파일을 만든다(10.2). 질의와 함께 한 번에 하나만 돈다."""
        runtime = self._runtime
        if runtime is None or runtime.sandbox is None:
            raise ServiceError(503, "sql_unavailable", "SQL 실행 환경이 없다")
        async with self._db.read() as session:
            row = await self._active(session, workspace_id, source_id)
            source = load(row, self._settings)
        if not source.files:
            raise ServiceError(
                422, "sql_source_empty", "데이터 소스에 파일이 없다. 파일을 먼저 올린다"
            )
        lib: Path | None = None
        if source.kind == SqlSourceKind.FILES:
            try:
                lib = await runtime.ready_lib()
            except SqlRuntimeMissing as exc:
                raise _runtime_error(exc) from None
        hidden = hidden_columns(source.annotations)
        concealed = hidden_tables(source.annotations)
        memory_mb = self.executor.memory_mb(source)
        started = self._clock()
        size: int | None = None
        annotations = source.annotations
        warnings: list[SqlSchemaWarning] = []
        try:
            async with runtime.slot(), runtime.work() as handle:
                sandbox = runtime.backend()
                # lib은 files 소스에만 있다.
                if lib is not None:
                    prepared = await prepare_files(
                        sandbox,
                        handle,
                        lib=lib,
                        extensions_dir=runtime.extensions_dir,
                        files=self._prepare_files(source, hidden, concealed),
                        memory_mb=memory_mb,
                        exec_id="sql-prepare",
                    )
                    if prepared.database is not None:
                        size = await runtime.install_database(prepared.database, source.id)
                else:
                    original = self._local_path(source.files[0])
                    prepared = await snapshot_sqlite(
                        sandbox,
                        handle,
                        database=original,
                        hidden={table: sorted(columns) for table, columns in hidden.items()},
                        hidden_tables=sorted(concealed),
                        memory_mb=memory_mb,
                        exec_id="sql-prepare",
                    )
                    warnings = _broken_views(prepared)
                    annotations, exposing = await _conceal_views(prepared, annotations)
                    warnings += exposing
                    if needs_copy(annotations):
                        copied = await build_sqlite_copy(
                            sandbox,
                            handle,
                            database=original,
                            tables=_copy_tables(prepared, annotations),
                            views=await _copy_views(prepared, annotations),
                            memory_mb=memory_mb,
                            exec_id="sql-copy",
                        )
                        warnings += _mark_skipped(prepared, copied.skipped)
                        size = await runtime.install_copy(copied.path, source.id)
                    else:
                        await runtime.remove_database(source.id)
        except PrepareFailed as exc:
            status = 409 if exc.code == "sql_runtime_missing" else 422
            details = {"file_id": exc.file_id} if exc.file_id is not None else None
            raise ServiceError(status, exc.code, exc.message, details=details) from None
        except SqlRunFailed as exc:
            raise ServiceError(422, exc.code, exc.message) from None
        current_hash = source_hash(source.kind, source.files, annotations)
        snapshot = SqlSchemaSnapshot(
            tables=prepared.tables,
            captured_at=self._clock(),
            source_hash=current_hash,
            duration_ms=prepared.elapsed_ms,
            warnings=warnings,
        )
        strip_hidden_samples(snapshot, annotations)
        async with self._db.write() as session:
            row = await self._active(session, workspace_id, source_id)
            row.table_schema = snapshot.model_dump(mode="json")
            row.annotations = annotations.model_dump(mode="json")
            built = self.executor.query_file(load(row, self._settings))
            row.materialized = (
                SqlMaterialized(
                    filename=built.name,
                    built_at=self._clock(),
                    source_hash=current_hash,
                    bytes=size,
                ).model_dump(mode="json")
                if built is not None and size is not None
                else None
            )
        log.info(
            "sql.source_prepared",
            source_id=str(source_id),
            kind=source.kind,
            tables=len(prepared.tables),
            seconds=round((self._clock() - started).total_seconds(), 2),
            bytes=size,
            warnings=len(snapshot.warnings),
        )
        return await self._out(row)

    def _local_path(self, item: SqlSourceFile) -> Path:
        if self._storage is None:
            raise ServiceError(503, "storage_unavailable", "파일 저장소가 없다")
        try:
            return self._storage.local_path(item.storage_key)
        except StorageError as exc:
            raise ServiceError(422, "sql_source_file_missing", exc.message) from None

    def _prepare_files(
        self, source: LoadedSource, hidden: Mapping[str, set[str]], concealed: Collection[str]
    ) -> list[PrepareFile]:
        files: list[PrepareFile] = []
        for item in source.files:
            table = item.table or default_table_name(item.filename, ())
            files.append(
                PrepareFile(
                    file_id=str(item.file_id),
                    path=self._local_path(item),
                    filename=item.filename,
                    format=item.format,
                    table=table,
                    sheet=item.sheet,
                    header=item.options.header,
                    encoding=item.options.encoding,
                    all_varchar=item.options.all_varchar,
                    hidden=sorted(hidden.get(table.lower(), set())),
                    table_hidden=table.lower() in concealed,
                )
            )
        return files

    async def update_annotations(
        self, workspace_id: uuid.UUID, source_id: uuid.UUID, body: SqlAnnotationsUpdate
    ) -> SqlSourceOut:
        """표·열 설명과 숨김을 바꾼다. 숨김이 바뀌면 옛 질의 파일·사본을 바로 지우고 다시 준비한다
        (prepare=false면 stale로 두어 준비 전까지 질의를 막는다)."""
        annotations = SqlAnnotations(tables=body.tables)
        async with self._db.write() as session:
            row = await self._active(session, workspace_id, source_id)
            before = load(row, self._settings)
            changed = (
                hidden_columns(before.annotations),
                hidden_tables(before.annotations),
            ) != (hidden_columns(annotations), hidden_tables(annotations))
            row.annotations = annotations.model_dump(mode="json")
            if body.glossary is not None:
                row.glossary = body.glossary
            snapshot = before.snapshot
            if snapshot is not None and changed:
                strip_hidden_samples(snapshot, annotations)
                row.table_schema = snapshot.model_dump(mode="json")
            if changed:
                row.materialized = None
        if changed and self._runtime is not None:
            await self._runtime.remove_database(source_id)
        if changed and body.prepare and snapshot is not None:
            try:
                return await self.prepare(workspace_id, source_id)
            except ServiceError as exc:
                out = await self._out(row)
                out.status.message = f"숨김을 바꿨지만 다시 준비하지 못했다: {exc.message}"
                return out
        return await self._out(row)

    async def query(
        self, workspace_id: uuid.UUID, source_id: uuid.UUID, body: SqlQueryRequest
    ) -> SqlQueryResult:
        """SQL 시험(origin=test). 검증·실행 실패는 ok=false로, 준비 전·런타임 없음은 409로
        알린다."""
        executor = self.executor
        try:
            source = await executor.source(workspace_id, source_id)
        except SqlUnavailable as exc:
            raise exc.service_error() from None
        max_rows = executor.max_rows(body.max_rows, source)
        try:
            executed = await executor.execute(
                source,
                body.sql,
                origin="test",
                max_rows=max_rows,
                timeout_sec=source.limits.timeout_sec,
            )
        except SqlUnavailable as exc:
            raise exc.service_error() from None
        except SqlRejected as exc:
            return SqlQueryResult(
                ok=False, error=SqlFailure(code=exc.code, message=exc.message, stage="validate")
            )
        except SqlRunFailed as exc:
            return SqlQueryResult(
                ok=False, error=SqlFailure(code=exc.code, message=exc.message, stage="execute")
            )
        output = executed.output
        return SqlQueryResult(
            ok=True,
            sql_executed=executed.validated.sql,
            columns=executed.columns,
            rows=executed.rows,
            row_count=output.row_count,
            truncated=output.truncated,
            rows_cut=executed.rows_cut,
            result_text=executor.text(
                source,
                executed,
                max_rows=max_rows,
                llm_rows=source.limits.llm_rows,
                llm_bytes=source.limits.llm_bytes,
            ),
            elapsed_ms=output.elapsed_ms,
            log_id=executed.log_id,
        )

    async def query_logs(
        self, workspace_id: uuid.UUID, source_id: uuid.UUID, *, page: int, limit: int
    ) -> SqlQueryLogList:
        async with self._db.read() as session:
            await self._active(session, workspace_id, source_id)
            logs = SqlQueryLogRepository(session, workspace_id)
            rows = await logs.list_page(source_id, limit=limit, offset=(page - 1) * limit)
            total = await logs.count(source_id)
        return SqlQueryLogList(
            items=[
                SqlQueryLogOut(
                    id=row.id,
                    origin=row.origin,  # pyright: ignore[reportArgumentType]
                    sql=row.sql,
                    status=row.status,  # pyright: ignore[reportArgumentType]
                    reject_code=row.reject_code,
                    row_count=row.row_count,
                    duration_ms=row.duration_ms,
                    error=row.error,
                    created_at=row.created_at,
                )
                for row in rows
            ],
            total=total,
            page=page,
            limit=limit,
        )

    async def schema(
        self,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        *,
        tables: Sequence[str] = (),
        include_samples: bool = True,
        max_tokens: int = DEFAULT_SCHEMA_TOKENS,
    ) -> SqlSchemaText:
        async with self._db.read() as session:
            row = await self._active(session, workspace_id, source_id)
            source = load(row, self._settings)
        text = describe(
            source, tables=tables, include_samples=include_samples, max_tokens=max_tokens
        )
        return SqlSchemaText(
            ddl=text.ddl,
            tables=text.tables,
            glossary=text.glossary,
            truncated=text.truncated,
            tokens=text.tokens,
        )

    async def export_schema(
        self,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        kb_id: uuid.UUID,
        documents: "KnowledgeDocumentsPort",
    ) -> SqlExportSchemaOut:
        """보이는 표마다 문서 하나(DDL·설명·샘플값)를 지식 베이스에 넣는다(10.7).

        다시 내보내면 표마다 새 문서를 만든 뒤 같은 이름(소스 이름 · 표)으로 앞서 API로 넣은 문서를
        지운다(knowledge_index의 replace와 같은 순서). 한 표가 실패해도 나머지 표는 계속하고, 실패한
        표의 이전 문서는 남긴다. 소스 이름을 바꾼 뒤에는 옛 이름의 문서를 찾지 못한다.
        """
        async with self._db.read() as session:
            row = await self._active(session, workspace_id, source_id)
            source = load(row, self._settings)
        if source.snapshot is None:
            raise ServiceError(
                409, ERR_NOT_PREPARED, "데이터 소스를 먼저 준비해야 스키마를 내보낼 수 있다"
            )
        exported: list[SqlExportedDocument] = []
        removed: list[uuid.UUID] = []
        failed: list[SqlExportFailure] = []
        for table in source.view.tables:
            name = f"{source.name} · {table.name}"[:255]
            try:
                created = await documents.create_from_text(
                    workspace_id,
                    kb_id,
                    KbDocumentTextCreate(
                        name=name,
                        text=table_document(source.view, table, source_name=source.name),
                        allow_duplicate=True,
                    ),
                    data_source_type=KbDataSourceType.API,
                )
            except ServiceError as exc:
                if exc.status == 404:
                    raise
                failed.append(
                    SqlExportFailure(table=table.name, code=exc.code, message=exc.message)
                )
                continue
            fresh = {document.id for document in created.documents}
            for document in created.documents:
                exported.append(
                    SqlExportedDocument(table=table.name, document_id=document.id, name=name)
                )
            try:
                removed += await _remove_exports(documents, workspace_id, kb_id, name, fresh)
            except ServiceError as exc:
                failed.append(
                    SqlExportFailure(
                        table=table.name,
                        code=exc.code,
                        message=f"새 문서는 만들었지만 이전 문서를 지우지 못했다: {exc.message}",
                    )
                )
        log.info(
            "sql.schema_exported",
            source_id=str(source_id),
            kb_id=str(kb_id),
            documents=len(exported),
            removed=len(removed),
            failed=len(failed),
        )
        return SqlExportSchemaOut(kb_id=kb_id, documents=exported, removed=removed, failed=failed)

    async def add_example(
        self,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID,
        body: SqlExampleCreate,
        documents: "KnowledgeDocumentsPort",
    ) -> SqlExampleOut:
        """(질문, SQL) 한 쌍을 Q&A 지식 베이스의 문서 하나로 넣는다(10.7 예시로 저장)."""
        async with self._db.read() as session:
            row = await self._active(session, workspace_id, source_id)
            kb = await KnowledgeBaseRepository(session, workspace_id).get_active(body.kb_id)
            if kb is None:
                raise not_found("지식 베이스")
            if kb.doc_form != KbDocForm.QA:
                raise ServiceError(
                    422,
                    "kb_not_qa",
                    "예시 SQL은 Q&A 모드 지식 베이스에만 넣는다(질문으로 찾아 SQL을 돌려준다)",
                )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["question", "answer"])
        writer.writerow([body.question, body.sql])
        name = f"{row.name} SQL 예시: {body.question}"[:255]
        created = await documents.create_from_text(
            workspace_id,
            body.kb_id,
            KbDocumentTextCreate(name=name, text=buffer.getvalue()),
            data_source_type=KbDataSourceType.API,
        )
        return SqlExampleOut(kb_id=body.kb_id, document_id=created.documents[0].id, name=name)


class KnowledgeDocumentsPort(Protocol):
    """export_schema·add_example이 부르는 지식 문서 서비스의 모양(KnowledgeDocumentService).

    지식 쪽 코드는 고치지 않고 문서 만들기·찾기·지우기를 부르기만 한다(10.7).
    """

    async def create_from_text(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        body: KbDocumentTextCreate,
        *,
        uploader: str | None = None,
        data_source_type: KbDataSourceType = KbDataSourceType.PASTE_TEXT,
    ) -> KbDocumentsCreated: ...

    async def list_documents(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        *,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> KbDocumentList: ...

    async def delete_document(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, document_id: uuid.UUID
    ) -> None: ...


_EXPORT_PAGE = 100


async def _remove_exports(
    documents: KnowledgeDocumentsPort,
    workspace_id: uuid.UUID,
    kb_id: uuid.UUID,
    name: str,
    keep: Collection[uuid.UUID],
) -> list[uuid.UUID]:
    """이름이 꼭 같고 API로 넣은 이전 내보내기 문서를 지운다. 지운 id를 돌려준다."""
    stale: list[uuid.UUID] = []
    page = 1
    while True:
        listed = await documents.list_documents(
            workspace_id, kb_id, keyword=name, page=page, limit=_EXPORT_PAGE
        )
        stale += [
            item.id
            for item in listed.items
            if item.name == name and item.data_source_type == "api" and item.id not in keep
        ]
        if not listed.has_more:
            break
        page += 1
    for document_id in stale:
        await documents.delete_document(workspace_id, kb_id, document_id)
    return stale


def describe(
    source: LoadedSource,
    *,
    tables: Sequence[str] = (),
    include_samples: bool = True,
    max_tokens: int = DEFAULT_SCHEMA_TOKENS,
) -> SchemaText:
    return schema_text(
        source.view,
        name=source.name,
        glossary=source.glossary,
        tables=tables,
        include_samples=include_samples,
        max_tokens=max_tokens,
    )


def sql_node_options(settings: Settings) -> SqlNodeOptions:
    defaults = default_limits(settings)
    return SqlNodeOptions(
        max_rows_max=settings.sql_max_rows,
        timeout_max=SQL_TIMEOUT_CAP,
        default_max_rows=defaults.max_rows,
        default_timeout_sec=defaults.timeout_sec,
        default_memory_mb=defaults.memory_mb,
        default_llm_rows=defaults.llm_rows,
        default_llm_bytes=defaults.llm_bytes,
        llm_bytes_max=LLM_BYTES_CAP,
        default_schema_tokens=DEFAULT_SCHEMA_TOKENS,
        approvals=["none", "always"],
    )


async def sql_source_refs(
    session: AsyncSession, workspace_id: uuid.UUID, source_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, SqlSourceRef]:
    """살아 있는 데이터 소스의 검증용 정보(이름, 유료 전용인지)."""
    if not source_ids:
        return {}
    rows = await SqlSourceRepository(session, workspace_id).get_many_active(source_ids)
    return {
        row.id: SqlSourceRef(
            id=row.id, name=row.name, slug=row.slug, paid=sql_key_tier_of(row) == "paid"
        )
        for row in rows
    }


async def graph_sql_sources(
    session: AsyncSession, workspace_id: uuid.UUID, graph: Graph | None
) -> dict[uuid.UUID, SqlSourceRef]:
    """그래프의 SQL 노드가 가리키는 살아 있는 데이터 소스(graph_knowledge와 같은 자리)."""
    if graph is None:
        return {}
    return await sql_source_refs(session, workspace_id, sql_references(graph))


async def sql_choices(
    session: AsyncSession, workspace_id: uuid.UUID, source_ids: Sequence[uuid.UUID]
) -> list[SqlSourceChoice]:
    """에이전트 런이 쓸 데이터 소스(버전에 고른 순서). 지운 것은 뺀다."""
    if not source_ids:
        return []
    rows = await SqlSourceRepository(session, workspace_id).get_many_active(source_ids)
    by_id = {row.id: row for row in rows}
    return [
        SqlSourceChoice(
            source_id=row.id,
            slug=row.slug,
            name=row.name,
            description=row.description,
            dialect=dialect_of(row.kind),
        )
        for source_id in source_ids
        if (row := by_id.get(source_id)) is not None
    ]


class WorkflowSqlPort:
    """sql_schema·sql_query 노드의 SqlPort(origin=node). 런의 워크스페이스로 SqlExecutor를
    부른다."""

    def __init__(self, executor: SqlExecutor, workspace_id: uuid.UUID) -> None:
        self._executor = executor
        self._workspace_id = workspace_id

    async def _source(self, source_id: uuid.UUID) -> LoadedSource:
        try:
            return await self._executor.source(self._workspace_id, source_id)
        except SqlUnavailable as exc:
            raise SqlPortError(
                exc.code, exc.message, stage="source", retryable=exc.retryable
            ) from None

    async def describe(self, source_id: uuid.UUID) -> SqlSourceInfo:
        source = await self._source(source_id)
        if source.snapshot is None:
            raise SqlPortError(
                ERR_NOT_PREPARED,
                f"데이터 소스 {source.name}을(를) 먼저 준비해야 한다(스키마가 없다)",
                stage="source",
            )
        limits = source.limits
        return SqlSourceInfo(
            id=source.id,
            name=source.name,
            slug=source.slug,
            view=source.view,
            glossary=source.glossary,
            approval="always" if source.approval == "always" else "none",
            max_rows=self._executor.max_rows(None, source),
            timeout_sec=limits.timeout_sec,
            llm_rows=limits.llm_rows,
            llm_bytes=limits.llm_bytes,
            max_rows_cap=self._executor.settings.sql_max_rows,
        )

    async def validate(self, source_id: uuid.UUID, sql: str, *, max_rows: int) -> str:
        source = await self._source(source_id)
        try:
            validated = await self._executor.validate(source, sql, max_rows=max_rows, origin="node")
        except SqlRejected as exc:
            raise SqlPortError(exc.code, exc.message, stage="validate") from None
        return validated.sql

    async def execute(
        self,
        source_id: uuid.UUID,
        sql: str,
        *,
        max_rows: int,
        timeout_sec: int,
        llm_rows: int,
        llm_bytes: int,
        save_csv: CsvSaver | None,
    ) -> SqlRun:
        source = await self._source(source_id)
        saved: list[dict[str, Any] | None] = []

        async def on_csv(path: Path) -> None:
            if save_csv is not None:
                saved.append(await save_csv(path))

        try:
            executed = await self._executor.execute(
                source,
                sql,
                origin="node",
                max_rows=max_rows,
                timeout_sec=timeout_sec,
                export_csv=save_csv is not None,
                on_csv=on_csv,
            )
        except SqlUnavailable as exc:
            raise SqlPortError(
                exc.code, exc.message, stage="source", retryable=exc.retryable
            ) from None
        except SqlRejected as exc:
            raise SqlPortError(exc.code, exc.message, stage="validate") from None
        except SqlRunFailed as exc:
            raise SqlPortError(
                exc.code, exc.message, stage="execute", retryable=exc.retryable
            ) from None
        output = executed.output
        return SqlRun(
            sql_executed=executed.validated.sql,
            columns=executed.columns,
            rows=executed.rows,
            row_count=output.row_count,
            truncated=output.truncated,
            rows_cut=executed.rows_cut,
            result_text=self._executor.text(
                source, executed, max_rows=max_rows, llm_rows=llm_rows, llm_bytes=llm_bytes
            ),
            elapsed_ms=output.elapsed_ms,
            file=saved[0] if saved else None,
        )


class AgentSqlPort:
    """describe_sql_source·query_sql의 SqlToolPort(origin=tool). 잘린 결과는 에이전트 작업공간에
    CSV로 복사한다(런이 끝나면 file.changed로 보인다)."""

    def __init__(
        self, executor: SqlExecutor, workspace_id: uuid.UUID, provisioner: WorkspaceProvisioner
    ) -> None:
        self._executor = executor
        self._workspace_id = workspace_id
        self._provisioner = provisioner

    async def _source(self, source_id: uuid.UUID) -> LoadedSource:
        try:
            return await self._executor.source(self._workspace_id, source_id)
        except SqlUnavailable as exc:
            raise SqlToolError(exc.code, exc.message) from None

    async def describe(self, source_id: uuid.UUID) -> str:
        source = await self._source(source_id)
        if source.snapshot is None:
            raise SqlToolError(
                ERR_NOT_PREPARED, f"데이터 소스 {source.name}은(는) 아직 준비되지 않았다"
            )
        return describe(source).ddl

    async def query(
        self,
        source_id: uuid.UUID,
        sql: str,
        *,
        max_rows: int | None,
        handle: WorkspaceHandle,
        csv_path: str,
    ) -> SqlToolResult:
        source = await self._source(source_id)
        rows_limit = self._executor.max_rows(max_rows, source)
        copied: list[str] = []

        async def on_csv(path: Path) -> None:
            try:
                await self._provisioner.copy_file_in(handle, csv_path, path)
            except (SandboxError, OSError) as exc:
                log.warning("sql.tool_csv_copy_failed", error=str(exc))
                return
            copied.append(csv_path)

        try:
            executed = await self._executor.execute(
                source,
                sql,
                origin="tool",
                max_rows=rows_limit,
                timeout_sec=source.limits.timeout_sec,
                export_csv=True,
                on_csv=on_csv,
            )
        except (SqlUnavailable, SqlRejected, SqlRunFailed) as exc:
            raise SqlToolError(exc.code, exc.message) from None
        output = executed.output
        return SqlToolResult(
            text=self._executor.text(
                source,
                executed,
                max_rows=rows_limit,
                llm_rows=source.limits.llm_rows,
                llm_bytes=source.limits.llm_bytes,
            ),
            sql_executed=executed.validated.sql,
            row_count=output.row_count,
            truncated=output.truncated,
            csv_path=copied[0] if copied else None,
        )


# 행이 가리키지 않는 원본·질의 파일을 남겨 두는 기간(4.5)과 옮기다 남은 조각의 기간.
LOOSE_FILE_KEEP = timedelta(days=7)
PARTIAL_KEEP = timedelta(hours=1)


def _stale_cache(root: Path, cutoff: float, partial_cutoff: float) -> list[tuple[Path, str]]:
    """(경로, 소스 id 또는 빈 글). 조각(.part)은 소스 id 없이 돌려준다."""
    found: list[tuple[Path, str]] = []
    if not root.is_dir():
        return found
    for path in root.iterdir():
        if not path.is_file():
            continue
        mtime = path.stat().st_mtime
        if path.name.endswith(".part"):
            if mtime < partial_cutoff:
                found.append((path, ""))
        elif path.suffix in (".duckdb", ".sqlite") and mtime < cutoff:
            found.append((path, path.stem))
    return found


def _uploads(root: Path) -> dict[uuid.UUID, list[tuple[str, float]]]:
    """워크스페이스 → (파일 이름, mtime)."""
    found: dict[uuid.UUID, list[tuple[str, float]]] = {}
    if not root.is_dir():
        return found
    for folder in root.iterdir():
        try:
            workspace_id = uuid.UUID(folder.name)
        except ValueError:
            continue
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.is_file():
                found.setdefault(workspace_id, []).append((path.name, path.stat().st_mtime))
    return found


class SqlMaintenance:
    """매시 정리(4.5): 질의 기록 보존(KB_QUERY_LOG_DAYS), cache/kb-sql의 행 없는 질의 파일·사본,
    지운 데이터 소스의 원본(지운 뒤 7일)과 어느 행도 가리키지 않는 원본(올린 뒤 7일)."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        storage: StorageBackend | None,
        runtime: SqlRuntime | None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.db = db
        self.settings = settings
        self.storage = storage
        self.runtime = runtime
        self.clock = clock

    async def _sources(self, *, deleted: bool = False) -> dict[uuid.UUID, list[SqlSource]]:
        """워크스페이스 → 데이터 소스 행. deleted면 지운 행까지다."""
        async with self.db.read() as session:
            workspaces = await WorkspaceRepository(session).list_all()
            found: dict[uuid.UUID, list[SqlSource]] = {}
            for workspace in workspaces:
                sources = SqlSourceRepository(session, workspace.id)
                found[workspace.id] = (
                    await sources.list_all() if deleted else await sources.list_active()
                )
        return found

    async def purge_query_logs(self) -> int:
        cutoff = self.clock() - timedelta(days=self.settings.kb_query_log_days)
        deleted = 0
        for workspace_id in await self._sources():
            async with self.db.write() as session:
                deleted += await SqlQueryLogRepository(session, workspace_id).delete_before(cutoff)
        return deleted

    async def purge_cache(self) -> int:
        """cache/kb-sql의 <id>.duckdb·<id>.sqlite 중 살아 있는 소스의 것이 아니고 7일이 지난 것.
        소스를 지우거나 숨김을 바꾸면 바로 지우므로 여기서는 남은 것만 거둔다."""
        if self.runtime is None:
            return 0
        now = self.clock()
        stale = await asyncio.to_thread(
            _stale_cache,
            self.runtime.cache_dir,
            (now - LOOSE_FILE_KEEP).timestamp(),
            (now - PARTIAL_KEEP).timestamp(),
        )
        if not stale:
            return 0
        live = {str(row.id) for rows in (await self._sources()).values() for row in rows}
        removed = 0
        for path, source_id in stale:
            if source_id and source_id in live:
                continue
            try:
                await asyncio.to_thread(path.unlink, True)
            except OSError as exc:
                log.warning("sql.cache_delete_failed", path=path.name, error=str(exc))
                continue
            removed += 1
        return removed

    async def purge_loose_files(self) -> int:
        """sql-sources/<워크스페이스>/ 아래 원본을 지운다. 지운 데이터 소스의 것은 지운
        시각(deleted_at)에서, 어느 행도 가리키지 않는 것은 올린 시각(mtime)에서 7일이 지난 것이다.
        살아 있는 소스가 가리키는 원본은 그대로 둔다."""
        if self.storage is None:
            return 0
        try:
            root = self.storage.local_path("sql-sources")
        except StorageError:
            return 0
        uploads = await asyncio.to_thread(_uploads, root)
        if not uploads:
            return 0
        now = self.clock()
        cutoff = now - LOOSE_FILE_KEEP
        sources = await self._sources(deleted=True)
        removed = 0
        for workspace_id, files in uploads.items():
            live: set[str] = set()
            deleted_at: dict[str, datetime] = {}
            for row in sources.get(workspace_id, []):
                for item in parse_files(row.files):
                    if row.deleted_at is None:
                        live.add(item.storage_key)
                    else:
                        known = deleted_at.get(item.storage_key)
                        if known is None or row.deleted_at > known:
                            deleted_at[item.storage_key] = row.deleted_at
            for name, mtime in files:
                key = f"sql-sources/{workspace_id}/{name}"
                if key in live:
                    continue
                gone = deleted_at.get(key)
                if gone is not None:
                    if gone > cutoff:
                        continue
                elif mtime > cutoff.timestamp():
                    continue
                try:
                    if await self.storage.delete(key):
                        removed += 1
                except (OSError, StorageError) as exc:
                    log.warning("sql.source_file_delete_failed", error=str(exc))
        return removed
