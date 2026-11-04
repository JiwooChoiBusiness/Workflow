"""지식 베이스 문서·청크 콘솔 API(3단계 설계 12.1).

문서 올리기와 미리보기는 한 경로가 multipart(files와 선택 칸 data = JSON 옵션)와 JSON 둘 다 받는다.
본문을 FastAPI 매개변수로 두지 않고 직접 읽는 것은 Content-Length로 크기를 먼저 보고 멀티파트가
로그인·CSRF 검사보다 먼저 다 받아지지 않게 하려는 것이다(workflow_dsl과 같다).
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ValidationError
from starlette.datastructures import UploadFile

from app.api.downloads import stored_download
from app.api.errors import ApiError
from app.api.uploads import MULTIPART_OVERHEAD, upload_chunks
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.knowledge import (
    ChildChunkCreate,
    ChildChunkList,
    ChildChunkOut,
    ChildChunkPatch,
    DisplayStatus,
    DocumentMetadataList,
    DocumentMetadataUpdate,
    DocumentPreview,
    DocumentPreviewOptions,
    DocumentPreviewText,
    DocumentReindex,
    DocumentStatusAction,
    DocumentStatusResult,
    DocumentStatusUpdate,
    KbDocumentList,
    KbDocumentOut,
    KbDocumentPatch,
    KbDocumentsCreated,
    KbDocumentTextCreate,
    KbDocumentUploadOptions,
    SegmentCreate,
    SegmentList,
    SegmentOut,
    SegmentPatch,
)
from app.schemas.service_api import DatasetIndexingStatus
from app.services.knowledge import MAX_UPLOAD_FILES
from app.services.knowledge_datasets import KnowledgeDatasetService
from app.services.knowledge_documents import KnowledgeDocumentService, UploadSource
from app.services.knowledge_metadata import KnowledgeMetadataService
from app.services.knowledge_runtime import KnowledgeRuntime, optional_knowledge_runtime
from app.services.knowledge_segments import KnowledgeSegmentService

router = APIRouter(tags=["knowledge"])

_FILE_FIELDS = ("files", "files[]")
_PREVIEW_FIELDS = ("file", "files", "files[]")
_JSON_SLACK = 64 * 1024

OptionalRuntimeDep = Annotated[KnowledgeRuntime | None, Depends(optional_knowledge_runtime)]


def get_document_service(
    request: Request, db: DbDep, settings: SettingsDep, runtime: OptionalRuntimeDep
) -> KnowledgeDocumentService:
    state = request.app.state
    return KnowledgeDocumentService(
        db,
        settings,
        state.storage,
        lane=getattr(state, "knowledge_lane", None),
        sandbox=getattr(state, "kb_sandbox", None),
        limiter=runtime.limiter if runtime is not None else None,
    )


def get_segment_service(
    request: Request, db: DbDep, runtime: OptionalRuntimeDep
) -> KnowledgeSegmentService:
    return KnowledgeSegmentService(
        db, runtime=runtime, lane=getattr(request.app.state, "knowledge_lane", None)
    )


def get_metadata_service(db: DbDep) -> KnowledgeMetadataService:
    return KnowledgeMetadataService(db)


def get_dataset_service(db: DbDep) -> KnowledgeDatasetService:
    return KnowledgeDatasetService(db)


DocumentServiceDep = Annotated[KnowledgeDocumentService, Depends(get_document_service)]
SegmentServiceDep = Annotated[KnowledgeSegmentService, Depends(get_segment_service)]
MetadataServiceDep = Annotated[KnowledgeMetadataService, Depends(get_metadata_service)]
DatasetServiceDep = Annotated[KnowledgeDatasetService, Depends(get_dataset_service)]


def _validation_error(exc: ValidationError, *prefix: str | int) -> ApiError:
    details = [
        {"loc": ["body", *prefix, *error["loc"]], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return ApiError(422, "validation_error", "요청 값이 올바르지 않다", details=details)


def _parse[ModelT: BaseModel](model: type[ModelT], raw: bytes | str, *prefix: str | int) -> ModelT:
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise _validation_error(exc, *prefix) from None


def _declared_length(request: Request, limit: int, *, required: bool, code: str) -> None:
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


async def _json_body(request: Request, limit: int) -> bytes:
    _declared_length(request, limit, required=False, code="text_too_large")
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


def _documents_openapi() -> dict[str, Any]:
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
                            "allow_duplicate": {"type": "boolean"},
                        },
                        "additionalProperties": False,
                    }
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                                "maxItems": MAX_UPLOAD_FILES,
                            },
                            "data": {
                                "type": "string",
                                "description": "JSON {process_rule?, allow_duplicate?}",
                            },
                        },
                    }
                },
            },
        }
    }


@router.get("/knowledge/{kb_id}/documents", response_model=KbDocumentList)
async def list_documents(
    kb_id: uuid.UUID,
    auth: CurrentAuth,
    service: DocumentServiceDep,
    status: DisplayStatus | None = None,
    keyword: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KbDocumentList:
    """지우지 않은 문서(최근 것부터). status는 Dify의 display_status 값이다."""
    return await service.list_documents(
        auth.workspace_id, kb_id, status=status, keyword=keyword, page=page, limit=limit
    )


# /documents/{document_id}보다 먼저 둔다. 뒤에 두면 indexing-status가 문서 id 자리에 잡혀
# 422가 된다.
@router.get("/knowledge/{kb_id}/documents/indexing-status", response_model=DatasetIndexingStatus)
async def documents_indexing_status(
    kb_id: uuid.UUID,
    auth: CurrentAuth,
    service: DatasetServiceDep,
    batch: Annotated[str, Query(min_length=1, max_length=64)],
) -> DatasetIndexingStatus:
    """올리기 응답의 batch로 묶인 문서들의 색인 상태와 진행(completed/total_segments).

    서비스 API GET /api/v1/datasets/{id}/documents/{batch}/indexing-status와 같은 계산이다.
    """
    return await service.indexing_status(auth.workspace_id, kb_id, batch)


@router.post(
    "/knowledge/{kb_id}/documents",
    status_code=201,
    response_model=KbDocumentsCreated,
    openapi_extra=_documents_openapi(),
)
async def create_documents(
    kb_id: uuid.UUID, request: Request, auth: CurrentAuth, service: DocumentServiceDep
) -> KbDocumentsCreated:
    """파일(multipart files, X-Requested-With: fetch)이나 글(JSON {name, text})로 문서를 만든다.

    문서는 waiting으로 생기고 한 요청의 문서는 batch 하나로 묶인다. 같은 KB에 내용이 같은 문서가
    있으면 409 duplicate_document(allow_duplicate로 넘긴다). txt·md·csv·html·pdf·docx가 아니면
    415, 파일이 KB_UPLOAD_MAX_MB를 넘으면 413이다.
    """
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    uploader = auth.account.name
    if media_type == "application/json":
        body = _parse(
            KbDocumentTextCreate,
            await _json_body(request, service.max_text_bytes * 2 + _JSON_SLACK),
        )
        return await service.create_from_text(auth.workspace_id, kb_id, body, uploader=uploader)
    if media_type != "multipart/form-data":
        raise ApiError(
            415,
            "unsupported_media_type",
            "multipart/form-data(files) 또는 application/json({name, text})로 보낸다",
        )
    limit = MAX_UPLOAD_FILES * (service.max_upload_bytes + MULTIPART_OVERHEAD)
    _declared_length(request, limit, required=True, code="file_too_large")
    # 개수 초과는 서비스가 422 too_many_files로 알리도록 하나 더 받는다.
    form = await request.form(max_files=MAX_UPLOAD_FILES + 1, max_fields=4)
    try:
        files = [
            item
            for name in _FILE_FIELDS
            for item in form.getlist(name)
            if isinstance(item, UploadFile)
        ]
        raw = form.get("data")
        options = (
            _parse(KbDocumentUploadOptions, raw, "data")
            if isinstance(raw, str) and raw.strip()
            else KbDocumentUploadOptions()
        )
        sources = [
            UploadSource(item.filename, item.content_type, upload_chunks(item)) for item in files
        ]
        return await service.create_from_uploads(
            auth.workspace_id, kb_id, sources, options, uploader=uploader
        )
    finally:
        await form.close()


def _preview_openapi() -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["text"],
                        "properties": {
                            "text": {"type": "string"},
                            "process_rule": {"type": "object"},
                        },
                        "additionalProperties": False,
                    }
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "data": {"type": "string", "description": "JSON {process_rule?}"},
                        },
                    }
                },
            },
        }
    }


@router.post(
    "/knowledge/{kb_id}/documents/preview",
    response_model=DocumentPreview,
    openapi_extra=_preview_openapi(),
)
async def preview_document(
    kb_id: uuid.UUID, request: Request, auth: CurrentAuth, service: DocumentServiceDep
) -> DocumentPreview:
    """파싱·정리·분할을 요청 안에서 돌려 앞 청크와 개수·추정 토큰·예상 소요(분)를 돌려준다.

    저장하지 않는다. 예상 소요는 분당 인제스트 몫으로만 나눈 값이라 일일 한도는 넣지 않았다.
    """
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type == "application/json":
        body = _parse(
            DocumentPreviewText,
            await _json_body(request, service.max_text_bytes * 2 + _JSON_SLACK),
        )
        if len(body.text.encode("utf-8")) > service.max_text_bytes:
            raise ApiError(
                413,
                "text_too_large",
                "글이 너무 크다",
                details={"max_bytes": service.max_text_bytes},
            )
        return await service.preview(
            auth.workspace_id, kb_id, process_rule=body.process_rule, text=body.text
        )
    if media_type != "multipart/form-data":
        raise ApiError(
            415,
            "unsupported_media_type",
            "multipart/form-data(file) 또는 application/json({text})로 보낸다",
        )
    _declared_length(
        request, service.max_upload_bytes + MULTIPART_OVERHEAD, required=True, code="file_too_large"
    )
    form = await request.form(max_files=2, max_fields=4)
    try:
        files = [
            item
            for name in _PREVIEW_FIELDS
            for item in form.getlist(name)
            if isinstance(item, UploadFile)
        ]
        if len(files) != 1:
            raise ApiError(422, "validation_error", "미리보기는 파일 하나만 받는다")
        raw = form.get("data")
        options = (
            _parse(DocumentPreviewOptions, raw, "data")
            if isinstance(raw, str) and raw.strip()
            else DocumentPreviewOptions()
        )
        item = files[0]
        return await service.preview(
            auth.workspace_id,
            kb_id,
            process_rule=options.process_rule,
            upload=UploadSource(item.filename, item.content_type, upload_chunks(item)),
        )
    finally:
        await form.close()


@router.post("/knowledge/{kb_id}/documents/status/{action}", response_model=DocumentStatusResult)
async def update_document_status(
    kb_id: uuid.UUID,
    action: DocumentStatusAction,
    body: DocumentStatusUpdate,
    auth: CurrentAuth,
    service: DocumentServiceDep,
) -> DocumentStatusResult:
    """enable·disable·archive·unarchive·pause·resume·retry. 맞지 않는 상태의 문서는 skipped에 있다.

    색인 중인 문서는 enable·disable·archive가 400 document_indexing이고 pause만 된다. 일일 한도로
    끝난 문서의 retry는 태평양 자정 전이면 skipped(retry_after)다.
    """
    return await service.set_status(auth.workspace_id, kb_id, action, body.document_ids)


@router.post("/knowledge/{kb_id}/documents/{document_id}/reindex", response_model=KbDocumentOut)
async def reindex_document(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    body: DocumentReindex,
    auth: CurrentAuth,
    service: DocumentServiceDep,
) -> KbDocumentOut:
    """원본에서 처음부터 다시 나눈다. extract_mode=model은 스캔 PDF를 채팅 모델로 읽는다(PDF만,
    쪽 이미지가 채팅 모델로 간다)."""
    return await service.reindex(auth.workspace_id, kb_id, document_id, body.extract_mode)


@router.post("/knowledge/{kb_id}/documents/metadata", response_model=DocumentMetadataList)
async def update_document_metadata(
    kb_id: uuid.UUID,
    body: DocumentMetadataUpdate,
    auth: CurrentAuth,
    service: MetadataServiceDep,
) -> DocumentMetadataList:
    """고른 문서들의 메타데이터 값을 한꺼번에 넣는다. value가 null이면 지운다. apply_to_all을 끄면
    그 필드에 이미 값이 있는 문서만 바꾼다. 기본 필드는 고칠 수 없다(400 metadata_built_in).
    """
    return await service.update_values(auth.workspace_id, kb_id, body)


@router.get("/knowledge/{kb_id}/documents/{document_id}", response_model=KbDocumentOut)
async def get_document(
    kb_id: uuid.UUID, document_id: uuid.UUID, auth: CurrentAuth, service: DocumentServiceDep
) -> KbDocumentOut:
    return await service.get_document(auth.workspace_id, kb_id, document_id)


@router.get("/knowledge/{kb_id}/documents/{document_id}/file", response_class=Response)
async def download_document_file(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    service: DocumentServiceDep,
) -> Response:
    """원본 내려받기. 원본을 바꾼 문서는 지금 원본, 글로 만든 문서는 저장한 .txt다.

    늘 attachment로 보내고 nosniff를 단다. html·svg 원본이 이 출처에서 열리지 않게 한다.
    """
    original = await service.original(auth.workspace_id, kb_id, document_id)
    return await stored_download(
        request.app.state.storage,
        original.storage_key,
        filename=original.filename,
        mime=original.mime,
    )


@router.patch("/knowledge/{kb_id}/documents/{document_id}", response_model=KbDocumentOut)
async def patch_document(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    body: KbDocumentPatch,
    auth: CurrentAuth,
    service: DocumentServiceDep,
) -> KbDocumentOut:
    """이름 바꾸기. 보관한 문서는 400 document_archived다."""
    return await service.update_document(
        auth.workspace_id, kb_id, document_id, body, uploader=auth.account.name
    )


@router.delete("/knowledge/{kb_id}/documents/{document_id}", status_code=204)
async def delete_document(
    kb_id: uuid.UUID, document_id: uuid.UUID, auth: CurrentAuth, service: DocumentServiceDep
) -> None:
    """바로 돌아오고 청크·색인·원본은 뒤에서 지운다. 되돌릴 수 없다."""
    await service.delete_document(auth.workspace_id, kb_id, document_id)


@router.get("/knowledge/{kb_id}/documents/{document_id}/segments", response_model=SegmentList)
async def list_segments(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    auth: CurrentAuth,
    service: SegmentServiceDep,
    keyword: Annotated[str | None, Query(max_length=200)] = None,
    enabled: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> SegmentList:
    """위치 순. Parent-child 문서는 청크마다 child_chunks가 붙는다."""
    return await service.list_segments(
        auth.workspace_id,
        kb_id,
        document_id,
        keyword=keyword,
        enabled=enabled,
        page=page,
        limit=limit,
    )


@router.post(
    "/knowledge/{kb_id}/documents/{document_id}/segments",
    status_code=201,
    response_model=SegmentOut,
)
async def create_segment(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    body: SegmentCreate,
    auth: CurrentAuth,
    service: SegmentServiceDep,
) -> SegmentOut:
    """문서 끝에 청크를 더한다(Q&A는 answer 필수). 색인 중인 문서는 400 document_indexing이다."""
    return await service.create_segment(auth.workspace_id, kb_id, document_id, body)


@router.get(
    "/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}", response_model=SegmentOut
)
async def get_segment(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    auth: CurrentAuth,
    service: SegmentServiceDep,
) -> SegmentOut:
    return await service.get_segment(auth.workspace_id, kb_id, document_id, segment_id)


@router.patch(
    "/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}", response_model=SegmentOut
)
async def patch_segment(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    body: SegmentPatch,
    auth: CurrentAuth,
    service: SegmentServiceDep,
) -> SegmentOut:
    """글을 고치면 그 유닛의 색인이 pending으로 돌아간다. enabled는 색인이 끝난 청크만 바꾼다."""
    return await service.update_segment(auth.workspace_id, kb_id, document_id, segment_id, body)


@router.delete("/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}", status_code=204)
async def delete_segment(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    auth: CurrentAuth,
    service: SegmentServiceDep,
) -> None:
    await service.delete_segment(auth.workspace_id, kb_id, document_id, segment_id)


@router.get(
    "/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}/children",
    response_model=ChildChunkList,
)
async def list_child_chunks(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    auth: CurrentAuth,
    service: SegmentServiceDep,
) -> ChildChunkList:
    return await service.list_children(auth.workspace_id, kb_id, document_id, segment_id)


@router.post(
    "/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}/children",
    status_code=201,
    response_model=ChildChunkOut,
)
async def create_child_chunk(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    body: ChildChunkCreate,
    auth: CurrentAuth,
    service: SegmentServiceDep,
) -> ChildChunkOut:
    """Parent-child 문서의 부모 청크에 자식 청크를 더한다. 다른 모드는 400 not_hierarchical이다."""
    return await service.create_child(auth.workspace_id, kb_id, document_id, segment_id, body)


@router.patch(
    "/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}/children/{child_id}",
    response_model=ChildChunkOut,
)
async def patch_child_chunk(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    child_id: uuid.UUID,
    body: ChildChunkPatch,
    auth: CurrentAuth,
    service: SegmentServiceDep,
) -> ChildChunkOut:
    return await service.update_child(
        auth.workspace_id, kb_id, document_id, segment_id, child_id, body
    )


@router.delete(
    "/knowledge/{kb_id}/documents/{document_id}/segments/{segment_id}/children/{child_id}",
    status_code=204,
)
async def delete_child_chunk(
    kb_id: uuid.UUID,
    document_id: uuid.UUID,
    segment_id: uuid.UUID,
    child_id: uuid.UUID,
    auth: CurrentAuth,
    service: SegmentServiceDep,
) -> None:
    await service.delete_child(auth.workspace_id, kb_id, document_id, segment_id, child_id)
