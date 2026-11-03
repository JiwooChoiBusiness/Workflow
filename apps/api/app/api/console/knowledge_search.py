"""지식 베이스 검색 시험·질의 기록·평가·API 키 콘솔 API(3단계 설계 7.7, 8.5, 12.1).

검색 시험은 저장하지 않는 임시 설정으로 돌고 응답은 Dify retrieve 모양이다. 평가셋 CSV는 JSON
{csv, replace}나 multipart file(+ 쿼리 replace)로 받는다. 본문을 직접 읽는 것은 크기를 다 받기 전에
끊기 위해서다(workflow_dsl과 같다).
"""

import math
import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.api.errors import ApiError
from app.api.uploads import MULTIPART_OVERHEAD
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.knowledge.eval import MAX_CSV_BYTES
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyList
from app.schemas.retrieval import (
    EvalCaseCreate,
    EvalCaseList,
    EvalCaseOut,
    EvalImportRequest,
    EvalImportResult,
    EvalRunList,
    EvalRunOut,
    EvalRunRequest,
    QueryLogList,
    RetrieveRequest,
    RetrieveResponse,
)
from app.services.api_keys import ApiKeyService, KeyTarget
from app.services.errors import ServiceError
from app.services.knowledge_eval import KnowledgeEvalService
from app.services.knowledge_runtime import (
    KnowledgeRuntime,
    get_knowledge_runtime,
    optional_knowledge_runtime,
)
from app.services.knowledge_search import KnowledgeSearchService, RetrievalFailed


def retry_after_headers(exc: ServiceError) -> dict[str, str] | None:
    if isinstance(exc, RetrievalFailed) and exc.status == 429 and exc.retry_after is not None:
        return {"Retry-After": str(max(1, math.ceil(exc.retry_after)))}
    return None


class RetrievalRoute(APIRoute):
    """검색이 한도(429)로 실패하면 Retry-After 헤더를 단다. 오류 본문은 그대로다."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except RetrievalFailed as exc:
                headers = retry_after_headers(exc)
                if headers is None:
                    raise
                raise ApiError(
                    exc.status, exc.code, exc.message, details=exc.details, headers=headers
                ) from exc

        return handle


router = APIRouter(tags=["knowledge"], route_class=RetrievalRoute)

# JSON 글은 이스케이프로 원문보다 길어질 수 있다. CSV 자체의 상한은 서비스가 따로 본다.
_MAX_JSON_BYTES = MAX_CSV_BYTES * 4 + 64 * 1024


def get_search_service(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    runtime: Annotated[KnowledgeRuntime, Depends(get_knowledge_runtime)],
) -> KnowledgeSearchService:
    return KnowledgeSearchService(db, settings, runtime, llm=request.app.state.llm)


def get_eval_service(request: Request, db: DbDep, settings: SettingsDep) -> KnowledgeEvalService:
    # 평가셋 관리와 기록 조회는 index.db 없이도 된다. 실행만 503 index_unavailable이다.
    runtime = optional_knowledge_runtime(request)
    search = KnowledgeSearchService(db, settings, runtime, llm=request.app.state.llm)
    return KnowledgeEvalService(db, settings, search)


def get_log_service(request: Request, db: DbDep, settings: SettingsDep) -> KnowledgeSearchService:
    return KnowledgeSearchService(db, settings, optional_knowledge_runtime(request))


def get_api_key_service(db: DbDep) -> ApiKeyService:
    return ApiKeyService(db)


SearchServiceDep = Annotated[KnowledgeSearchService, Depends(get_search_service)]
EvalServiceDep = Annotated[KnowledgeEvalService, Depends(get_eval_service)]
LogServiceDep = Annotated[KnowledgeSearchService, Depends(get_log_service)]
ApiKeyServiceDep = Annotated[ApiKeyService, Depends(get_api_key_service)]


@router.post("/knowledge/{kb_id}/retrieve", response_model=RetrieveResponse)
async def retrieve(
    kb_id: uuid.UUID, body: RetrieveRequest, auth: CurrentAuth, service: SearchServiceDep
) -> RetrieveResponse:
    """검색 시험. retrieval_model을 비우면 KB의 검색 설정이고, 저장하지 않는다.

    records에 결합 내역(fusion: 코사인 원값, 벡터·전문 점수)과 문서 색인 상태가 붙는다. 질의
    임베딩이 실패한 하이브리드는 전문 검색만으로 답하고 warnings에 vector_unavailable이 남는다.
    """
    result = await service.retrieve(
        auth.workspace_id,
        [kb_id],
        body.query,
        body.retrieval_model,
        metadata_filter=body.metadata_filter,
        source="hit_test",
    )
    return result.response()


@router.get("/knowledge/{kb_id}/query-logs", response_model=QueryLogList)
async def list_query_logs(
    kb_id: uuid.UUID,
    auth: CurrentAuth,
    service: LogServiceDep,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> QueryLogList:
    """최근 질의부터. KB_QUERY_LOG_DAYS가 0이면 기록이 쌓이지 않는다."""
    return await service.list_query_logs(auth.workspace_id, kb_id, page=page, limit=limit)


@router.get("/knowledge/{kb_id}/eval/cases", response_model=EvalCaseList)
async def list_eval_cases(
    kb_id: uuid.UUID, auth: CurrentAuth, service: EvalServiceDep
) -> EvalCaseList:
    return await service.list_cases(auth.workspace_id, kb_id)


@router.post("/knowledge/{kb_id}/eval/cases", status_code=201, response_model=EvalCaseOut)
async def create_eval_case(
    kb_id: uuid.UUID, body: EvalCaseCreate, auth: CurrentAuth, service: EvalServiceDep
) -> EvalCaseOut:
    """정답은 이 KB의 문서나 청크다. 문서 정답이면 그 문서의 어느 청크든 맞은 것으로 본다."""
    return await service.create_case(auth.workspace_id, kb_id, body)


@router.delete("/knowledge/{kb_id}/eval/cases/{case_id}", status_code=204)
async def delete_eval_case(
    kb_id: uuid.UUID, case_id: uuid.UUID, auth: CurrentAuth, service: EvalServiceDep
) -> None:
    await service.delete_case(auth.workspace_id, kb_id, case_id)


def _import_openapi() -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["csv"],
                        "properties": {
                            "csv": {"type": "string"},
                            "replace": {"type": "boolean"},
                        },
                        "additionalProperties": False,
                    }
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                },
            },
        }
    }


def _too_large() -> ApiError:
    return ApiError(
        413,
        "csv_too_large",
        f"평가셋 CSV는 {MAX_CSV_BYTES // 2**20}MB 이하여야 한다",
        details={"max_bytes": MAX_CSV_BYTES},
    )


def _declared_length(request: Request, limit: int, *, required: bool) -> None:
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
        raise _too_large()


async def _read_csv(request: Request, replace: bool) -> tuple[str, bool]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type == "multipart/form-data":
        _declared_length(request, MAX_CSV_BYTES + MULTIPART_OVERHEAD, required=True)
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
            data = await item.read(MAX_CSV_BYTES + 1)
        finally:
            await form.close()
        if len(data) > MAX_CSV_BYTES:
            raise _too_large()
        try:
            return data.decode("utf-8-sig"), replace
        except UnicodeDecodeError:
            raise ApiError(422, "csv_not_utf8", "평가셋 CSV는 UTF-8 글이어야 한다") from None
    if media_type != "application/json":
        raise ApiError(
            415,
            "unsupported_media_type",
            "application/json({csv, replace}) 또는 multipart/form-data(file)로 보낸다",
        )
    _declared_length(request, _MAX_JSON_BYTES, required=False)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_JSON_BYTES:
            raise _too_large()
        chunks.append(chunk)
    try:
        body = EvalImportRequest.model_validate_json(b"".join(chunks))
    except ValidationError as exc:
        details = [
            {"loc": ["body", *error["loc"]], "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        raise ApiError(
            422, "validation_error", "요청 값이 올바르지 않다", details=details
        ) from None
    return body.csv, body.replace or replace


@router.post(
    "/knowledge/{kb_id}/eval/cases/import",
    response_model=EvalImportResult,
    openapi_extra=_import_openapi(),
)
async def import_eval_cases(
    kb_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    service: EvalServiceDep,
    replace: Annotated[bool, Query()] = False,
) -> EvalImportResult:
    """CSV 머리글은 question과 expected_document_name 또는 expected_segment_id, 선택 note다.

    문서 이름이 없거나 형식이 틀린 줄은 넣지 않고 problems에 줄 번호로 남긴다. replace면 지금
    평가셋을 지우고 넣는다.
    """
    text, replace_all = await _read_csv(request, replace)
    return await service.import_cases(auth.workspace_id, kb_id, text, replace=replace_all)


@router.post("/knowledge/{kb_id}/eval/run", response_model=EvalRunOut)
async def run_eval(
    kb_id: uuid.UUID, body: EvalRunRequest, auth: CurrentAuth, service: EvalServiceDep
) -> EvalRunOut:
    """평가셋 전체를 그 설정으로 검색해 R@1·R@3·R@5·MRR@10과 질문별 순위를 낸다.

    save면 결과를 남긴다(KB마다 최근 30개). 질의 임베딩은 LRU에 남아 설정만 바꿔 다시 돌리면 호출이
    없다.
    """
    return await service.run(auth.workspace_id, kb_id, body)


@router.get("/knowledge/{kb_id}/eval/runs", response_model=EvalRunList)
async def list_eval_runs(
    kb_id: uuid.UUID, auth: CurrentAuth, service: EvalServiceDep
) -> EvalRunList:
    return await service.list_runs(auth.workspace_id, kb_id)


@router.get("/knowledge/{kb_id}/eval/runs/{run_id}", response_model=EvalRunOut)
async def get_eval_run(
    kb_id: uuid.UUID, run_id: uuid.UUID, auth: CurrentAuth, service: EvalServiceDep
) -> EvalRunOut:
    return await service.get_run(auth.workspace_id, kb_id, run_id)


@router.get("/knowledge/{kb_id}/api-keys", response_model=ApiKeyList)
async def list_api_keys(
    kb_id: uuid.UUID, auth: CurrentAuth, service: ApiKeyServiceDep
) -> ApiKeyList:
    return await service.list_for(auth.workspace_id, KeyTarget.knowledge(kb_id))


@router.post("/knowledge/{kb_id}/api-keys", status_code=201, response_model=ApiKeyCreated)
async def create_api_key(
    kb_id: uuid.UUID, body: ApiKeyCreate, auth: CurrentAuth, service: ApiKeyServiceDep
) -> ApiKeyCreated:
    """이 KB 하나만 보는 키(8.5). 권한은 knowledge뿐이고 키 원문은 이 응답에만 있다."""
    return await service.create_for(auth.workspace_id, KeyTarget.knowledge(kb_id), body)


@router.delete("/knowledge/{kb_id}/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    kb_id: uuid.UUID, key_id: uuid.UUID, auth: CurrentAuth, service: ApiKeyServiceDep
) -> None:
    await service.revoke_for(auth.workspace_id, KeyTarget.knowledge(kb_id), key_id)
