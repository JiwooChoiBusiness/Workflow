import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from app.api.console.runs import SSE_CONTENT, RunServiceDep, get_run_service, run_headers
from app.api.downloads import stored_file_response
from app.api.uploads import multipart_openapi, single_upload, upload_chunks
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.run import RunResult, SessionDetail
from app.schemas.session import (
    MessageCreate,
    MessageList,
    SessionCreate,
    SessionFileList,
    SessionFileOut,
    SessionList,
    SessionOut,
    SessionPatch,
    SessionSource,
)
from app.services.logs import assume_local
from app.services.runs import RunService
from app.services.sessions import MAX_UPLOAD_BYTES, SessionService
from app.streaming.sse import sse_response

router = APIRouter(tags=["sessions"])


def get_session_service(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    runs: Annotated[RunService, Depends(get_run_service)],
) -> SessionService:
    state = request.app.state
    return SessionService(db, settings, storage=state.storage, backend=state.sandbox, runs=runs)


SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]


@router.post("/agents/{agent_id}/preview/sessions", status_code=201, response_model=SessionOut)
async def create_preview_session(
    agent_id: uuid.UUID,
    auth: CurrentAuth,
    service: SessionServiceDep,
    body: SessionCreate | None = None,
) -> SessionOut:
    """빌더 미리보기 세션. 초안을 쓴다."""
    return await service.create_preview(auth.workspace_id, agent_id, body or SessionCreate())


@router.get("/sessions", response_model=SessionList)
async def list_sessions(
    auth: CurrentAuth,
    service: SessionServiceDep,
    agent_id: uuid.UUID | None = None,
    source: SessionSource | None = None,
    api_key_id: uuid.UUID | None = None,
    external_user: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SessionList:
    """세션 목록(최근 활동 순). 지운 세션은 나오지 않는다.

    기간은 세션을 만든 시각 기준 [since, until)이다. 시간대 없이 온 시각은 서버 PC 시간대로 본다.
    """
    return await service.list_sessions(
        auth.workspace_id,
        agent_id=agent_id,
        source=source,
        api_key_id=api_key_id,
        external_user=external_user,
        since=assume_local(since),
        until=assume_local(until),
        limit=limit,
        offset=offset,
    )


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: uuid.UUID, auth: CurrentAuth, service: SessionServiceDep
) -> SessionDetail:
    return await service.get_session(auth.workspace_id, session_id)


@router.patch("/sessions/{session_id}", response_model=SessionOut)
async def patch_session(
    session_id: uuid.UUID, body: SessionPatch, auth: CurrentAuth, service: SessionServiceDep
) -> SessionOut:
    """제목, 세션 자동 승인(approval_override: "off"로 켜고 null로 끈다)."""
    return await service.update_session(auth.workspace_id, session_id, body)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: uuid.UUID, auth: CurrentAuth, service: SessionServiceDep
) -> None:
    """진행 중인 런을 멈추고 작업공간 폴더를 지운다. 결과 파일은 남는다."""
    await service.delete_session(auth.workspace_id, session_id)


@router.get("/sessions/{session_id}/messages", response_model=MessageList)
async def list_messages(
    session_id: uuid.UUID,
    auth: CurrentAuth,
    service: SessionServiceDep,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> MessageList:
    """seq 오름차순. 기본은 마지막 100개이고, 앞을 더 보려면 before_seq=첫 메시지의 seq."""
    return await service.list_messages(
        auth.workspace_id, session_id, before_seq=before_seq, limit=limit
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=None,
    responses={
        200: {
            "model": RunResult,
            "description": "response_mode=streaming(기본)이면 SSE, blocking이면 RunResult",
            "content": SSE_CONTENT,
        }
    },
)
async def send_message(
    session_id: uuid.UUID, body: MessageCreate, auth: CurrentAuth, runs: RunServiceDep
) -> Response:
    """메시지를 보내고 런을 만든다. 질문을 기다리는 런이 있으면 이 글이 그 답이 된다(4.9)."""
    started = await runs.send_message(auth.workspace_id, session_id, body, subscribe=True)
    headers = run_headers(started.run_id, started.session_id)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    result = await runs.wait_result(auth.workspace_id, started)
    return JSONResponse(result.model_dump(mode="json"), headers=headers)


@router.get(
    "/sessions/{session_id}/events",
    response_class=Response,
    responses={200: {"description": "런 이벤트 SSE", "content": SSE_CONTENT}},
)
async def session_events(
    session_id: uuid.UUID,
    auth: CurrentAuth,
    runs: RunServiceDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    run_id: uuid.UUID | None = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> Response:
    """진행 중인 런(없으면 마지막 런)의 이벤트를 after 다음부터 다시 받는다(재접속).

    런이 끝났거나 답을 기다리는 중이면 남은 기록만 보내고 닫는다.
    """
    start = after
    if start is None and last_event_id is not None and last_event_id.strip().isdigit():
        start = int(last_event_id.strip())
    subscription = await runs.session_stream(
        auth.workspace_id, session_id, after=start or 0, run_id=run_id
    )
    return sse_response(
        subscription, headers=run_headers(subscription.ref.run_id, subscription.ref.session_id)
    )


@router.get("/sessions/{session_id}/files", response_model=SessionFileList)
async def list_session_files(
    session_id: uuid.UUID,
    auth: CurrentAuth,
    service: SessionServiceDep,
    kind: str | None = None,
) -> SessionFileList:
    return await service.list_files(auth.workspace_id, session_id, kind=kind)


@router.get("/sessions/{session_id}/files/{file_id}/download", response_class=Response)
async def download_session_file(
    session_id: uuid.UUID,
    file_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    service: SessionServiceDep,
) -> Response:
    row = await service.file_for_download(auth.workspace_id, session_id, file_id)
    storage = request.app.state.storage
    key = row.storage_key or ""
    if await storage.size(key) is None:
        return JSONResponse(
            {"error": {"code": "file_missing", "message": "저장소에 파일이 없다"}},
            status_code=404,
        )
    return stored_file_response(storage, key, filename=row.filename, mime=row.mime)


@router.post(
    "/sessions/{session_id}/uploads",
    status_code=201,
    response_model=SessionFileOut,
    openapi_extra=multipart_openapi(),
)
async def upload_session_file(
    session_id: uuid.UUID, request: Request, auth: CurrentAuth, service: SessionServiceDep
) -> SessionFileOut:
    """메시지 첨부 파일. 보낼 때 file_ids에 넣는다(멀티파트, X-Requested-With: fetch)."""
    async with single_upload(request, max_bytes=MAX_UPLOAD_BYTES, what="첨부 파일") as upload:
        return await service.upload(
            auth.workspace_id,
            session_id,
            filename=upload.filename,
            content_type=upload.content_type,
            chunks=upload_chunks(upload),
        )
