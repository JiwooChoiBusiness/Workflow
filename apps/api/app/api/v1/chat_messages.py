"""메시지 전송·중지·응답·재접속과 런 조회(설계 6.4).

중지·응답·재접속·런 조회는 대화(api)와 과업(task) 런에 같이 쓴다. 키의 권한이 허용하는 세션의 런만
다룬다.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse, Response

from app.agent.interactions import InteractionReply
from app.api.console.runs import SSE_CONTENT, RunServiceDep, run_headers
from app.api.console.sessions import SessionServiceDep
from app.api.v1.common import key_scope
from app.auth.dependencies import ApiKeyAuth, require_scope
from app.db.models.enums import SessionSource
from app.schemas.run import RunOut, RunResult
from app.schemas.service_api import (
    ApiRunInput,
    ChatMessageCreate,
    ChatMessageResult,
    ExternalUser,
    StopRequest,
    StopResult,
)
from app.schemas.session import MessageCreate
from app.streaming.sse import sse_response

router = APIRouter(tags=["service-api"])

UserQuery = Annotated[ExternalUser | None, Query()]


def chat_result(result: RunResult) -> ChatMessageResult:
    run = result.run
    return ChatMessageResult(
        conversation_id=run.session_id,
        message_id=result.message_id,
        run_id=run.id,
        answer=result.answer,
        files=result.files,
        usage=run.usage,
        status=run.status,
        pending_interaction=run.pending_interaction,
        error=run.error,
    )


@router.post(
    "/chat-messages",
    response_model=None,
    responses={
        200: {
            "model": ChatMessageResult,
            "description": "response_mode=streaming(기본)이면 SSE, blocking이면 ChatMessageResult",
            "content": SSE_CONTENT,
        }
    },
)
async def send_chat_message(
    body: ChatMessageCreate,
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
    runs: RunServiceDep,
) -> Response:
    """메시지를 보내고 런을 만든다. conversation_id가 없으면 새 대화(발행 버전)를 연다."""
    require_scope(key, "chat")
    scope = key_scope(key, user=body.user, only=SessionSource.API)
    if body.conversation_id is not None:
        conversation_id = (
            await sessions.check_session(key.workspace_id, body.conversation_id, scope)
        ).id
    elif body.files:
        conversation_id = await sessions.upload_session(key.workspace_id, body.files, scope)
    else:
        created = await sessions.create_published(
            key.workspace_id,
            key.agent_id,
            source="api",
            api_key_id=key.key_id,
            external_user=body.user,
            meta={"metadata": body.metadata} if body.metadata else None,
        )
        conversation_id = created.id
    started = await runs.send_message(
        key.workspace_id,
        conversation_id,
        MessageCreate(text=body.query, file_ids=body.files, response_mode=body.response_mode),
        subscribe=True,
        inputs=body.inputs or None,
    )
    headers = run_headers(started.run_id, started.session_id)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    result = await runs.wait_result(key.workspace_id, started)
    return JSONResponse(chat_result(result).model_dump(mode="json"), headers=headers)


@router.post("/chat-messages/{run_id}/stop", response_model=StopResult)
async def stop_run(
    run_id: uuid.UUID, key: ApiKeyAuth, runs: RunServiceDep, body: StopRequest | None = None
) -> StopResult:
    """중지. 실행 중이면 도구 프로세스까지 끝내고 run.completed(cancelled)로 끝난다."""
    user = body.user if body is not None else None
    await runs.check_run(key.workspace_id, run_id, key_scope(key, user=user))
    out = await runs.stop(key.workspace_id, run_id)
    return StopResult(status=out.status)


@router.post(
    "/chat-messages/{run_id}/input",
    response_model=None,
    status_code=202,
    responses={
        202: {"model": RunOut, "description": "response_mode=none(기본). 런이 다시 큐에 들어갔다"},
        200: {
            "model": ChatMessageResult,
            "description": "response_mode=blocking이면 ChatMessageResult, streaming이면 SSE",
            "content": SSE_CONTENT,
        },
    },
)
async def provide_input(
    run_id: uuid.UUID, body: ApiRunInput, key: ApiKeyAuth, runs: RunServiceDep
) -> Response:
    """질문(answer)·승인(decision) 응답. 같은 런이 다시 큐에 들어가 이어서 실행된다."""
    await runs.check_run(key.workspace_id, run_id, key_scope(key, user=body.user))
    reply = InteractionReply(
        answer=body.answer,
        decision=body.decision,
        note=body.note,
        session_auto_approve=body.session_auto_approve,
    )
    started = await runs.provide_input(
        key.workspace_id, run_id, reply, subscribe=body.response_mode != "none"
    )
    headers = run_headers(started.run_id, started.session_id)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    if body.response_mode == "blocking":
        result = await runs.wait_result(key.workspace_id, started)
        return JSONResponse(chat_result(result).model_dump(mode="json"), headers=headers)
    run = await runs.get_run(key.workspace_id, run_id)
    return JSONResponse(run.model_dump(mode="json"), status_code=202, headers=headers)


@router.get(
    "/chat-messages/{run_id}/events",
    response_class=Response,
    responses={200: {"description": "런 이벤트 SSE", "content": SSE_CONTENT}},
)
async def run_events(
    run_id: uuid.UUID,
    key: ApiKeyAuth,
    runs: RunServiceDep,
    user: UserQuery = None,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> Response:
    """런 이벤트를 after(또는 Last-Event-ID) 다음부터 다시 받는다. 끝난 런이면 남은 기록만 준다."""
    run = await runs.check_run(key.workspace_id, run_id, key_scope(key, user=user))
    start = after
    if start is None and last_event_id is not None and last_event_id.strip().isdigit():
        start = int(last_event_id.strip())
    subscription = await runs.run_stream(key.workspace_id, run_id, after=start or 0)
    return sse_response(subscription, headers=run_headers(run.id, run.session_id))


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID, key: ApiKeyAuth, runs: RunServiceDep, user: UserQuery = None
) -> RunOut:
    """런 상태·사용량·결과(과업)·대기 중인 승인·질문. 폴링용."""
    return await runs.check_run(key.workspace_id, run_id, key_scope(key, user=user))
