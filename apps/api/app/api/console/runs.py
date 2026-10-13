import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from app.agent.interactions import InteractionReply
from app.api.downloads import stored_file_response, text_download_response
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.logs import RunLogList
from app.schemas.run import RunEventList, RunInput, RunOut, RunResult, RunStatus
from app.schemas.session import SessionSource
from app.services.logs import RunLogService, assume_local
from app.services.runs import RunService
from app.streaming.sse import SSE_MEDIA_TYPE, sse_response

router = APIRouter(prefix="/runs", tags=["runs"])

# SSE 응답의 OpenAPI 표시. 본문은 런 이벤트(packages/event-schema/run-events.schema.json)의
# 연속이다.
SSE_CONTENT: dict[str, Any] = {SSE_MEDIA_TYPE: {"schema": {"type": "string"}}}


def get_run_service(request: Request, db: DbDep, settings: SettingsDep) -> RunService:
    state = request.app.state
    return RunService(
        db,
        settings,
        bus=state.bus,
        supervisor=state.supervisor,
        storage=state.storage,
        backend=state.sandbox,
    )


RunServiceDep = Annotated[RunService, Depends(get_run_service)]


def run_headers(run_id: uuid.UUID, session_id: uuid.UUID) -> dict[str, str]:
    # 스트림의 첫 이벤트를 읽기 전에도 중지·응답에 쓸 수 있게 헤더로도 준다.
    return {"X-Run-Id": str(run_id), "X-Session-Id": str(session_id)}


@router.get("", response_model=RunLogList)
async def list_runs(
    auth: CurrentAuth,
    db: DbDep,
    session_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    source: SessionSource | None = None,
    status: RunStatus | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunLogList:
    """런 목록(최근 것부터). 지운 세션의 런도 나온다(session_deleted).

    기간은 런을 만든 시각(queued_at) 기준 [since, until)이다. 시간대 없이 온 시각은 서버 PC
    시간대로 본다.
    """
    return await RunLogService(db).list_runs(
        auth.workspace_id,
        session_id=session_id,
        agent_id=agent_id,
        source=source,
        status=status,
        since=assume_local(since),
        until=assume_local(until),
        limit=limit,
        offset=offset,
    )


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, auth: CurrentAuth, service: RunServiceDep) -> RunOut:
    return await service.get_run(auth.workspace_id, run_id)


@router.get("/{run_id}/events", response_model=RunEventList)
async def list_run_events(
    run_id: uuid.UUID,
    auth: CurrentAuth,
    service: RunServiceDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> RunEventList:
    """SQLite에 남은 영속 이벤트(페이지). 델타는 없다."""
    return await service.list_events(auth.workspace_id, run_id, after=after, limit=limit)


@router.post("/{run_id}/stop", response_model=RunOut)
async def stop_run(run_id: uuid.UUID, auth: CurrentAuth, service: RunServiceDep) -> RunOut:
    """중지. 실행 중이면 취소 신호를 보내고 도구 프로세스를 끝낸다. 결과는 run.completed로 온다."""
    return await service.stop(auth.workspace_id, run_id)


@router.post(
    "/{run_id}/input",
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
    run_id: uuid.UUID, body: RunInput, auth: CurrentAuth, service: RunServiceDep
) -> Response:
    """질문(answer)·승인(decision) 응답. 같은 런이 다시 큐에 들어가 이어서 실행된다(4.9)."""
    reply = InteractionReply(
        answer=body.answer,
        decision=body.decision,
        note=body.note,
        session_auto_approve=body.session_auto_approve,
    )
    started = await service.provide_input(
        auth.workspace_id, run_id, reply, subscribe=body.response_mode != "none"
    )
    headers = run_headers(started.run_id, started.session_id)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    if body.response_mode == "blocking":
        result = await service.wait_result(auth.workspace_id, started)
        return JSONResponse(result.model_dump(mode="json"), headers=headers)
    run = await service.get_run(auth.workspace_id, run_id)
    return JSONResponse(run.model_dump(mode="json"), status_code=202, headers=headers)


@router.get("/{run_id}/tool-calls/{tool_call_id}/output", response_class=Response)
async def download_tool_output(
    run_id: uuid.UUID,
    tool_call_id: str,
    request: Request,
    auth: CurrentAuth,
    service: RunServiceDep,
) -> Response:
    """도구 출력 전문. 256KB를 넘은 출력은 tool-outputs 파일, 아니면 이벤트에 남은 전문."""
    output = await service.tool_output(auth.workspace_id, run_id, tool_call_id)
    storage = request.app.state.storage
    # 흘려 쓰기에 실패했거나 보존 정리로 지워졌으면 이벤트에 남은 발췌를 준다.
    if output.storage_key is not None and await storage.size(output.storage_key) is not None:
        return stored_file_response(
            storage,
            output.storage_key,
            filename=output.filename,
            mime="text/plain; charset=utf-8",
        )
    return text_download_response(output.text, filename=output.filename)
