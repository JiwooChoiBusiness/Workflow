"""KB 단위 서비스 API(3단계 설계 8.5, 확장). 경로는 Dify와 같은 datasets다.

지식 베이스 키만 받고(다른 대상의 키는 400 wrong_target) 키에 묶인 KB 하나만 다룬다. 다른 KB의
id는 404다. 문서·청크·메타데이터 조작은 콘솔과 같은 서비스를 부르고, 목록은 Dify처럼 {data,
has_more, limit, total, page}로 감싼다.

색인 파일을 쓸 수 없을 때(503 index_unavailable)는 콘솔과 달리 사유를 싣지 않는다. 사유는 예외
원문이라 서버 안의 경로가 들어갈 수 있다.
"""

import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ValidationError
from starlette.datastructures import UploadFile

from app.api.console.knowledge import MetadataServiceDep
from app.api.console.knowledge_documents import DocumentServiceDep, SegmentServiceDep
from app.api.console.knowledge_search import get_search_service, retry_after_headers
from app.api.errors import ApiError
from app.api.uploads import MULTIPART_OVERHEAD, upload_chunks
from app.api.v1.common import dataset_of
from app.auth.dependencies import ApiKeyAuth, DbDep
from app.db.models.enums import KbDataSourceType
from app.knowledge.index_db import ERR_INDEX_UNAVAILABLE
from app.schemas.knowledge import (
    ChildChunkCreate,
    ChildChunkPatch,
    DisplayStatus,
    DocumentMetadataList,
    DocumentMetadataUpdate,
    KbDocumentTextCreate,
    KbDocumentUploadOptions,
    MetadataFieldCreate,
    MetadataFieldList,
    MetadataFieldOut,
    MetadataFieldPatch,
)
from app.schemas.retrieval import RetrieveResponse
from app.schemas.service_api import (
    DatasetChildChunkList,
    DatasetChildChunkOut,
    DatasetDocumentCreated,
    DatasetDocumentList,
    DatasetFileOptions,
    DatasetFileUpdateOptions,
    DatasetIndexingStatus,
    DatasetList,
    DatasetOut,
    DatasetRetrieveRequest,
    DatasetSegmentList,
    DatasetSegmentOut,
    DatasetSegmentsCreate,
    DatasetSegmentsCreated,
    DatasetSegmentUpdate,
    DatasetStatusChange,
    DatasetStatusResult,
    DatasetTextCreate,
    DatasetTextUpdate,
    DifyDocumentMetadataUpdate,
)
from app.services.errors import ServiceError
from app.services.knowledge_datasets import KnowledgeDatasetService
from app.services.knowledge_documents import UploadSource
from app.services.knowledge_search import KnowledgeSearchService

UNAVAILABLE_MESSAGE = "지식 색인을 지금 쓸 수 없다. 잠시 뒤 다시 시도한다"


class DatasetRoute(APIRoute):
    """index_unavailable의 사유를 빼고, 검색 한도(429)에는 Retry-After 헤더를 단다."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except ServiceError as exc:
                if exc.code == ERR_INDEX_UNAVAILABLE:
                    raise ApiError(
                        503, exc.code, UNAVAILABLE_MESSAGE, details={"retryable": True}
                    ) from None
                headers = retry_after_headers(exc)
                if headers is None:
                    raise
                raise ApiError(
                    exc.status, exc.code, exc.message, details=exc.details, headers=headers
                ) from exc

        return handle


router = APIRouter(prefix="/datasets", tags=["service-api"], route_class=DatasetRoute)

_JSON_SLACK = 64 * 1024


def get_dataset_service(db: DbDep) -> KnowledgeDatasetService:
    return KnowledgeDatasetService(db)


DatasetServiceDep = Annotated[KnowledgeDatasetService, Depends(get_dataset_service)]
SearchServiceDep = Annotated[KnowledgeSearchService, Depends(get_search_service)]


@router.get("", response_model=DatasetList)
async def list_datasets(key: ApiKeyAuth, datasets: DatasetServiceDep) -> DatasetList:
    """키에 묶인 지식 베이스 하나. 워크스페이스 전체 범위 키는 없다(8.5)."""
    dataset = await datasets.dataset(key.workspace_id, dataset_of(key))
    return DatasetList(data=[dataset], has_more=False, limit=20, total=1, page=1)


@router.get("/{dataset_id}", response_model=DatasetOut)
async def get_dataset(
    dataset_id: uuid.UUID, key: ApiKeyAuth, datasets: DatasetServiceDep
) -> DatasetOut:
    return await datasets.dataset(key.workspace_id, dataset_of(key, dataset_id))


@router.post("/{dataset_id}/retrieve", response_model=RetrieveResponse)
async def retrieve(
    dataset_id: uuid.UUID,
    body: DatasetRetrieveRequest,
    key: ApiKeyAuth,
    search: SearchServiceDep,
) -> RetrieveResponse:
    """검색(Dify retrieve). retrieval_model은 우리 모양과 Dify 모양을 다 받고, 비우면 KB 설정이다.

    메타데이터 조건은 metadata_filtering_conditions(또는 Dify처럼 retrieval_model 안)로 준다. 자동
    필터(mode=automatic)는 KB 키로 채팅 호출을 쓰게 되므로 받지 않는다(422).
    """
    conditions = body.metadata_filtering_conditions
    if conditions is not None and conditions.mode == "automatic":
        raise ApiError(
            422,
            "auto_filter_not_supported",
            "서비스 API에서는 메타데이터 자동 필터를 쓸 수 없다. 조건을 직접 준다",
        )
    result = await search.retrieve(
        key.workspace_id,
        [dataset_of(key, dataset_id)],
        body.query,
        body.retrieval_model,
        metadata_filter=body.metadata_filtering_conditions,
        source="api",
    )
    return result.response()


def _validation(exc: ValidationError, *prefix: str | int) -> ApiError:
    details = [
        {"loc": ["body", *prefix, *error["loc"]], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return ApiError(422, "validation_error", "요청 값이 올바르지 않다", details=details)


def _parse[ModelT: BaseModel](model: type[ModelT], raw: bytes | str, *prefix: str | int) -> ModelT:
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise _validation(exc, *prefix) from None


def _length(request: Request, limit: int, *, required: bool, code: str) -> None:
    raw = request.headers.get("content-length")
    if raw is None:
        if required:
            raise ApiError(411, "length_required", "Content-Length 헤더가 필요하다")
        return
    try:
        length = int(raw)
    except ValueError:
        raise ApiError(400, "bad_request", "Content-Length 값이 올바르지 않다") from None
    if length > limit:
        raise ApiError(413, code, "요청 본문이 너무 크다", details={"max_bytes": limit})


async def _read_json(request: Request, limit: int) -> bytes:
    _length(request, limit, required=False, code="text_too_large")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(
                413, "text_too_large", "요청 본문이 너무 크다", details={"max_bytes": limit}
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _text_openapi() -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["name", "text"],
                        "properties": {
                            "name": {"type": "string", "maxLength": 255},
                            "text": {"type": "string"},
                            "process_rule": {"type": "object"},
                            "doc_metadata": {"type": "object"},
                            "allow_duplicate": {"type": "boolean"},
                        },
                    }
                }
            },
        }
    }


@router.post(
    "/{dataset_id}/document/create-by-text",
    response_model=DatasetDocumentCreated,
    openapi_extra=_text_openapi(),
)
async def create_by_text(
    dataset_id: uuid.UUID,
    request: Request,
    key: ApiKeyAuth,
    documents: DocumentServiceDep,
    datasets: DatasetServiceDep,
) -> DatasetDocumentCreated:
    """글 하나로 문서를 만든다. 색인은 지식 레인이 뒤에서 하고 batch로 상태를 본다.

    doc_metadata는 이 KB에 있는 필드 이름 → 값이다. 없는 필드면 문서를 만들지 않고 422다.
    """
    kb_id = dataset_of(key, dataset_id)
    body = _parse(
        DatasetTextCreate, await _read_json(request, documents.max_text_bytes * 2 + _JSON_SLACK)
    )
    values = await datasets.metadata_fields(key.workspace_id, kb_id, body.doc_metadata or {})
    created = await documents.create_from_text(
        key.workspace_id,
        kb_id,
        KbDocumentTextCreate(
            name=body.name,
            text=body.text,
            process_rule=body.process_rule,
            allow_duplicate=body.allow_duplicate,
        ),
        data_source_type=KbDataSourceType.API,
    )
    document = created.documents[0]
    if values:
        await datasets.apply_metadata(key.workspace_id, kb_id, document.id, values)
        document = await documents.get_document(key.workspace_id, kb_id, document.id)
    return DatasetDocumentCreated(document=document, batch=created.batch)


def _file_openapi() -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "data": {"type": "string", "description": "DatasetFileOptions JSON"},
                        },
                    }
                }
            },
        }
    }


@router.post(
    "/{dataset_id}/document/create-by-file",
    response_model=DatasetDocumentCreated,
    openapi_extra=_file_openapi(),
)
async def create_by_file(
    dataset_id: uuid.UUID,
    request: Request,
    key: ApiKeyAuth,
    documents: DocumentServiceDep,
    datasets: DatasetServiceDep,
) -> DatasetDocumentCreated:
    """multipart file 하나와 선택 칸 data(JSON 옵션)로 문서를 만든다. 형식·크기 제한은 콘솔과 같다
    (txt·md·csv·html·pdf·docx가 아니면 415, KB_UPLOAD_MAX_MB를 넘으면 413)."""
    kb_id = dataset_of(key, dataset_id)
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise ApiError(415, "multipart_required", "multipart/form-data로 파일을 보낸다")
    _length(
        request,
        documents.max_upload_bytes + MULTIPART_OVERHEAD,
        required=True,
        code="file_too_large",
    )
    form = await request.form(max_files=1, max_fields=4)
    try:
        item = form.get("file")
        if not isinstance(item, UploadFile):
            raise ApiError(
                422,
                "validation_error",
                "요청 값이 올바르지 않다",
                details=[{"loc": ["body", "file"], "msg": "파일이 없다", "type": "missing"}],
            )
        raw = form.get("data")
        options = (
            _parse(DatasetFileOptions, raw, "data")
            if isinstance(raw, str) and raw.strip()
            else DatasetFileOptions()
        )
        values = await datasets.metadata_fields(key.workspace_id, kb_id, options.doc_metadata or {})
        created = await documents.create_from_uploads(
            key.workspace_id,
            kb_id,
            [UploadSource(item.filename, item.content_type, upload_chunks(item))],
            KbDocumentUploadOptions(
                process_rule=options.process_rule, allow_duplicate=options.allow_duplicate
            ),
        )
    finally:
        await form.close()
    document = created.documents[0]
    if values:
        await datasets.apply_metadata(key.workspace_id, kb_id, document.id, values)
        document = await documents.get_document(key.workspace_id, kb_id, document.id)
    return DatasetDocumentCreated(document=document, batch=created.batch)


@router.post(
    "/{dataset_id}/documents/{document_id}/update-by-text",
    response_model=DatasetDocumentCreated,
    openapi_extra=_text_openapi(),
)
async def update_by_text(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    key: ApiKeyAuth,
    documents: DocumentServiceDep,
) -> DatasetDocumentCreated:
    """원본을 새 글로 바꾸고 다시 색인한다. 문서 id·메타데이터는 그대로다.

    색인 중이면 400 document_indexing, 보관 문서면 400 document_archived, 같은 KB의 다른 문서와
    내용이 같으면 409 duplicate_document다.
    """
    kb_id = dataset_of(key, dataset_id)
    body = _parse(
        DatasetTextUpdate, await _read_json(request, documents.max_text_bytes * 2 + _JSON_SLACK)
    )
    document = await documents.replace_text(
        key.workspace_id,
        kb_id,
        document_id,
        body.text,
        name=body.name,
        process_rule=body.process_rule,
        allow_duplicate=body.allow_duplicate,
    )
    return DatasetDocumentCreated(document=document, batch=document.batch)


@router.post(
    "/{dataset_id}/documents/{document_id}/update-by-file",
    response_model=DatasetDocumentCreated,
    openapi_extra=_file_openapi(),
)
async def update_by_file(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    key: ApiKeyAuth,
    documents: DocumentServiceDep,
) -> DatasetDocumentCreated:
    """원본을 새 파일(multipart file, 선택 칸 data)로 바꾸고 다시 색인한다. 형식·크기 제한과 오류는
    create-by-file과 update-by-text를 합친 것이다."""
    kb_id = dataset_of(key, dataset_id)
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise ApiError(415, "multipart_required", "multipart/form-data로 파일을 보낸다")
    _length(
        request,
        documents.max_upload_bytes + MULTIPART_OVERHEAD,
        required=True,
        code="file_too_large",
    )
    form = await request.form(max_files=1, max_fields=4)
    try:
        item = form.get("file")
        if not isinstance(item, UploadFile):
            raise ApiError(
                422,
                "validation_error",
                "요청 값이 올바르지 않다",
                details=[{"loc": ["body", "file"], "msg": "파일이 없다", "type": "missing"}],
            )
        raw = form.get("data")
        options = (
            _parse(DatasetFileUpdateOptions, raw, "data")
            if isinstance(raw, str) and raw.strip()
            else DatasetFileUpdateOptions()
        )
        document = await documents.replace_file(
            key.workspace_id,
            kb_id,
            document_id,
            UploadSource(item.filename, item.content_type, upload_chunks(item)),
            name=options.name,
            process_rule=options.process_rule,
            allow_duplicate=options.allow_duplicate,
        )
    finally:
        await form.close()
    return DatasetDocumentCreated(document=document, batch=document.batch)


@router.get("/{dataset_id}/documents", response_model=DatasetDocumentList)
async def list_documents(
    dataset_id: uuid.UUID,
    key: ApiKeyAuth,
    documents: DocumentServiceDep,
    status: DisplayStatus | None = None,
    keyword: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> DatasetDocumentList:
    """지우지 않은 문서(최근 것부터). status는 display_status 값이다."""
    found = await documents.list_documents(
        key.workspace_id,
        dataset_of(key, dataset_id),
        status=status,
        keyword=keyword,
        page=page,
        limit=limit,
    )
    return DatasetDocumentList(
        data=found.items,
        has_more=found.has_more,
        limit=found.limit,
        total=found.total,
        page=found.page,
    )


@router.get("/{dataset_id}/documents/{batch}/indexing-status", response_model=DatasetIndexingStatus)
async def indexing_status(
    dataset_id: uuid.UUID, batch: str, key: ApiKeyAuth, datasets: DatasetServiceDep
) -> DatasetIndexingStatus:
    """create-by-* 응답의 batch로 묶인 문서들의 색인 상태와 진행(completed/total_segments)."""
    return await datasets.indexing_status(key.workspace_id, dataset_of(key, dataset_id), batch)


@router.patch("/{dataset_id}/documents/status/{action}", response_model=DatasetStatusResult)
async def change_document_status(
    dataset_id: uuid.UUID,
    action: Literal["enable", "disable", "archive", "un_archive"],
    body: DatasetStatusChange,
    key: ApiKeyAuth,
    documents: DocumentServiceDep,
) -> DatasetStatusResult:
    """문서 켜기·끄기·보관·보관 풀기(Dify 이름). 색인 중인 문서가 있으면 400 document_indexing.

    상태가 맞지 않아 건너뛴 문서는 skipped에 사유와 함께 남는다.
    """
    changed = await documents.set_status(
        key.workspace_id,
        dataset_of(key, dataset_id),
        "unarchive" if action == "un_archive" else action,
        body.document_ids,
    )
    return DatasetStatusResult(skipped=changed.skipped)


@router.delete("/{dataset_id}/documents/{document_id}", status_code=204)
async def delete_document(
    dataset_id: uuid.UUID, document_id: uuid.UUID, key: ApiKeyAuth, documents: DocumentServiceDep
) -> None:
    await documents.delete_document(key.workspace_id, dataset_of(key, dataset_id), document_id)


async def _doc_form(datasets: KnowledgeDatasetService, key: Any, kb_id: uuid.UUID) -> Any:
    return await datasets.doc_form(key.workspace_id, kb_id)


@router.get("/{dataset_id}/documents/{document_id}/segments", response_model=DatasetSegmentList)
async def list_segments(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    key: ApiKeyAuth,
    segments: SegmentServiceDep,
    datasets: DatasetServiceDep,
    keyword: Annotated[str | None, Query(max_length=200)] = None,
    enabled: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> DatasetSegmentList:
    kb_id = dataset_of(key, dataset_id)
    found = await segments.list_segments(
        key.workspace_id,
        kb_id,
        document_id,
        keyword=keyword,
        enabled=enabled,
        page=page,
        limit=limit,
    )
    return DatasetSegmentList(
        data=found.items,
        doc_form=await _doc_form(datasets, key, kb_id),
        has_more=found.has_more,
        limit=found.limit,
        total=found.total,
        page=found.page,
    )


@router.post(
    "/{dataset_id}/documents/{document_id}/segments", response_model=DatasetSegmentsCreated
)
async def create_segments(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    body: DatasetSegmentsCreate,
    key: ApiKeyAuth,
    segments: SegmentServiceDep,
    datasets: DatasetServiceDep,
) -> DatasetSegmentsCreated:
    """청크를 차례로 더한다(한 번에 1,000개까지). 중간에 실패하면 그 앞까지는 남는다."""
    kb_id = dataset_of(key, dataset_id)
    created = [
        await segments.create_segment(key.workspace_id, kb_id, document_id, item)
        for item in body.segments
    ]
    return DatasetSegmentsCreated(data=created, doc_form=await _doc_form(datasets, key, kb_id))


@router.get(
    "/{dataset_id}/documents/{document_id}/segments/{segment_id}",
    response_model=DatasetSegmentOut,
)
async def get_segment(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    key: ApiKeyAuth,
    segments: SegmentServiceDep,
    datasets: DatasetServiceDep,
) -> DatasetSegmentOut:
    kb_id = dataset_of(key, dataset_id)
    found = await segments.get_segment(key.workspace_id, kb_id, document_id, segment_id)
    return DatasetSegmentOut(data=found, doc_form=await _doc_form(datasets, key, kb_id))


@router.post(
    "/{dataset_id}/documents/{document_id}/segments/{segment_id}",
    response_model=DatasetSegmentOut,
)
async def update_segment(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    body: DatasetSegmentUpdate,
    key: ApiKeyAuth,
    segments: SegmentServiceDep,
    datasets: DatasetServiceDep,
) -> DatasetSegmentOut:
    """Dify처럼 POST {segment: {...}}로 고친다."""
    kb_id = dataset_of(key, dataset_id)
    updated = await segments.update_segment(
        key.workspace_id, kb_id, document_id, segment_id, body.segment
    )
    return DatasetSegmentOut(data=updated, doc_form=await _doc_form(datasets, key, kb_id))


@router.delete("/{dataset_id}/documents/{document_id}/segments/{segment_id}", status_code=204)
async def delete_segment(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    key: ApiKeyAuth,
    segments: SegmentServiceDep,
) -> None:
    await segments.delete_segment(
        key.workspace_id, dataset_of(key, dataset_id), document_id, segment_id
    )


@router.get(
    "/{dataset_id}/documents/{document_id}/segments/{segment_id}/child_chunks",
    response_model=DatasetChildChunkList,
)
async def list_child_chunks(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    key: ApiKeyAuth,
    segments: SegmentServiceDep,
) -> DatasetChildChunkList:
    found = await segments.list_children(
        key.workspace_id, dataset_of(key, dataset_id), document_id, segment_id
    )
    return DatasetChildChunkList(data=found.items, total=len(found.items))


@router.post(
    "/{dataset_id}/documents/{document_id}/segments/{segment_id}/child_chunks",
    response_model=DatasetChildChunkOut,
)
async def create_child_chunk(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    body: ChildChunkCreate,
    key: ApiKeyAuth,
    segments: SegmentServiceDep,
) -> DatasetChildChunkOut:
    created = await segments.create_child(
        key.workspace_id, dataset_of(key, dataset_id), document_id, segment_id, body
    )
    return DatasetChildChunkOut(data=created)


@router.patch(
    "/{dataset_id}/documents/{document_id}/segments/{segment_id}/child_chunks/{child_id}",
    response_model=DatasetChildChunkOut,
)
async def update_child_chunk(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    child_id: uuid.UUID,
    body: ChildChunkPatch,
    key: ApiKeyAuth,
    segments: SegmentServiceDep,
) -> DatasetChildChunkOut:
    updated = await segments.update_child(
        key.workspace_id, dataset_of(key, dataset_id), document_id, segment_id, child_id, body
    )
    return DatasetChildChunkOut(data=updated)


@router.delete(
    "/{dataset_id}/documents/{document_id}/segments/{segment_id}/child_chunks/{child_id}",
    status_code=204,
)
async def delete_child_chunk(
    dataset_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    child_id: uuid.UUID,
    key: ApiKeyAuth,
    segments: SegmentServiceDep,
) -> None:
    await segments.delete_child(
        key.workspace_id, dataset_of(key, dataset_id), document_id, segment_id, child_id
    )


@router.get("/{dataset_id}/metadata", response_model=MetadataFieldList)
async def list_metadata(
    dataset_id: uuid.UUID, key: ApiKeyAuth, metadata: MetadataServiceDep
) -> MetadataFieldList:
    return await metadata.list_fields(key.workspace_id, dataset_of(key, dataset_id))


@router.post("/{dataset_id}/metadata", status_code=201, response_model=MetadataFieldOut)
async def create_metadata(
    dataset_id: uuid.UUID, body: MetadataFieldCreate, key: ApiKeyAuth, metadata: MetadataServiceDep
) -> MetadataFieldOut:
    return await metadata.create_field(key.workspace_id, dataset_of(key, dataset_id), body)


@router.patch("/{dataset_id}/metadata/{field_id}", response_model=MetadataFieldOut)
async def rename_metadata(
    dataset_id: uuid.UUID,
    field_id: uuid.UUID,
    body: MetadataFieldPatch,
    key: ApiKeyAuth,
    metadata: MetadataServiceDep,
) -> MetadataFieldOut:
    return await metadata.rename_field(
        key.workspace_id, dataset_of(key, dataset_id), field_id, body
    )


@router.delete("/{dataset_id}/metadata/{field_id}", status_code=204)
async def delete_metadata(
    dataset_id: uuid.UUID, field_id: uuid.UUID, key: ApiKeyAuth, metadata: MetadataServiceDep
) -> None:
    await metadata.delete_field(key.workspace_id, dataset_of(key, dataset_id), field_id)


@router.post("/{dataset_id}/metadata/built-in/{action}", response_model=MetadataFieldList)
async def toggle_built_in(
    dataset_id: uuid.UUID,
    action: Literal["enable", "disable"],
    key: ApiKeyAuth,
    metadata: MetadataServiceDep,
) -> MetadataFieldList:
    # uploader 필드는 API 키로 켜면 채울 계정 이름이 없다.
    return await metadata.set_built_in(
        key.workspace_id, dataset_of(key, dataset_id), action == "enable", uploader=None
    )


@router.post("/{dataset_id}/documents/metadata", response_model=DocumentMetadataList)
async def update_document_metadata(
    dataset_id: uuid.UUID,
    body: DocumentMetadataUpdate | DifyDocumentMetadataUpdate,
    key: ApiKeyAuth,
    metadata: MetadataServiceDep,
    datasets: DatasetServiceDep,
) -> DocumentMetadataList:
    """문서 메타데이터 값. 콘솔과 같은 모양({document_ids, values, apply_to_all})과 Dify 모양
    ({operation_data: [{document_id, metadata_list: [{id, name, value}], partial_update}]})을 다
    받는다. Dify 모양은 목록에 없는 사용자 필드 값을 지우고(partial_update가 참이면 그대로 둔다)
    기본 필드는 건너뛴다. 응답은 두 모양 모두 바뀐 문서의 메타데이터다."""
    kb_id = dataset_of(key, dataset_id)
    if isinstance(body, DifyDocumentMetadataUpdate):
        return await datasets.apply_operations(key.workspace_id, kb_id, body)
    return await metadata.update_values(key.workspace_id, kb_id, body)
