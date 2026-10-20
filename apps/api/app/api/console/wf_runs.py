"""워크플로우 런 콘솔 API(2단계 설계 6.1): 조회, 노드 런, 이벤트, 중지, 승인·질문·폼 응답."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from app.agent.interactions import InteractionReply
from app.api.console.runs import SSE_CONTENT
from app.api.downloads import stored_download
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.run import RunEventList, RunStatus
from app.schemas.wf_run import (
    WfNodeRunDetail,
    WfNodeRunList,
    WfRunDetail,
    WfRunInput,
    WfRunLogList,
    WfRunOut,
    WfRunTrigger,
)
from app.services.errors import ServiceError
from app.services.logs import assume_local
from app.services.runs import RunService
from app.services.tasks import TaskService
from app.services.workflow_files import WorkflowFileService
from app.services.workflow_runs import StartedWfRun, WorkflowRunService
from app.streaming.sse import sse_response

router = APIRouter(prefix="/wf-runs", tags=["workflow-runs"])


def get_wf_run_service(request: Request, db: DbDep, settings: SettingsDep) -> WorkflowRunService:
    state = request.app.state
    # 자식 런에 응답을 넘긴 뒤 부모를 다시 넣는 일은 WorkflowRunService가 직접 순서를 정해 한다.
    # 그래서 이 과업 서비스의 RunService에는 부모 재투입 훅(parents)을 걸지 않는다.
    runs = RunService(
        db,
        settings,
        bus=state.bus,
        supervisor=state.supervisor,
        storage=state.storage,
        backend=state.sandbox,
    )
    return WorkflowRunService(
        db,
        settings,
        bus=state.bus,
        supervisor=state.workflow_supervisor,
        storage=state.storage,
        tasks=TaskService(db, settings, runs=runs, bus=state.bus),
    )


WfRunServiceDep = Annotated[WorkflowRunService, Depends(get_wf_run_service)]


def form_values(
    kind: str | None, values: dict[str, Any] | None, *, answer: str | None, decision: str | None
) -> dict[str, Any] | None:
    """/input 본문이 폼 응답이면 그 값, 승인·질문 응답이면 None. 둘을 섞으면 422 invalid_input."""
    if kind != "form" and values is None:
        return None
    if kind not in (None, "form") or answer is not None or decision is not None:
        raise ServiceError(
            422,
            "invalid_input",
            "폼 응답(kind=form, values)과 승인·질문 응답(answer, decision)은 함께 보낼 수 없다",
        )
    return dict(values or {})


async def resume_with(
    service: WorkflowRunService,
    workspace_id: uuid.UUID,
    run_id: uuid.UUID,
    body: WfRunInput,
    *,
    subscribe: bool,
) -> StartedWfRun:
    values = form_values(body.kind, body.values, answer=body.answer, decision=body.decision)
    if values is not None:
        return await service.provide_form(workspace_id, run_id, values, subscribe=subscribe)
    reply = InteractionReply(
        answer=body.answer,
        decision=body.decision,
        note=body.note,
        session_auto_approve=body.session_auto_approve,
    )
    return await service.provide_input(workspace_id, run_id, reply, subscribe=subscribe)


def wf_run_headers(run_id: uuid.UUID) -> dict[str, str]:
    # 워크플로우 런에는 세션이 없다. 첫 이벤트를 읽기 전에도 중지·응답에 쓸 수 있게 헤더로 준다.
    return {"X-Run-Id": str(run_id)}


@router.get("", response_model=WfRunLogList)
async def list_wf_runs(
    auth: CurrentAuth,
    service: WfRunServiceDep,
    workflow_id: uuid.UUID | None = None,
    status: RunStatus | None = None,
    trigger: WfRunTrigger | None = None,
    conversation_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WfRunLogList:
    """워크스페이스 전체 워크플로우 런(최근 것부터, 로그 화면의 워크플로우 런 탭).

    지운 워크플로우의 런도 이름과 함께 나온다. 필터는 /workflows/{id}/runs와 같고 단일 노드 실행
    런은 trigger=single_node로 고를 때만 나온다. 기간은 queued_at 기준 [since, until)이다.
    """
    return await service.list_workspace_runs(
        auth.workspace_id,
        workflow_id=workflow_id,
        status=status,
        trigger=trigger,
        conversation_id=conversation_id,
        since=assume_local(since),
        until=assume_local(until),
        limit=limit,
        offset=offset,
    )


@router.get("/{run_id}", response_model=WfRunDetail)
async def get_wf_run(run_id: uuid.UUID, auth: CurrentAuth, service: WfRunServiceDep) -> WfRunDetail:
    """런 상세. 노드 런은 루트 그래프의 것만 요약으로 붙는다."""
    return await service.get_run(auth.workspace_id, run_id)


@router.get("/{run_id}/node-runs", response_model=WfNodeRunList)
async def list_node_runs(
    run_id: uuid.UUID,
    auth: CurrentAuth,
    service: WfRunServiceDep,
    iteration: str | None = None,
) -> WfNodeRunList:
    """실행 순서. iteration=<노드 id>를 주면 그 iteration 하위 노드 런만."""
    return await service.list_node_runs(auth.workspace_id, run_id, iteration=iteration)


@router.get("/{run_id}/node-runs/{node_run_id}", response_model=WfNodeRunDetail)
async def get_node_run(
    run_id: uuid.UUID, node_run_id: uuid.UUID, auth: CurrentAuth, service: WfRunServiceDep
) -> WfNodeRunDetail:
    """노드 런 상세(inputs·outputs·process 전체). 이벤트의 4KB 미리보기를 펼칠 때 쓴다."""
    return await service.get_node_run(auth.workspace_id, run_id, node_run_id)


@router.get(
    "/{run_id}/events",
    response_class=Response,
    responses={200: {"description": "워크플로우 런 이벤트 SSE", "content": SSE_CONTENT}},
)
async def wf_run_events(
    run_id: uuid.UUID,
    auth: CurrentAuth,
    service: WfRunServiceDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> Response:
    """after 다음부터 다시 받는다(재접속).

    진행 중이면 workflow.completed까지 따라가고, 끝났거나 입력을 기다리는 중이면 남은 기록만 보내고
    닫는다.
    """
    start = after
    if start is None and last_event_id is not None and last_event_id.strip().isdigit():
        start = int(last_event_id.strip())
    subscription = await service.stream(auth.workspace_id, run_id, after=start or 0)
    return sse_response(subscription, headers=wf_run_headers(run_id))


@router.get("/{run_id}/event-log", response_model=RunEventList)
async def wf_run_event_log(
    run_id: uuid.UUID,
    auth: CurrentAuth,
    service: WfRunServiceDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> RunEventList:
    """SQLite에 남은 영속 이벤트(페이지). 델타는 없다."""
    return await service.list_events(auth.workspace_id, run_id, after=after, limit=limit)


@router.post("/{run_id}/stop", response_model=WfRunOut)
async def stop_wf_run(run_id: uuid.UUID, auth: CurrentAuth, service: WfRunServiceDep) -> WfRunOut:
    """중지. 입력을 기다리는 런이면 기다리던 자식 런도 중지한다.

    결과는 workflow.completed로 온다.
    """
    return await service.stop(auth.workspace_id, run_id)


@router.post(
    "/{run_id}/input",
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
async def provide_wf_input(
    run_id: uuid.UUID, body: WfRunInput, auth: CurrentAuth, service: WfRunServiceDep
) -> Response:
    """질문(answer)·승인(decision)·폼(kind=form, values) 응답(3.7의 3번).

    승인·질문은 1단계 /runs/{rid}/input과 같은 본문이고 기다리는 자식 런에 응답을 넘긴 뒤 같은
    워크플로우 런을 다시 큐에 넣는다. 폼은 필드 정의로 검사(422 invalid_inputs)한 값을 담아 같은
    런을 다시 넣는다. 기다리는 것과 다른 종류의 응답은 409 wrong_input_kind다. streaming이면 첫
    이벤트가 workflow.queued다.
    """
    started = await resume_with(
        service, auth.workspace_id, run_id, body, subscribe=body.response_mode != "none"
    )
    headers = wf_run_headers(run_id)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    if body.response_mode == "blocking":
        result = await service.wait_result(auth.workspace_id, started)
        return JSONResponse(result.model_dump(mode="json"), headers=headers)
    run: dict[str, Any] = (await service.get_run(auth.workspace_id, run_id)).model_dump(
        mode="json", exclude={"node_runs"}
    )
    return JSONResponse(run, status_code=202, headers=headers)


@router.get("/{run_id}/files/{file_id}/download", response_class=Response)
async def download_wf_run_file(
    run_id: uuid.UUID, file_id: uuid.UUID, request: Request, auth: CurrentAuth, db: DbDep
) -> Response:
    """런의 입력·출력에 든 파일, 그 런이 만든 파일, 챗플로우 런이면 그 대화에 붙은 업로드.

    실행 패널과 런 상세의 결과 링크가 쓴다. 웹앱 런도 여기서 받을 수 있다.
    """
    storage = request.app.state.storage
    target = await WorkflowFileService(db, storage).run_file(auth.workspace_id, run_id, file_id)
    return await stored_download(
        storage, target.storage_key, filename=target.filename, mime=target.mime
    )
