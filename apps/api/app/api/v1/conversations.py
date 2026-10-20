"""대화 목록·상세·이름 변경·삭제(설계 6.4, 2단계 설계 6.3).

에이전트 키의 대화는 그 에이전트의 source=api 세션이다. 챗플로우 키의 대화는 그 키로 만든
wf_conversation이고 응답은 같은 이름에 agent_version_id 대신 wf_version_id를 쓴다. 워크플로우
(kind=workflow) 키는 400 wrong_target이다.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.console.sessions import SessionServiceDep
from app.api.console.wf_conversations import ConversationServiceDep
from app.api.v1.common import agent_of, flow_of, flow_scope, key_scope, parse_cursor
from app.auth.dependencies import ApiKeyAuth, require_scope
from app.db.models.enums import SessionSource, WfConversationSource
from app.schemas.service_api import (
    ConversationDetail,
    ConversationList,
    ConversationOut,
    ConversationPatch,
    ExternalUser,
    FlowConversationDetail,
    FlowConversationList,
    FlowConversationOut,
)
from app.schemas.session import SessionOut, SessionPatch
from app.schemas.wf_conversation import WfConversationOut, WfConversationPatch
from app.services.access import FlowScope, SessionScope
from app.services.api_keys import AuthenticatedApiKey

router = APIRouter(prefix="/conversations", tags=["service-api"])

UserQuery = Annotated[ExternalUser | None, Query()]


def conversation_out(item: SessionOut) -> ConversationOut:
    return ConversationOut(
        id=item.id,
        name=item.title,
        external_user=item.external_user,
        agent_version_id=item.agent_version_id,
        message_count=item.message_count,
        last_message_at=item.last_message_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def flow_conversation_out(item: WfConversationOut) -> FlowConversationOut:
    return FlowConversationOut(
        id=item.id,
        name=item.title,
        external_user=item.external_user,
        wf_version_id=item.wf_version_id,
        message_count=item.message_count,
        last_message_at=item.last_message_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def chat_scope(key: AuthenticatedApiKey, user: str | None) -> SessionScope:
    require_scope(key, "chat")
    return key_scope(key, user=user, only=SessionSource.API)


def chatflow_scope(key: AuthenticatedApiKey, user: str | None) -> FlowScope:
    """챗플로우 키의 대화 범위. 에이전트 키는 여기 오지 않는다."""
    flow_of(key, "chatflow")
    require_scope(key, "chat")
    return flow_scope(key, user=user)


@router.get("", response_model=ConversationList | FlowConversationList)
async def list_conversations(
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
    chats: ConversationServiceDep,
    user: UserQuery = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> ConversationList | FlowConversationList:
    """최근에 대화한 순서. 메시지를 하나도 보내지 않은 대화(파일만 올린 대화)는 빠진다."""
    offset = parse_cursor(cursor) or 0
    if key.agent_id is None:
        chatflow_scope(key, user)
        flows = await chats.list_conversations(
            key.workspace_id,
            workflow_id=key.workflow_id,
            source=WfConversationSource.API,
            api_key_id=key.key_id,
            external_user=user,
            non_empty=True,
            limit=limit,
            offset=offset,
        )
        return FlowConversationList(
            items=[flow_conversation_out(item) for item in flows.items],
            next_cursor=str(flows.next_offset) if flows.next_offset is not None else None,
        )
    require_scope(key, "chat")
    page = await sessions.list_sessions(
        key.workspace_id,
        agent_id=agent_of(key),
        source=SessionSource.API,
        external_user=user,
        non_empty=True,
        limit=limit,
        offset=offset,
    )
    return ConversationList(
        items=[conversation_out(item) for item in page.items],
        next_cursor=str(page.next_offset) if page.next_offset is not None else None,
    )


@router.get("/{conversation_id}", response_model=ConversationDetail | FlowConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
    chats: ConversationServiceDep,
    user: UserQuery = None,
) -> ConversationDetail | FlowConversationDetail:
    if key.agent_id is None:
        await chats.check(key.workspace_id, conversation_id, chatflow_scope(key, user))
        flow = await chats.get(key.workspace_id, conversation_id)
        return FlowConversationDetail(
            **flow_conversation_out(flow).model_dump(),
            active_run=flow.active_run,
            last_run=flow.last_run,
        )
    await sessions.check_session(key.workspace_id, conversation_id, chat_scope(key, user))
    detail = await sessions.get_session(key.workspace_id, conversation_id)
    return ConversationDetail(
        **conversation_out(detail).model_dump(),
        active_run=detail.active_run,
        last_run=detail.last_run,
    )


@router.patch("/{conversation_id}", response_model=ConversationOut | FlowConversationOut)
async def rename_conversation(
    conversation_id: uuid.UUID,
    body: ConversationPatch,
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
    chats: ConversationServiceDep,
) -> ConversationOut | FlowConversationOut:
    if key.agent_id is None:
        await chats.check(key.workspace_id, conversation_id, chatflow_scope(key, body.user))
        renamed = await chats.update(
            key.workspace_id, conversation_id, WfConversationPatch(title=body.name)
        )
        return flow_conversation_out(renamed)
    await sessions.check_session(key.workspace_id, conversation_id, chat_scope(key, body.user))
    updated = await sessions.update_session(
        key.workspace_id, conversation_id, SessionPatch(title=body.name)
    )
    return conversation_out(updated)


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID,
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
    chats: ConversationServiceDep,
    user: UserQuery = None,
) -> None:
    """진행 중인 런을 멈추고 지운다. 에이전트 대화는 작업공간도 지운다. 로그는 남는다."""
    if key.agent_id is None:
        await chats.check(key.workspace_id, conversation_id, chatflow_scope(key, user))
        await chats.delete(key.workspace_id, conversation_id)
        return
    await sessions.check_session(key.workspace_id, conversation_id, chat_scope(key, user))
    await sessions.delete_session(key.workspace_id, conversation_id)
