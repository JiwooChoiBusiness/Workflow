"""워크플로우 서비스 API(2단계 설계 6.3, A7).

워크플로우 키는 그 워크플로우의 발행 버전만 실행한다(trigger=api, 런에 키와 user가 남는다). 런 조회·
중지·응답·목록은 워크플로우 키와 챗플로우 키 둘 다 쓰고, 그 키로 만든 런만 보인다. 에이전트 키는 400
wrong_target이다. 챗플로우 실행은 /chat-messages다.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from app.agent.interactions import InteractionReply
from app.api.console.runs import SSE_CONTENT
from app.api.console.wf_runs import WfRunServiceDep, form_values, wf_run_headers
from app.api.v1.common import flow_of, flow_scope, parse_cursor
from app.auth.dependencies import ApiKeyAuth, DbDep, SettingsDep, require_scope
from app.db.models.enums import WfRunTrigger
from app.db.types import utcnow
from app.schemas.run import RunStatus
from app.schemas.service_api import (
    ApiFlowRunInput,
    ExternalUser,
    StopRequest,
    StopResult,
    WorkflowRunList,
    WorkflowRunRequest,
    WorkflowRunResult,
)
from app.schemas.wf_run import WfRunOut
from app.services.workflow_webapp import published_flow, start_version_run
from app.streaming.sse import sse_response

router = APIRouter(prefix="/workflows", tags=["service-api"])

UserQuery = Annotated[ExternalUser | None, Query()]


def elapsed_ms(run: WfRunOut, now: datetime | None = None) -> int:
    if run.started_at is None:
        return 0
    end = run.ended_at or now or utcnow()
    return max(0, int((end - run.started_at).total_seconds() * 1000))


def run_result(run: WfRunOut) -> WorkflowRunResult:
    return WorkflowRunResult(
        workflow_run_id=run.id,
        status=run.status,
        outputs=run.outputs,
        usage=run.usage,
        elapsed_ms=elapsed_ms(run),
        error=run.error,
        pending_interaction=run.pending_interaction,
        conversation_id=run.conversation_id,
        message_id=run.message_id,
        created_at=run.created_at,
        ended_at=run.ended_at,
    )


@router.post(
    "/run",
    response_model=None,
    responses={
        200: {
            "model": WorkflowRunResult,
            "description": "response_mode=streaming(기본)이면 SSE, blocking이면 WorkflowRunResult",
            "content": SSE_CONTENT,
        }
    },
)
async def run_workflow(
    body: WorkflowRunRequest,
    request: Request,
    key: ApiKeyAuth,
    db: DbDep,
    settings: SettingsDep,
    runs: WfRunServiceDep,
) -> Response:
    """발행 버전으로 실행한다. 발행 전이면 409 workflow_not_published, 입력이 start 필드에 맞지
    않으면 422 invalid_inputs.

    streaming이면 첫 이벤트가 workflow.queued이고 workflow.completed(입력 대기 포함) 뒤에 닫힌다.
    """
    workflow_id = flow_of(key, "workflow")
    require_scope(key, "workflow")
    flow = await published_flow(db, key.workspace_id, workflow_id)
    state = request.app.state
    started = await start_version_run(
        db,
        settings,
        bus=state.bus,
        supervisor=state.workflow_supervisor,
        storage=state.storage,
        workspace_id=key.workspace_id,
        workflow=flow.workflow,
        version=flow.version,
        inputs=body.inputs,
        trigger=WfRunTrigger.API,
        subscribe=True,
        api_key_id=key.key_id,
        external_user=body.user,
    )
    headers = wf_run_headers(started.run_id)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    result = await runs.wait_result(key.workspace_id, started)
    return JSONResponse(run_result(result).model_dump(mode="json"), headers=headers)


@router.get("/run/{run_id}", response_model=WorkflowRunResult)
async def get_workflow_run(
    run_id: uuid.UUID, key: ApiKeyAuth, runs: WfRunServiceDep, user: UserQuery = None
) -> WorkflowRunResult:
    """상태·출력·사용량·대기 중인 승인·질문(폴링용). 챗플로우 런도 run_id로 본다."""
    flow_of(key, "workflow", "chatflow")
    return run_result(await runs.check_run(key.workspace_id, run_id, flow_scope(key, user=user)))


@router.post("/run/{run_id}/stop", response_model=StopResult)
async def stop_workflow_run(
    run_id: uuid.UUID, key: ApiKeyAuth, runs: WfRunServiceDep, body: StopRequest | None = None
) -> StopResult:
    """중지. 돌던 런은 workflow.completed(cancelled)로 끝나고 입력을 기다리던 자식 런도 멈춘다."""
    flow_of(key, "workflow", "chatflow")
    user = body.user if body is not None else None
    await runs.check_run(key.workspace_id, run_id, flow_scope(key, user=user))
    out = await runs.stop(key.workspace_id, run_id)
    return StopResult(status=out.status)


@router.post(
    "/run/{run_id}/input",
    response_model=None,
    status_code=202,
    responses={
        202: {
            "model": WorkflowRunResult,
            "description": "response_mode=none(기본). 런이 다시 큐에 들어갔다",
        },
        200: {
            "model": WorkflowRunResult,
            "description": "response_mode=blocking이면 결과, streaming이면 SSE",
            "content": SSE_CONTENT,
        },
    },
)
async def provide_workflow_input(
    run_id: uuid.UUID, body: ApiFlowRunInput, key: ApiKeyAuth, runs: WfRunServiceDep
) -> Response:
    """agent 노드의 승인(decision)·질문(answer)과 human_input 폼(kind=form, values) 응답.

    같은 런이 다시 큐에 들어가 이어서 실행된다. 폼의 파일 칸에는 /files/upload로 올린 id를 준다.
    """
    flow_of(key, "workflow", "chatflow")
    await runs.check_run(key.workspace_id, run_id, flow_scope(key, user=body.user))
    subscribe = body.response_mode != "none"
    values = form_values(body.kind, body.values, answer=body.answer, decision=body.decision)
    if values is not None:
        resumed = await runs.provide_form(key.workspace_id, run_id, values, subscribe=subscribe)
    else:
        reply = InteractionReply(
            answer=body.answer,
            decision=body.decision,
            note=body.note,
            session_auto_approve=body.session_auto_approve,
        )
        resumed = await runs.provide_input(key.workspace_id, run_id, reply, subscribe=subscribe)
    headers = wf_run_headers(run_id)
    if body.response_mode == "streaming" and resumed.subscription is not None:
        return sse_response(resumed.subscription, headers=headers)
    if body.response_mode == "blocking":
        result = await runs.wait_result(key.workspace_id, resumed)
        return JSONResponse(run_result(result).model_dump(mode="json"), headers=headers)
    current = await runs.check_run(key.workspace_id, run_id, None)
    return JSONResponse(
        run_result(current).model_dump(mode="json"), status_code=202, headers=headers
    )


@router.get("/logs", response_model=WorkflowRunList)
async def list_workflow_runs(
    key: ApiKeyAuth,
    runs: WfRunServiceDep,
    status: RunStatus | None = None,
    user: UserQuery = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> WorkflowRunList:
    """이 키로 만든 런(최근 것부터). 챗플로우 키면 대화 턴마다의 런이다."""
    flow_of(key, "workflow", "chatflow")
    page = await runs.list_for_key(
        key.workspace_id,
        key.key_id,
        status=status,
        external_user=user,
        limit=limit,
        offset=parse_cursor(cursor) or 0,
    )
    return WorkflowRunList(
        items=[run_result(item) for item in page.items],
        next_cursor=str(page.next_offset) if page.next_offset is not None else None,
    )
