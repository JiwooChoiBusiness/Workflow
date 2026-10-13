"""웹앱 세션 API(설계 6.3). 콘솔 세션 API의 부분집합이다.

범위(webapp 세션, 발행되어 webapp_enabled인 에이전트)를 확인한 뒤 콘솔 핸들러에 그대로 넘긴다.
범위 밖의 세션·런은 404다.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import Response

from app.api.console import runs as console_runs
from app.api.console import sessions as console_sessions
from app.api.console.runs import SSE_CONTENT, RunServiceDep
from app.api.console.sessions import SessionServiceDep
from app.api.uploads import multipart_openapi
from app.auth.dependencies import CurrentAuth
from app.db.models.enums import SessionSource
from app.schemas.run import RunInput, RunOut, RunResult, SessionDetail
from app.schemas.session import (
    MessageCreate,
    MessageList,
    SessionFileOut,
    SessionOut,
    SessionPatch,
)
from app.services.access import SessionScope

router = APIRouter(tags=["webapp"])

WEBAPP_SCOPE = SessionScope(sources=frozenset({SessionSource.WEBAPP}), webapp=True)


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: uuid.UUID, auth: CurrentAuth, service: SessionServiceDep
) -> SessionDetail:
    """active_run이 있으면 GET .../events?after=0으로 다시 붙는다(7.4)."""
    await service.check_session(auth.workspace_id, session_id, WEBAPP_SCOPE)
    return await service.get_session(auth.workspace_id, session_id)


@router.patch("/sessions/{session_id}", response_model=SessionOut)
async def patch_session(
    session_id: uuid.UUID, body: SessionPatch, auth: CurrentAuth, service: SessionServiceDep
) -> SessionOut:
    """제목, 세션 자동 승인 끄기·켜기(approval_override)."""
    await service.check_session(auth.workspace_id, session_id, WEBAPP_SCOPE)
    return await service.update_session(auth.workspace_id, session_id, body)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: uuid.UUID, auth: CurrentAuth, service: SessionServiceDep
) -> None:
    await service.check_session(auth.workspace_id, session_id, WEBAPP_SCOPE)
    await service.delete_session(auth.workspace_id, session_id)


@router.get("/sessions/{session_id}/messages", response_model=MessageList)
async def list_messages(
    session_id: uuid.UUID,
    auth: CurrentAuth,
    service: SessionServiceDep,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> MessageList:
    await service.check_session(auth.workspace_id, session_id, WEBAPP_SCOPE)
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
    session_id: uuid.UUID,
    body: MessageCreate,
    auth: CurrentAuth,
    runs: RunServiceDep,
    service: SessionServiceDep,
) -> Response:
    await service.check_session(auth.workspace_id, session_id, WEBAPP_SCOPE)
    return await console_sessions.send_message(session_id, body, auth, runs)


@router.get(
    "/sessions/{session_id}/events",
    response_class=Response,
    responses={200: {"description": "런 이벤트 SSE", "content": SSE_CONTENT}},
)
async def session_events(
    session_id: uuid.UUID,
    auth: CurrentAuth,
    runs: RunServiceDep,
    service: SessionServiceDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    run_id: uuid.UUID | None = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> Response:
    await service.check_session(auth.workspace_id, session_id, WEBAPP_SCOPE)
    return await console_sessions.session_events(
        session_id, auth, runs, after=after, run_id=run_id, last_event_id=last_event_id
    )


@router.get("/sessions/{session_id}/files/{file_id}/download", response_class=Response)
async def download_session_file(
    session_id: uuid.UUID,
    file_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    service: SessionServiceDep,
) -> Response:
    await service.check_session(auth.workspace_id, session_id, WEBAPP_SCOPE)
    return await console_sessions.download_session_file(session_id, file_id, request, auth, service)


@router.post(
    "/sessions/{session_id}/uploads",
    status_code=201,
    response_model=SessionFileOut,
    openapi_extra=multipart_openapi(),
)
async def upload_session_file(
    session_id: uuid.UUID, request: Request, auth: CurrentAuth, service: SessionServiceDep
) -> SessionFileOut:
    """메시지 첨부(멀티파트, X-Requested-With: fetch)."""
    await service.check_session(auth.workspace_id, session_id, WEBAPP_SCOPE)
    return await console_sessions.upload_session_file(session_id, request, auth, service)


@router.post("/runs/{run_id}/stop", response_model=RunOut)
async def stop_run(run_id: uuid.UUID, auth: CurrentAuth, runs: RunServiceDep) -> RunOut:
    await runs.check_run(auth.workspace_id, run_id, WEBAPP_SCOPE)
    return await runs.stop(auth.workspace_id, run_id)


@router.post(
    "/runs/{run_id}/input",
    response_model=None,
    status_code=202,
    responses={
        202: {"model": RunOut, "description": "response_mode=none(기본). 런이 다시 큐에 들어갔다"},
        200: {
            "model": RunResult,
            "description": "response_mode=blocking이면 RunResult, streaming이면 SSE",
            "content": SSE_CONTENT,
        },
    },
)
async def provide_input(
    run_id: uuid.UUID, body: RunInput, auth: CurrentAuth, runs: RunServiceDep
) -> Response:
    await runs.check_run(auth.workspace_id, run_id, WEBAPP_SCOPE)
    return await console_runs.provide_input(run_id, body, auth, runs)
