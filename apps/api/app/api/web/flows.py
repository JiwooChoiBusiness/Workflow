"""웹앱의 워크플로우·챗플로우 API(2단계 설계 6.2).

발행되어 webapp_enabled인 워크플로우만 열고, 런은 지금의 발행 버전으로 trigger=webapp이다.
챗플로우는 대화(source=webapp)를 만들 때의 발행 버전으로 이어 가고, 메시지를 보내면 런이 생긴다.
런 조회·이벤트·중지·응답은 범위를 확인한 뒤 콘솔 핸들러에 그대로 넘긴다. 범위 밖은 404다.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from app.api.console import wf_conversations as console_conversations
from app.api.console import wf_runs as console_wf_runs
from app.api.console.runs import SSE_CONTENT
from app.api.console.wf_conversations import ConversationServiceDep
from app.api.console.wf_runs import WfRunServiceDep, wf_run_headers
from app.api.downloads import stored_download
from app.api.uploads import multipart_openapi, single_upload, upload_chunks
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.db.models.enums import WfConversationSource, WorkflowKind
from app.schemas.webapp import FlowAppOut, FlowRunResult
from app.schemas.wf_conversation import (
    WfConversationCreate,
    WfConversationDetail,
    WfConversationList,
    WfConversationOut,
    WfConversationPatch,
    WfMessageCreate,
    WfMessageList,
)
from app.schemas.wf_run import WfRunCreate, WfRunDetail, WfRunInput, WfRunList, WfRunOut
from app.schemas.workflow import WfFileOut
from app.services.access import FlowScope
from app.services.wf_conversations import wrong_kind
from app.services.workflow_files import MAX_WF_FILE_BYTES, WorkflowFileService
from app.services.workflow_webapp import WorkflowWebappService, flow_result
from app.streaming.sse import sse_response

router = APIRouter(tags=["webapp"])


def get_webapp_flows(request: Request, db: DbDep, settings: SettingsDep) -> WorkflowWebappService:
    state = request.app.state
    return WorkflowWebappService(
        db,
        settings,
        bus=state.bus,
        supervisor=state.workflow_supervisor,
        storage=state.storage,
    )


FlowsDep = Annotated[WorkflowWebappService, Depends(get_webapp_flows)]

WEBAPP_FLOW_SCOPE = FlowScope(sources=frozenset({WfConversationSource.WEBAPP}), webapp=True)


@router.get("/flows/{slug}", response_model=FlowAppOut)
async def get_flow(slug: str, auth: CurrentAuth, flows: FlowsDep) -> FlowAppOut:
    """발행되어 webapp_enabled인 워크플로우의 공개 정보와 입력 필드. 아니면 404 app_not_found."""
    return await flows.get_app(auth.workspace_id, slug)


@router.post(
    "/flows/{slug}/runs",
    response_model=None,
    responses={
        200: {
            "model": FlowRunResult,
            "description": "response_mode=streaming(기본)이면 SSE, blocking이면 FlowRunResult",
            "content": SSE_CONTENT,
        }
    },
)
async def run_flow(
    slug: str, body: WfRunCreate, auth: CurrentAuth, flows: FlowsDep, runs: WfRunServiceDep
) -> Response:
    """발행 버전으로 실행한다(trigger=webapp). 챗플로우는 409 wrong_kind.

    입력이 start 필드에 맞지 않으면 422 invalid_inputs. streaming이면 첫 이벤트가
    workflow.queued이고 workflow.completed(입력 대기 포함) 뒤에 닫힌다.
    """
    started = await flows.start_run(auth.workspace_id, slug, body.inputs, subscribe=True)
    headers = wf_run_headers(started.run_id)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    result = await runs.wait_result(auth.workspace_id, started)
    return JSONResponse(flow_result(result).model_dump(mode="json"), headers=headers)


@router.get("/flows/{slug}/runs", response_model=WfRunList)
async def list_flow_runs(
    slug: str,
    auth: CurrentAuth,
    flows: FlowsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WfRunList:
    """이 웹앱에서 실행한 런(최근 것부터). 미리보기·서비스 API 런은 나오지 않는다."""
    return await flows.list_runs(auth.workspace_id, slug, limit=limit, offset=offset)


@router.post(
    "/flows/{slug}/files",
    status_code=201,
    response_model=WfFileOut,
    openapi_extra=multipart_openapi(),
)
async def upload_flow_file(
    slug: str, request: Request, auth: CurrentAuth, flows: FlowsDep
) -> WfFileOut:
    """파일 입력(멀티파트, X-Requested-With: fetch). 런 입력의 파일 필드에는 받은 id를 준다."""
    async with single_upload(request, max_bytes=MAX_WF_FILE_BYTES, what="파일") as upload:
        return await flows.upload(
            auth.workspace_id,
            slug,
            filename=upload.filename,
            content_type=upload.content_type,
            chunks=upload_chunks(upload),
        )


@router.get("/wf-runs/{run_id}", response_model=WfRunDetail)
async def get_flow_run(
    run_id: uuid.UUID, auth: CurrentAuth, flows: FlowsDep, runs: WfRunServiceDep
) -> WfRunDetail:
    await flows.check_run(auth.workspace_id, run_id)
    return await runs.get_run(auth.workspace_id, run_id)


@router.get(
    "/wf-runs/{run_id}/events",
    response_class=Response,
    responses={200: {"description": "워크플로우 런 이벤트 SSE", "content": SSE_CONTENT}},
)
async def flow_run_events(
    run_id: uuid.UUID,
    auth: CurrentAuth,
    flows: FlowsDep,
    runs: WfRunServiceDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> Response:
    await flows.check_run(auth.workspace_id, run_id)
    return await console_wf_runs.wf_run_events(
        run_id, auth, runs, after=after, last_event_id=last_event_id
    )


@router.post("/wf-runs/{run_id}/stop", response_model=WfRunOut)
async def stop_flow_run(
    run_id: uuid.UUID, auth: CurrentAuth, flows: FlowsDep, runs: WfRunServiceDep
) -> WfRunOut:
    await flows.check_run(auth.workspace_id, run_id)
    return await runs.stop(auth.workspace_id, run_id)


@router.post(
    "/wf-runs/{run_id}/input",
    response_model=None,
    status_code=202,
    responses={
        202: {
            "model": WfRunOut,
            "description": "response_mode=none(기본). 런이 다시 큐에 들어갔다",
        },
        200: {
            "model": WfRunOut,
            "description": "response_mode=blocking이면 WfRunOut, streaming이면 SSE",
            "content": SSE_CONTENT,
        },
    },
)
async def provide_flow_input(
    run_id: uuid.UUID,
    body: WfRunInput,
    auth: CurrentAuth,
    flows: FlowsDep,
    runs: WfRunServiceDep,
) -> Response:
    """agent 노드의 승인·질문과 human_input 폼 응답. 콘솔 /wf-runs/{rid}/input과 같다.

    폼의 파일 칸에는 POST /flows/{slug}/files로 올린 id를 준다.
    """
    await flows.check_run(auth.workspace_id, run_id)
    return await console_wf_runs.provide_wf_input(run_id, body, auth, runs)


@router.get("/wf-runs/{run_id}/files/{file_id}/download", response_class=Response)
async def download_flow_run_file(
    run_id: uuid.UUID, file_id: uuid.UUID, request: Request, auth: CurrentAuth, flows: FlowsDep
) -> Response:
    """런의 입력·출력에 든 파일. 출력 화면의 내려받기 링크가 쓴다."""
    target = await flows.run_file(auth.workspace_id, run_id, file_id)
    storage = request.app.state.storage
    return await stored_download(
        storage, target.storage_key, filename=target.filename, mime=target.mime
    )


@router.get("/flows/{slug}/conversations", response_model=WfConversationList)
async def list_flow_conversations(
    slug: str,
    auth: CurrentAuth,
    flows: FlowsDep,
    conversations: ConversationServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WfConversationList:
    """이 챗플로우 웹앱의 대화(최근 대화 순). 미리보기·서비스 API 대화는 섞이지 않는다."""
    flow = await flows.published(auth.workspace_id, slug)
    if flow.workflow.kind != WorkflowKind.CHATFLOW:
        raise wrong_kind()
    return await conversations.list_conversations(
        auth.workspace_id,
        workflow_id=flow.workflow.id,
        source=WfConversationSource.WEBAPP,
        limit=limit,
        offset=offset,
    )


@router.post("/flows/{slug}/conversations", status_code=201, response_model=WfConversationOut)
async def create_flow_conversation(
    slug: str,
    auth: CurrentAuth,
    flows: FlowsDep,
    conversations: ConversationServiceDep,
    body: WfConversationCreate | None = None,
) -> WfConversationOut:
    """새 대화. 지금의 발행 버전을 쓴다. 워크플로우(kind=workflow)는 409 wrong_kind."""
    flow = await flows.published(auth.workspace_id, slug)
    return await conversations.create_published(
        auth.workspace_id,
        flow.workflow.id,
        source=WfConversationSource.WEBAPP,
        title=body.title if body is not None else None,
    )


@router.get("/wf-conversations/{conversation_id}", response_model=WfConversationDetail)
async def get_flow_conversation(
    conversation_id: uuid.UUID, auth: CurrentAuth, conversations: ConversationServiceDep
) -> WfConversationDetail:
    """active_run이 있으면 GET /wf-runs/{id}/events?after=0으로 다시 붙는다."""
    await conversations.check(auth.workspace_id, conversation_id, WEBAPP_FLOW_SCOPE)
    return await conversations.get(auth.workspace_id, conversation_id)


@router.patch("/wf-conversations/{conversation_id}", response_model=WfConversationOut)
async def patch_flow_conversation(
    conversation_id: uuid.UUID,
    body: WfConversationPatch,
    auth: CurrentAuth,
    conversations: ConversationServiceDep,
) -> WfConversationOut:
    await conversations.check(auth.workspace_id, conversation_id, WEBAPP_FLOW_SCOPE)
    return await conversations.update(auth.workspace_id, conversation_id, body)


@router.delete("/wf-conversations/{conversation_id}", status_code=204)
async def delete_flow_conversation(
    conversation_id: uuid.UUID, auth: CurrentAuth, conversations: ConversationServiceDep
) -> None:
    await conversations.check(auth.workspace_id, conversation_id, WEBAPP_FLOW_SCOPE)
    await conversations.delete(auth.workspace_id, conversation_id)


@router.get("/wf-conversations/{conversation_id}/messages", response_model=WfMessageList)
async def list_flow_messages(
    conversation_id: uuid.UUID,
    auth: CurrentAuth,
    conversations: ConversationServiceDep,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> WfMessageList:
    await conversations.check(auth.workspace_id, conversation_id, WEBAPP_FLOW_SCOPE)
    return await conversations.list_messages(
        auth.workspace_id, conversation_id, before_seq=before_seq, limit=limit
    )


@router.post(
    "/wf-conversations/{conversation_id}/messages",
    response_model=None,
    responses=console_conversations.MESSAGE_RESPONSES,
)
async def send_flow_message(
    conversation_id: uuid.UUID,
    body: WfMessageCreate,
    auth: CurrentAuth,
    conversations: ConversationServiceDep,
) -> Response:
    """콘솔 POST /wf-conversations/{cid}/messages와 같다.

    파일은 POST /flows/{slug}/files로 올린 id를 files에 준다.
    """
    await conversations.check(auth.workspace_id, conversation_id, WEBAPP_FLOW_SCOPE)
    return await console_conversations.respond(
        conversations, auth.workspace_id, conversation_id, body
    )


@router.get("/wf-conversations/{conversation_id}/files/{file_id}/download", response_class=Response)
async def download_flow_conversation_file(
    conversation_id: uuid.UUID,
    file_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    db: DbDep,
) -> Response:
    """웹앱 대화의 메시지에 붙은 업로드."""
    storage = request.app.state.storage
    target = await WorkflowFileService(db, storage).conversation_file(
        auth.workspace_id, conversation_id, file_id, WEBAPP_FLOW_SCOPE
    )
    return await stored_download(
        storage, target.storage_key, filename=target.filename, mime=target.mime
    )
