"""워크플로우 런 콘솔 API(2단계 설계 6.1): 조회, 노드 런, 이벤트, 중지, 승인·질문 응답."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from app.agent.interactions import InteractionReply
from app.api.console.runs import SSE_CONTENT
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.run import RunEventList, RunInput
from app.schemas.wf_run import WfNodeRunDetail, WfNodeRunList, WfRunDetail, WfRunOut
from app.services.runs import RunService
from app.services.tasks import TaskService
from app.services.workflow_runs import WorkflowRunService
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


def wf_run_headers(run_id: uuid.UUID) -> dict[str, str]:
    # 워크플로우 런에는 세션이 없다. 첫 이벤트를 읽기 전에도 중지·응답에 쓸 수 있게 헤더로 준다.
    return {"X-Run-Id": str(run_id)}


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
    run_id: uuid.UUID, body: RunInput, auth: CurrentAuth, service: WfRunServiceDep
) -> Response:
    """질문(answer)·승인(decision) 응답. 1단계 /runs/{rid}/input과 같은 본문이다(3.7의 3번).

    기다리는 자식 런에 응답을 넘긴 뒤 같은 워크플로우 런을 다시 큐에 넣는다. streaming이면 첫
    이벤트가 workflow.queued다.
    """
    reply = InteractionReply(
        answer=body.answer,
        decision=body.decision,
        note=body.note,
        session_auto_approve=body.session_auto_approve,
    )
    started = await service.provide_input(
        auth.workspace_id, run_id, reply, subscribe=body.response_mode != "none"
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
