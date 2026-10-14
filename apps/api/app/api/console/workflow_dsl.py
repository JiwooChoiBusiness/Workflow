"""워크플로우 DSL 내보내기·가져오기 콘솔 API(2단계 설계 5장, 6.1).

가져오기는 JSON {yaml} 본문과 multipart file 둘 다 받는다. 본문을 FastAPI 매개변수로 두지 않고 직접
읽는 것은 크기를 본문을 다 받기 전에 끊기 위해서다(멀티파트는 의존성 검사보다 먼저 전부 받는다).
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.api.downloads import download_headers
from app.api.errors import ApiError
from app.api.uploads import MULTIPART_OVERHEAD
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.workflow import WorkflowImportOut, WorkflowImportRequest
from app.services.workflow_dsl import WorkflowDslService
from app.workflow.dsl.schema import MAX_DSL_BYTES

router = APIRouter(tags=["workflows"])

YAML_MEDIA_TYPE = "application/yaml"
# include_secrets로 비밀값을 평문으로 넣었을 때 붙는다. 화면은 이 헤더를 보고 경고한다.
WARNING_HEADER = "X-Dsl-Warning"
SECRETS_INCLUDED = "secrets_included"
# JSON 문자열은 한글·이모지를 \uXXXX로 쓰면 YAML보다 몇 배 길어질 수 있다. YAML 자체의 2MB는
# 서비스가 따로 본다.
MAX_JSON_BODY_BYTES = MAX_DSL_BYTES * 4 + 64 * 1024


def get_dsl_service(db: DbDep, settings: SettingsDep) -> WorkflowDslService:
    return WorkflowDslService(db, settings)


DslServiceDep = Annotated[WorkflowDslService, Depends(get_dsl_service)]


def _too_large() -> ApiError:
    return ApiError(
        422,
        "dsl_too_large",
        f"DSL 파일은 {MAX_DSL_BYTES // 2**20}MB까지다",
        details={"max_bytes": MAX_DSL_BYTES},
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


async def _json_body(request: Request) -> bytes:
    _declared_length(request, MAX_JSON_BODY_BYTES, required=False)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_JSON_BODY_BYTES:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


async def _file_body(request: Request) -> bytes:
    _declared_length(request, MAX_DSL_BYTES + MULTIPART_OVERHEAD, required=True)
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
        return await item.read(MAX_DSL_BYTES + 1)
    finally:
        await form.close()


async def read_dsl_text(request: Request) -> str:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type == "multipart/form-data":
        data = await _file_body(request)
    elif media_type == "application/json":
        try:
            body = WorkflowImportRequest.model_validate_json(await _json_body(request))
        except ValidationError as exc:
            details = [
                {"loc": ["body", *error["loc"]], "msg": error["msg"], "type": error["type"]}
                for error in exc.errors()
            ]
            raise ApiError(
                422, "validation_error", "요청 값이 올바르지 않다", details=details
            ) from None
        data = body.yaml.encode("utf-8")
    else:
        raise ApiError(
            415,
            "unsupported_media_type",
            "application/json({yaml}) 또는 multipart/form-data(file)로 보낸다",
        )
    if len(data) > MAX_DSL_BYTES:
        raise _too_large()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ApiError(422, "invalid_yaml", "DSL 파일은 UTF-8 글이어야 한다") from None


def _import_openapi() -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["yaml"],
                        "properties": {"yaml": {"type": "string"}},
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


@router.post(
    "/workflows/{workflow_id}/export",
    response_class=Response,
    responses={
        200: {
            "description": "우리 DSL YAML(첨부 파일). include_secrets로 비밀값을 넣었으면 "
            f"{WARNING_HEADER}: {SECRETS_INCLUDED} 헤더가 붙는다",
            "content": {YAML_MEDIA_TYPE: {"schema": {"type": "string"}}},
        }
    },
)
async def export_workflow(
    workflow_id: uuid.UUID,
    auth: CurrentAuth,
    service: DslServiceDep,
    version: Annotated[Literal["draft", "published"] | uuid.UUID, Query()] = "draft",
    include_secrets: bool = False,
) -> Response:
    """워크플로우를 우리 DSL YAML로 내려 준다. version은 draft(기본)·published·버전 id다.

    비밀 환경 변수는 기본으로 이름만 쓴다. include_secrets=true면 평문으로 넣고 경고 헤더를 붙인다.
    agent 노드에는 다른 PC에서 에이전트를 찾을 agent_slug를 더 쓴다.
    """
    exported = await service.export(
        auth.workspace_id, workflow_id, version=version, include_secrets=include_secrets
    )
    headers = download_headers(exported.filename, exported.ascii_filename)
    if exported.secrets_included:
        headers[WARNING_HEADER] = SECRETS_INCLUDED
    return Response(
        exported.text.encode("utf-8"),
        media_type=f"{YAML_MEDIA_TYPE}; charset=utf-8",
        headers=headers,
    )


@router.post(
    "/workflows/import",
    status_code=201,
    response_model=WorkflowImportOut,
    responses={
        200: {"model": WorkflowImportOut, "description": "into 워크플로우의 초안을 덮어썼다"}
    },
    openapi_extra=_import_openapi(),
)
async def import_workflow(
    request: Request,
    response: Response,
    auth: CurrentAuth,
    service: DslServiceDep,
    into: uuid.UUID | None = None,
) -> WorkflowImportOut:
    """우리 DSL이나 Dify DSL(workflow·advanced-chat)을 가져온다.

    본문은 {"yaml": "..."}(JSON) 또는 multipart의 file(X-Requested-With: fetch). 새 워크플로우를
    만들면 201, into로 준 워크플로우의 초안을 덮어쓰면 200이다(발행 버전·이름은 그대로). 저장한
    초안의 검증 결과가 issues에, 바꾸거나 버린 것이 converted.warnings에, 옮기지 못한 노드 id가
    converted.unsupported에 온다. 가져온 code 노드는 ui.imported_code가 붙어 검토 전까지
    imported_code_unreviewed 오류다(초안 저장에서 그 키를 지우면 풀린다). 2MB 초과는 422
    dsl_too_large, 읽을 수 없는 YAML은 422 invalid_yaml이다.
    """
    text = await read_dsl_text(request)
    result = await service.import_text(auth.workspace_id, text, into=into)
    if into is not None:
        response.status_code = 200
    return result
