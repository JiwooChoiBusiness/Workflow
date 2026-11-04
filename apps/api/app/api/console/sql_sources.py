"""Text2SQL 데이터 소스 콘솔 API(3단계 설계 12.1).

데이터 소스 목록·생성·설정·삭제, 파일 올리기·지우기, 준비(스냅샷과 질의 파일), 표·열 설명과 숨김,
SQL 시험(origin=test로 노드·도구와 같은 검증·실행 경로), 질의 기록, 스키마 글, 스키마를 지식
베이스로 내보내기, 예시 SQL 저장, DuckDB 런타임 준비·상태. 파일 올리기는 knowledge_documents와 같이
본문을 직접 읽어 Content-Length를 먼저 보고 로그인·CSRF 검사 뒤에 받는다.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from starlette.datastructures import UploadFile

from app.api.errors import ApiError
from app.api.uploads import MULTIPART_OVERHEAD, upload_chunks
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.sql_source import (
    DEFAULT_SCHEMA_TOKENS,
    MAX_SOURCE_FILES,
    SqlAnnotationsUpdate,
    SqlExampleCreate,
    SqlExampleOut,
    SqlExportSchemaOut,
    SqlExportSchemaRequest,
    SqlQueryLogList,
    SqlQueryRequest,
    SqlQueryResult,
    SqlRuntimeStatus,
    SqlSchemaText,
    SqlSourceCreate,
    SqlSourceList,
    SqlSourceOut,
    SqlSourcePatch,
)
from app.services.knowledge_documents import KnowledgeDocumentService
from app.services.knowledge_runtime import KnowledgeRuntime
from app.services.sql_runtime import SqlRuntime
from app.services.sql_sources import SqlSourceService, SqlUpload

router = APIRouter(tags=["sql-sources"])

_FILE_FIELDS = ("files", "files[]", "file")
BASE = "/sql-sources/{source_id}"


def get_sql_service(request: Request, db: DbDep, settings: SettingsDep) -> SqlSourceService:
    state = request.app.state
    return SqlSourceService(
        db, settings, getattr(state, "storage", None), getattr(state, "sql_runtime", None)
    )


def get_document_service(
    request: Request, db: DbDep, settings: SettingsDep
) -> KnowledgeDocumentService:
    # 스키마 내보내기·예시 저장은 지식 문서 서비스의 글 문서 만들기를 부르기만 한다(10.7).
    state = request.app.state
    runtime = getattr(state, "knowledge", None)
    return KnowledgeDocumentService(
        db,
        settings,
        state.storage,
        lane=getattr(state, "knowledge_lane", None),
        sandbox=getattr(state, "kb_sandbox", None),
        limiter=runtime.limiter if isinstance(runtime, KnowledgeRuntime) else None,
    )


def get_runtime(request: Request) -> SqlRuntime:
    runtime = getattr(request.app.state, "sql_runtime", None)
    if not isinstance(runtime, SqlRuntime):
        raise ApiError(503, "sql_unavailable", "SQL 실행 환경이 없다")
    return runtime


SqlServiceDep = Annotated[SqlSourceService, Depends(get_sql_service)]
DocumentServiceDep = Annotated[KnowledgeDocumentService, Depends(get_document_service)]
RuntimeDep = Annotated[SqlRuntime, Depends(get_runtime)]


def _files_openapi() -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                                "maxItems": MAX_SOURCE_FILES,
                            }
                        },
                    }
                }
            },
        }
    }


def _declared_length(request: Request, limit: int) -> None:
    raw = request.headers.get("content-length")
    if raw is None:
        raise ApiError(411, "length_required", "Content-Length 헤더가 필요하다")
    try:
        length = int(raw)
    except ValueError:
        raise ApiError(400, "bad_request", "Content-Length 값이 올바르지 않다") from None
    if length > limit:
        raise ApiError(413, "file_too_large", "요청 본문이 너무 크다", details={"max_bytes": limit})


@router.get("/sql-sources", response_model=SqlSourceList)
async def list_sql_sources(auth: CurrentAuth, service: SqlServiceDep) -> SqlSourceList:
    """살아 있는 데이터 소스. state는 empty·not_prepared·stale·ready다."""
    return await service.list_sources(auth.workspace_id)


@router.post("/sql-sources", status_code=201, response_model=SqlSourceOut)
async def create_sql_source(
    body: SqlSourceCreate, auth: CurrentAuth, service: SqlServiceDep
) -> SqlSourceOut:
    """data_policy.acknowledged가 참이어야 만든다(422 data_policy_not_acknowledged). key_tier=paid는
    GEMINI_PAID_API_KEY가 있어야 한다(422 paid_key_missing). kind는 만든 뒤 바꿀 수 없다."""
    return await service.create_source(auth.workspace_id, body)


@router.get(BASE, response_model=SqlSourceOut)
async def get_sql_source(
    source_id: uuid.UUID, auth: CurrentAuth, service: SqlServiceDep
) -> SqlSourceOut:
    return await service.get_source(auth.workspace_id, source_id)


@router.patch(BASE, response_model=SqlSourceOut)
async def patch_sql_source(
    source_id: uuid.UUID, body: SqlSourcePatch, auth: CurrentAuth, service: SqlServiceDep
) -> SqlSourceOut:
    """파일의 표 이름·시트·옵션을 바꾸면 state가 stale이 되고 다시 준비해야 질의할 수 있다."""
    return await service.update_source(auth.workspace_id, source_id, body)


@router.delete(BASE, status_code=204)
async def delete_sql_source(
    source_id: uuid.UUID, auth: CurrentAuth, service: SqlServiceDep
) -> None:
    """소프트 삭제. 질의 파일은 바로 지우고 원본 파일은 매시 정리가 7일 뒤 지운다."""
    await service.delete_source(auth.workspace_id, source_id)


@router.post(f"{BASE}/files", response_model=SqlSourceOut, openapi_extra=_files_openapi())
async def upload_sql_source_files(
    source_id: uuid.UUID, request: Request, auth: CurrentAuth, service: SqlServiceDep
) -> SqlSourceOut:
    """multipart files(X-Requested-With: fetch). files 소스는 csv·tsv·xlsx를 20개까지, sqlite 소스는
    .db·.sqlite 하나(올리면 바꾼다). 표 이름은 파일 이름에서 만든다. 다른 형식은 415, 파일이
    SQL_UPLOAD_MAX_MB를 넘으면 413이다."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise ApiError(415, "multipart_required", "multipart/form-data(files)로 보낸다")
    _declared_length(request, MAX_SOURCE_FILES * (service.max_upload_bytes + MULTIPART_OVERHEAD))
    form = await request.form(max_files=MAX_SOURCE_FILES + 1, max_fields=2)
    try:
        uploads = [
            SqlUpload(item.filename, item.content_type, upload_chunks(item))
            for name in _FILE_FIELDS
            for item in form.getlist(name)
            if isinstance(item, UploadFile)
        ]
        return await service.add_files(auth.workspace_id, source_id, uploads)
    finally:
        await form.close()


@router.delete(f"{BASE}/files/{{file_id}}", response_model=SqlSourceOut)
async def delete_sql_source_file(
    source_id: uuid.UUID, file_id: uuid.UUID, auth: CurrentAuth, service: SqlServiceDep
) -> SqlSourceOut:
    return await service.delete_file(auth.workspace_id, source_id, file_id)


@router.post(f"{BASE}/prepare", response_model=SqlSourceOut)
async def prepare_sql_source(
    source_id: uuid.UUID, auth: CurrentAuth, service: SqlServiceDep
) -> SqlSourceOut:
    """스키마 스냅샷을 뜨고 files 소스는 보이는 열만 담은 질의 파일을 만든다(끝날 때까지 기다린다).

    files 소스인데 런타임이 없으면 409 sql_runtime_missing, 파일을 읽지 못하면 422
    sql_prepare_failed(details.file_id)다."""
    return await service.prepare(auth.workspace_id, source_id)


@router.patch(f"{BASE}/annotations", response_model=SqlSourceOut)
async def update_sql_source_annotations(
    source_id: uuid.UUID, body: SqlAnnotationsUpdate, auth: CurrentAuth, service: SqlServiceDep
) -> SqlSourceOut:
    """표·열 설명과 숨김을 통째로 바꾼다. files 소스는 숨김이 바뀌면 옛 질의 파일을 지우고(prepare가
    참이면) 다시 준비한다. 다시 준비하지 못하면 status.message에 이유가 있다."""
    return await service.update_annotations(auth.workspace_id, source_id, body)


@router.post(f"{BASE}/query", response_model=SqlQueryResult)
async def query_sql_source(
    source_id: uuid.UUID, body: SqlQueryRequest, auth: CurrentAuth, service: SqlServiceDep
) -> SqlQueryResult:
    """SQL 시험. 검증 거부와 실행 실패는 200에 ok=false·error{code, message, stage}다. 준비 전은 409
    sql_source_not_prepared, files 소스인데 런타임이 없으면 409 sql_runtime_missing이다."""
    return await service.query(auth.workspace_id, source_id, body)


@router.get(f"{BASE}/query-logs", response_model=SqlQueryLogList)
async def sql_source_query_logs(
    source_id: uuid.UUID,
    auth: CurrentAuth,
    service: SqlServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SqlQueryLogList:
    """검증·실행 기록(최근 것부터). 거부도 reject_code와 함께 남는다."""
    return await service.query_logs(auth.workspace_id, source_id, page=page, limit=limit)


@router.get(f"{BASE}/schema", response_model=SqlSchemaText)
async def sql_source_schema(
    source_id: uuid.UUID,
    auth: CurrentAuth,
    service: SqlServiceDep,
    tables: Annotated[list[str] | None, Query(max_length=200)] = None,
    include_samples: bool = True,
    max_tokens: Annotated[int, Query(ge=500, le=64_000)] = DEFAULT_SCHEMA_TOKENS,
) -> SqlSchemaText:
    """sql_schema 노드·describe_sql_source 도구가 모델에 주는 DDL 글과 같은 것(숨긴 열·표 제외)."""
    return await service.schema(
        auth.workspace_id,
        source_id,
        tables=tables or [],
        include_samples=include_samples,
        max_tokens=max_tokens,
    )


@router.post(f"{BASE}/export-schema", status_code=201, response_model=SqlExportSchemaOut)
async def export_sql_source_schema(
    source_id: uuid.UUID,
    body: SqlExportSchemaRequest,
    auth: CurrentAuth,
    service: SqlServiceDep,
    documents: DocumentServiceDep,
) -> SqlExportSchemaOut:
    """보이는 표마다 문서 하나(DDL·설명·샘플값)를 지식 베이스에 넣는다(data_source_type=api)."""
    return await service.export_schema(auth.workspace_id, source_id, body.kb_id, documents)


@router.post(f"{BASE}/examples", status_code=201, response_model=SqlExampleOut)
async def save_sql_example(
    source_id: uuid.UUID,
    body: SqlExampleCreate,
    auth: CurrentAuth,
    service: SqlServiceDep,
    documents: DocumentServiceDep,
) -> SqlExampleOut:
    """(질문, SQL)을 Q&A 지식 베이스의 문서 하나로 넣는다. Q&A가 아니면 422 kb_not_qa, 같은 쌍을
    다시 넣으면 409 duplicate_document다."""
    return await service.add_example(auth.workspace_id, source_id, body, documents)


@router.get("/sql-runtime", response_model=SqlRuntimeStatus)
async def sql_runtime_status(auth: CurrentAuth, runtime: RuntimeDep) -> SqlRuntimeStatus:
    """DuckDB 런타임 상태(missing·installing·ready·failed). sqlite 소스는 런타임 없이 돈다."""
    return await runtime.status()


@router.post("/sql-runtime/prepare", status_code=202, response_model=SqlRuntimeStatus)
async def prepare_sql_runtime(auth: CurrentAuth, runtime: RuntimeDep) -> SqlRuntimeStatus:
    """샌드박스 pip으로 DuckDB를 받고 excel·encodings 확장을 미리 설치한다. 뒤에서 돌고 GET으로
    상태를 본다. 인터넷 연결이 필요하다(확장만 약 220MB)."""
    return await runtime.start_install()
