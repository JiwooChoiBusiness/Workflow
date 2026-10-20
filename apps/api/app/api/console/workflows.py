"""워크플로우 콘솔 API(2단계 설계 6.1).

목록·생성·초안·검증·미리보기 런·단일 노드 실행·발행·버전·파일·노드 타입·API 키·챗플로우 미리보기
대화.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from app.api.console.agents import ApiKeyServiceDep
from app.api.console.runs import SSE_CONTENT
from app.api.console.wf_conversations import ConversationServiceDep
from app.api.console.wf_runs import WfRunServiceDep, wf_run_headers
from app.api.uploads import multipart_openapi, single_upload, upload_chunks
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyList
from app.schemas.run import RunStatus
from app.schemas.wf_conversation import WfConversationCreate, WfConversationList, WfConversationOut
from app.schemas.wf_run import WfNodeRunCreate, WfRunCreate, WfRunList, WfRunOut, WfRunTrigger
from app.schemas.workflow import (
    NodeTypeList,
    ValidationOut,
    WfFileOut,
    WorkflowCreate,
    WorkflowDraftCheck,
    WorkflowDraftUpdate,
    WorkflowList,
    WorkflowOut,
    WorkflowPatch,
    WorkflowPublishOut,
    WorkflowPublishRequest,
    WorkflowVersionList,
    WorkflowVersionOut,
)
from app.services.api_key_target import KeyTarget
from app.services.logs import assume_local
from app.services.workflow_files import MAX_WF_FILE_BYTES, WorkflowFileService
from app.services.workflow_single_run import WorkflowSingleRunService
from app.services.workflows import WorkflowService
from app.streaming.sse import sse_response

router = APIRouter(tags=["workflows"])


def get_workflow_service(db: DbDep, settings: SettingsDep) -> WorkflowService:
    return WorkflowService(db, settings)


def get_workflow_file_service(request: Request, db: DbDep) -> WorkflowFileService:
    return WorkflowFileService(db, request.app.state.storage)


def get_single_run_service(
    request: Request, db: DbDep, settings: SettingsDep
) -> WorkflowSingleRunService:
    state = request.app.state
    return WorkflowSingleRunService(
        db,
        settings,
        bus=state.bus,
        supervisor=state.workflow_supervisor,
        storage=state.storage,
    )


WorkflowServiceDep = Annotated[WorkflowService, Depends(get_workflow_service)]
WorkflowFileServiceDep = Annotated[WorkflowFileService, Depends(get_workflow_file_service)]
SingleRunServiceDep = Annotated[WorkflowSingleRunService, Depends(get_single_run_service)]


@router.get("/node-types", response_model=NodeTypeList)
async def list_node_types(auth: CurrentAuth, service: WorkflowServiceDep) -> NodeTypeList:
    """노드 레지스트리. 캔버스 팔레트·속성 패널·변수 선택기가 쓴다."""
    return service.node_types()


@router.get("/workflows", response_model=WorkflowList)
async def list_workflows(
    auth: CurrentAuth,
    service: WorkflowServiceDep,
    kind: Literal["workflow", "chatflow"] | None = None,
) -> WorkflowList:
    return await service.list_workflows(auth.workspace_id, kind=kind)


@router.post("/workflows", status_code=201, response_model=WorkflowOut)
async def create_workflow(
    body: WorkflowCreate, auth: CurrentAuth, service: WorkflowServiceDep
) -> WorkflowOut:
    """kind는 만든 뒤 바꿀 수 없다. 초안 그래프는 template에 맞는 최소 모양으로 시작한다."""
    return await service.create_workflow(auth.workspace_id, body)


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
async def get_workflow(
    workflow_id: uuid.UUID, auth: CurrentAuth, service: WorkflowServiceDep
) -> WorkflowOut:
    return await service.get_workflow(auth.workspace_id, workflow_id)


@router.patch("/workflows/{workflow_id}", response_model=WorkflowOut)
async def patch_workflow(
    workflow_id: uuid.UUID, body: WorkflowPatch, auth: CurrentAuth, service: WorkflowServiceDep
) -> WorkflowOut:
    return await service.update_workflow(auth.workspace_id, workflow_id, body)


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: uuid.UUID, auth: CurrentAuth, service: WorkflowServiceDep
) -> None:
    """소프트 삭제. 런 기록은 남고 slug는 다시 쓸 수 있다."""
    await service.delete_workflow(auth.workspace_id, workflow_id)


@router.get("/workflows/{workflow_id}/draft", response_model=WorkflowVersionOut)
async def get_draft(
    workflow_id: uuid.UUID, auth: CurrentAuth, service: WorkflowServiceDep
) -> WorkflowVersionOut:
    """초안과 지금 레지스트리로 다시 검증한 issues. 비밀 env는 값 대신 has_value만 있다."""
    return await service.get_draft(auth.workspace_id, workflow_id)


@router.put("/workflows/{workflow_id}/draft", response_model=WorkflowVersionOut)
async def save_draft(
    workflow_id: uuid.UUID,
    body: WorkflowDraftUpdate,
    auth: CurrentAuth,
    service: WorkflowServiceDep,
) -> WorkflowVersionOut:
    """초안 전체 저장(낙관적 잠금 updated_at). 검증 오류가 있어도 저장하고 issues로 알린다."""
    return await service.save_draft(auth.workspace_id, workflow_id, body)


@router.post("/workflows/{workflow_id}/draft/validate", response_model=ValidationOut)
async def validate_draft(
    workflow_id: uuid.UUID,
    auth: CurrentAuth,
    service: WorkflowServiceDep,
    body: WorkflowDraftCheck | None = None,
) -> ValidationOut:
    """저장하지 않고 검증한다. 본문이 없으면 저장된 초안을 검증한다."""
    return await service.validate_draft(auth.workspace_id, workflow_id, body)


@router.post(
    "/workflows/{workflow_id}/draft/runs",
    response_model=None,
    responses={
        200: {
            "model": WfRunOut,
            "description": "response_mode=streaming(기본)이면 SSE, blocking이면 WfRunOut",
            "content": SSE_CONTENT,
        }
    },
)
async def run_draft(
    workflow_id: uuid.UUID, body: WfRunCreate, auth: CurrentAuth, service: WfRunServiceDep
) -> Response:
    """초안으로 미리보기 런을 만든다(workflow만. 챗플로우는 대화로 실행한다).

    초안에 검증 오류가 있으면 422 draft_invalid, 입력이 start 필드에 맞지 않으면 422 invalid_inputs.
    streaming이면 첫 이벤트가 workflow.queued이고 workflow.completed 뒤에 닫힌다.
    """
    started = await service.start_draft_run(auth.workspace_id, workflow_id, body, subscribe=True)
    headers = wf_run_headers(started.run_id)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    result = await service.wait_result(auth.workspace_id, started)
    return JSONResponse(result.model_dump(mode="json"), headers=headers)


@router.post(
    "/workflows/{workflow_id}/draft/nodes/{node_id}/runs",
    response_model=None,
    responses={
        200: {
            "model": WfRunOut,
            "description": "response_mode=streaming(기본)이면 SSE, blocking이면 WfRunOut",
            "content": SSE_CONTENT,
        }
    },
)
async def run_draft_node(
    workflow_id: uuid.UUID,
    node_id: str,
    body: WfNodeRunCreate,
    auth: CurrentAuth,
    single: SingleRunServiceDep,
    service: WfRunServiceDep,
) -> Response:
    """초안의 노드 하나만 돌린다(trigger=single_node). iteration이면 하위 그래프가 한 번 돈다.

    inputs에는 노드가 참조하는 변수 값을 "<scope>.<name>" 키로 준다. 그 노드와 하위 그래프에 검증
    오류가 있으면 422 draft_invalid, 키가 그래프에 없거나 노드 자신·env를 가리키면 422
    invalid_inputs. 런 출력(workflow.completed.outputs)은 그 노드의 출력이다. agent 노드가 승인을
    기다리면 /wf-runs/{id}/input으로 잇는다.
    """
    started = await single.start(auth.workspace_id, workflow_id, node_id, body, subscribe=True)
    headers = wf_run_headers(started.run_id)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    result = await service.wait_result(auth.workspace_id, started)
    return JSONResponse(result.model_dump(mode="json"), headers=headers)


@router.get("/workflows/{workflow_id}/runs", response_model=WfRunList)
async def list_workflow_runs(
    workflow_id: uuid.UUID,
    auth: CurrentAuth,
    service: WfRunServiceDep,
    status: RunStatus | None = None,
    trigger: WfRunTrigger | None = None,
    conversation_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WfRunList:
    """런 목록(최근 것부터). 단일 노드 실행 런은 trigger=single_node로 고를 때만 나온다.

    기간은 런을 만든 시각(queued_at) 기준 [since, until)이다. 시간대 없이 온 시각은 서버 PC 시간대로
    본다.
    """
    return await service.list_runs(
        auth.workspace_id,
        workflow_id,
        status=status,
        trigger=trigger,
        conversation_id=conversation_id,
        since=assume_local(since),
        until=assume_local(until),
        limit=limit,
        offset=offset,
    )


@router.post("/workflows/{workflow_id}/publish", status_code=201, response_model=WorkflowPublishOut)
async def publish_workflow(
    workflow_id: uuid.UUID,
    auth: CurrentAuth,
    service: WorkflowServiceDep,
    body: WorkflowPublishRequest | None = None,
) -> WorkflowPublishOut:
    """초안을 발행 버전으로 복제한다. 검증 오류가 있으면 422 draft_invalid(details에 오류 목록)."""
    note = body.change_note if body is not None else None
    return await service.publish(auth.workspace_id, workflow_id, note)


@router.get("/workflows/{workflow_id}/versions", response_model=WorkflowVersionList)
async def list_versions(
    workflow_id: uuid.UUID, auth: CurrentAuth, service: WorkflowServiceDep
) -> WorkflowVersionList:
    return await service.list_versions(auth.workspace_id, workflow_id)


@router.get("/workflows/{workflow_id}/versions/{version_id}", response_model=WorkflowVersionOut)
async def get_version(
    workflow_id: uuid.UUID, version_id: uuid.UUID, auth: CurrentAuth, service: WorkflowServiceDep
) -> WorkflowVersionOut:
    return await service.get_version(auth.workspace_id, workflow_id, version_id)


@router.post("/workflows/{workflow_id}/versions/{version_id}/restore", response_model=WorkflowOut)
async def restore_version(
    workflow_id: uuid.UUID, version_id: uuid.UUID, auth: CurrentAuth, service: WorkflowServiceDep
) -> WorkflowOut:
    """롤백. 이 발행 버전을 현재 발행 버전으로 지정한다."""
    return await service.restore_version(auth.workspace_id, workflow_id, version_id)


@router.post(
    "/workflows/{workflow_id}/files",
    status_code=201,
    response_model=WfFileOut,
    openapi_extra=multipart_openapi(),
)
async def upload_workflow_file(
    workflow_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    service: WorkflowFileServiceDep,
) -> WfFileOut:
    """미리보기 런의 파일 입력(멀티파트, X-Requested-With: fetch). 런 입력에는 받은 id를 준다."""
    async with single_upload(request, max_bytes=MAX_WF_FILE_BYTES, what="파일") as upload:
        return await service.upload(
            auth.workspace_id,
            workflow_id,
            filename=upload.filename,
            content_type=upload.content_type,
            chunks=upload_chunks(upload),
        )


@router.get("/workflows/{workflow_id}/api-keys", response_model=ApiKeyList)
async def list_workflow_api_keys(
    workflow_id: uuid.UUID, auth: CurrentAuth, service: ApiKeyServiceDep
) -> ApiKeyList:
    return await service.list_for(auth.workspace_id, KeyTarget.workflow(workflow_id))


@router.post("/workflows/{workflow_id}/api-keys", status_code=201, response_model=ApiKeyCreated)
async def create_workflow_api_key(
    workflow_id: uuid.UUID,
    body: ApiKeyCreate,
    auth: CurrentAuth,
    service: ApiKeyServiceDep,
    response: Response,
) -> ApiKeyCreated:
    """서비스 API 키. scopes를 비우면 워크플로우는 workflow, 챗플로우는 chat이다(2단계 4.1)."""
    # 원문 키가 든 응답이 브라우저·프록시 캐시에 남지 않게 한다.
    response.headers["Cache-Control"] = "no-store"
    return await service.create_for(auth.workspace_id, KeyTarget.workflow(workflow_id), body)


@router.delete("/workflows/{workflow_id}/api-keys/{key_id}", status_code=204)
async def revoke_workflow_api_key(
    workflow_id: uuid.UUID, key_id: uuid.UUID, auth: CurrentAuth, service: ApiKeyServiceDep
) -> None:
    await service.revoke_for(auth.workspace_id, KeyTarget.workflow(workflow_id), key_id)


@router.post(
    "/workflows/{workflow_id}/preview/conversations",
    status_code=201,
    response_model=WfConversationOut,
)
async def create_preview_conversation(
    workflow_id: uuid.UUID,
    auth: CurrentAuth,
    service: ConversationServiceDep,
    body: WfConversationCreate | None = None,
) -> WfConversationOut:
    """챗플로우 미리보기 대화. 초안으로 돈다. 워크플로우(kind=workflow)는 409 wrong_kind."""
    return await service.create_preview(
        auth.workspace_id, workflow_id, body or WfConversationCreate()
    )


@router.get("/workflows/{workflow_id}/preview/conversations", response_model=WfConversationList)
async def list_preview_conversations(
    workflow_id: uuid.UUID,
    auth: CurrentAuth,
    service: ConversationServiceDep,
    workflows: WorkflowServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WfConversationList:
    """이 챗플로우의 미리보기 대화(최근 대화 순)."""
    await workflows.get_workflow(auth.workspace_id, workflow_id)
    return await service.list_conversations(
        auth.workspace_id, workflow_id=workflow_id, source="preview", limit=limit, offset=offset
    )
