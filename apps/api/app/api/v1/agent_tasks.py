"""과업 실행(설계 4.10, 6.4). AgentTaskService(13.1)의 얇은 HTTP 래퍼다.

승인·질문을 기다리면(waiting_input) /chat-messages/{run_id}/input으로 답하고 이 경로나
/runs/{run_id}로 결과를 다시 본다.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response

from app.agent.task_service import AgentTaskSpec
from app.api.console.runs import SSE_CONTENT, RunServiceDep, run_headers
from app.api.v1.common import TaskServiceDep, agent_of, file_scope, key_scope
from app.auth.dependencies import ApiKeyAuth, require_scope
from app.schemas.service_api import AgentTaskCreate, ExternalUser
from app.schemas.task import AgentTaskOut
from app.streaming.sse import sse_response

router = APIRouter(prefix="/agent-tasks", tags=["service-api"])


@router.post(
    "",
    response_model=None,
    responses={
        200: {
            "model": AgentTaskOut,
            "description": "response_mode=blocking(기본)이면 AgentTaskOut, streaming이면 SSE",
            "content": SSE_CONTENT,
        }
    },
)
async def create_task(body: AgentTaskCreate, key: ApiKeyAuth, tasks: TaskServiceDep) -> Response:
    """입력·지시·출력 스키마로 과업을 실행한다. run.completed의 data.output에 결과가 있다."""
    agent_id = agent_of(key)
    require_scope(key, "tasks")
    spec = AgentTaskSpec(
        workspace_id=key.workspace_id,
        agent_id=agent_id,
        inputs=body.inputs,
        instruction=body.instruction,
        output_schema=body.output_schema,
        files=body.files,
        limits=body.limits,
        session_mode=body.session_mode,
        session_id=body.session_id,
        external_user=body.user,
    )
    started = await tasks.create(
        spec, subscribe=True, scope=file_scope(key, user=body.user), api_key_id=key.key_id
    )
    headers = run_headers(started.run_id, started.session_id)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    view = await tasks.wait_started(key.workspace_id, started)
    return JSONResponse(view.model_dump(mode="json"), headers=headers)


@router.get("/{run_id}", response_model=AgentTaskOut)
async def get_task(
    run_id: uuid.UUID,
    key: ApiKeyAuth,
    tasks: TaskServiceDep,
    runs: RunServiceDep,
    user: Annotated[ExternalUser | None, Query()] = None,
) -> AgentTaskOut:
    """과업 상태와 결과. 진행 중이면 지금까지의 글과 사용량만 있다.

    대화(api 세션)에서 부른 하위 에이전트의 과업 런도 키의 범위로 연다. 범위는 부모 사슬의 뿌리
    세션으로 보므로 세션 소스를 task로 좁히지 않고, 과업 런인지는 view가 본다(오케스트레이션 6.3).
    """
    agent_of(key)
    require_scope(key, "tasks")
    await runs.check_run(key.workspace_id, run_id, key_scope(key, user=user))
    return await tasks.view(key.workspace_id, run_id)
