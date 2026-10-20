"""메시지 전송·중지·응답·재접속과 런 조회(설계 6.4, 2단계 설계 6.3).

경로는 하나이고 키 대상에 따라 갈린다. 에이전트 키는 1단계 세션 경로(대화·과업 런)로, 챗플로우 키는
챗플로우 대화와 그 워크플로우 런으로 간다. 이벤트 타입이 다르므로(run.* / workflow.*) 받는 쪽은 type
접두로 구분한다. 워크플로우(kind=workflow) 키는 400 wrong_target이다. 키 범위 밖의 런은 404다.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse, Response

from app.agent.interactions import InteractionReply
from app.api.console.runs import SSE_CONTENT, RunServiceDep, run_headers
from app.api.console.sessions import SessionServiceDep
from app.api.console.wf_conversations import ConversationServiceDep, chat_headers
from app.api.console.wf_runs import WfRunServiceDep, form_values, wf_run_headers
from app.api.v1.common import agent_of, flow_of, flow_scope, key_scope
from app.auth.dependencies import ApiKeyAuth, require_scope
from app.db.models.enums import SessionSource, WfConversationSource
from app.schemas.run import RunOut, RunResult
from app.schemas.service_api import (
    ApiFlowRunInput,
    ChatMessageCreate,
    ChatMessageResult,
    ExternalUser,
    FlowChatMessageResult,
    StopRequest,
    StopResult,
)
from app.schemas.session import MessageCreate
from app.schemas.wf_conversation import WfChatResult, WfMessageCreate
from app.schemas.wf_run import WfRunOut
from app.services.api_keys import AuthenticatedApiKey
from app.services.errors import ServiceError
from app.services.wf_conversations import WfConversationService
from app.streaming.sse import sse_response

router = APIRouter(tags=["service-api"])

UserQuery = Annotated[ExternalUser | None, Query()]
CHAT_RESULT = ChatMessageResult | FlowChatMessageResult


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


def flow_chat_result(result: WfChatResult) -> FlowChatMessageResult:
    run = result.run
    return FlowChatMessageResult(
        conversation_id=result.conversation_id,
        message_id=result.message_id,
        run_id=run.id,
        answer=result.answer,
        usage=run.usage,
        status=run.status,
        pending_interaction=run.pending_interaction,
        error=run.error,
    )


def is_flow(key: AuthenticatedApiKey) -> bool:
    """워크플로우 키인지. 챗플로우만 받는 경로면 flow_of가 workflow 키를 거절한다."""
    return key.agent_id is None


@router.post(
    "/chat-messages",
    response_model=None,
    responses={
        200: {
            "model": CHAT_RESULT,
            "description": (
                "response_mode=streaming(기본)이면 SSE, blocking이면 에이전트 키는 "
                "ChatMessageResult, 챗플로우 키는 FlowChatMessageResult"
            ),
            "content": SSE_CONTENT,
        }
    },
)
async def send_chat_message(
    body: ChatMessageCreate,
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
    runs: RunServiceDep,
    chats: ConversationServiceDep,
) -> Response:
    """메시지를 보내고 런을 만든다. conversation_id가 없으면 새 대화(발행 버전)를 연다."""
    if is_flow(key):
        return await _send_flow_message(body, key, chats)
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
            agent_of(key),
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


async def _send_flow_message(
    body: ChatMessageCreate, key: AuthenticatedApiKey, chats: WfConversationService
) -> Response:
    """챗플로우 키. 대화는 그 키로 만든 api 대화이고 metadata는 쓰지 않는다."""
    workflow_id = flow_of(key, "chatflow")
    require_scope(key, "chat")
    scope = flow_scope(key, user=body.user)
    conversation_id = body.conversation_id
    if conversation_id is None:
        conversation_id = await chats.files_conversation(key.workspace_id, body.files)
    if conversation_id is not None:
        conversation_id = (await chats.check(key.workspace_id, conversation_id, scope)).id
    else:
        created = await chats.create_published(
            key.workspace_id,
            workflow_id,
            source=WfConversationSource.API,
            api_key_id=key.key_id,
            external_user=body.user,
        )
        conversation_id = created.id
    message = WfMessageCreate(
        text=body.query, inputs=body.inputs, files=body.files, response_mode=body.response_mode
    )
    started = await chats.send_message(key.workspace_id, conversation_id, message, subscribe=True)
    headers = chat_headers(started)
    if body.response_mode == "streaming" and started.subscription is not None:
        return sse_response(started.subscription, headers=headers)
    result = await chats.wait_result(key.workspace_id, started)
    return JSONResponse(flow_chat_result(result).model_dump(mode="json"), headers=headers)


@router.post("/chat-messages/{run_id}/stop", response_model=StopResult)
async def stop_run(
    run_id: uuid.UUID,
    key: ApiKeyAuth,
    runs: RunServiceDep,
    wf_runs: WfRunServiceDep,
    body: StopRequest | None = None,
) -> StopResult:
    """중지. 에이전트 런은 run.completed(cancelled), 챗플로우 런은 workflow.completed(cancelled)로
    끝난다."""
    user = body.user if body is not None else None
    if is_flow(key):
        flow_of(key, "chatflow")
        await wf_runs.check_run(key.workspace_id, run_id, flow_scope(key, user=user))
        stopped = await wf_runs.stop(key.workspace_id, run_id)
        return StopResult(status=stopped.status)
    await runs.check_run(key.workspace_id, run_id, key_scope(key, user=user))
    out = await runs.stop(key.workspace_id, run_id)
    return StopResult(status=out.status)


@router.post(
    "/chat-messages/{run_id}/input",
    response_model=None,
    status_code=202,
    responses={
        202: {
            "model": RunOut | WfRunOut,
            "description": "response_mode=none(기본). 런이 다시 큐에 들어갔다",
        },
        200: {
            "model": CHAT_RESULT,
            "description": "response_mode=blocking이면 결과, streaming이면 SSE",
            "content": SSE_CONTENT,
        },
    },
)
async def provide_input(
    run_id: uuid.UUID,
    body: ApiFlowRunInput,
    key: ApiKeyAuth,
    runs: RunServiceDep,
    wf_runs: WfRunServiceDep,
    chats: ConversationServiceDep,
) -> Response:
    """질문(answer)·승인(decision) 응답. 같은 런이 다시 큐에 들어가 이어서 실행된다.

    챗플로우 키는 human_input 폼 응답(kind=form, values)도 보낸다. 에이전트 런에는 폼이 없어 422다.
    """
    reply = InteractionReply(
        answer=body.answer,
        decision=body.decision,
        note=body.note,
        session_auto_approve=body.session_auto_approve,
    )
    values = form_values(body.kind, body.values, answer=body.answer, decision=body.decision)
    if is_flow(key):
        flow_of(key, "chatflow")
        await wf_runs.check_run(key.workspace_id, run_id, flow_scope(key, user=body.user))
        subscribe = body.response_mode != "none"
        if values is not None:
            resumed = await wf_runs.provide_form(
                key.workspace_id, run_id, values, subscribe=subscribe
            )
        else:
            resumed = await wf_runs.provide_input(
                key.workspace_id, run_id, reply, subscribe=subscribe
            )
        headers = wf_run_headers(run_id)
        if body.response_mode == "streaming" and resumed.subscription is not None:
            return sse_response(resumed.subscription, headers=headers)
        if body.response_mode == "blocking":
            await wf_runs.wait_result(key.workspace_id, resumed)
            result = await chats.result(key.workspace_id, run_id)
            return JSONResponse(flow_chat_result(result).model_dump(mode="json"), headers=headers)
        current = await wf_runs.check_run(key.workspace_id, run_id, None)
        return JSONResponse(current.model_dump(mode="json"), status_code=202, headers=headers)
    if values is not None:
        raise ServiceError(
            422, "invalid_input", "에이전트 런에는 폼 응답(kind=form)을 보낼 수 없다"
        )
    await runs.check_run(key.workspace_id, run_id, key_scope(key, user=body.user))
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
    wf_runs: WfRunServiceDep,
    user: UserQuery = None,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> Response:
    """런 이벤트를 after(또는 Last-Event-ID) 다음부터 다시 받는다. 끝난 런이면 남은 기록만 준다."""
    start = after
    if start is None and last_event_id is not None and last_event_id.strip().isdigit():
        start = int(last_event_id.strip())
    if is_flow(key):
        flow_of(key, "chatflow")
        await wf_runs.check_run(key.workspace_id, run_id, flow_scope(key, user=user))
        subscription = await wf_runs.stream(key.workspace_id, run_id, after=start or 0)
        return sse_response(subscription, headers=wf_run_headers(run_id))
    run = await runs.check_run(key.workspace_id, run_id, key_scope(key, user=user))
    subscription = await runs.run_stream(key.workspace_id, run_id, after=start or 0)
    return sse_response(subscription, headers=run_headers(run.id, run.session_id))


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID, key: ApiKeyAuth, runs: RunServiceDep, user: UserQuery = None
) -> RunOut:
    """에이전트 런 상태·사용량·결과(과업)·대기 중인 승인·질문. 폴링용. 워크플로우 런은
    GET /workflows/run/{run_id}."""
    return await runs.check_run(key.workspace_id, run_id, key_scope(key, user=user))
