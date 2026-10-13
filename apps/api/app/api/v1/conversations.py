"""대화 목록·상세·이름 변경·삭제(설계 6.4). 대화는 이 에이전트의 source=api 세션이다."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.console.sessions import SessionServiceDep
from app.api.v1.common import key_scope, parse_cursor
from app.auth.dependencies import ApiKeyAuth, require_scope
from app.db.models.enums import SessionSource
from app.schemas.service_api import (
    ConversationDetail,
    ConversationList,
    ConversationOut,
    ConversationPatch,
    ExternalUser,
)
from app.schemas.session import SessionOut, SessionPatch
from app.services.access import SessionScope
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


def chat_scope(key: AuthenticatedApiKey, user: str | None) -> SessionScope:
    require_scope(key, "chat")
    return key_scope(key, user=user, only=SessionSource.API)


@router.get("", response_model=ConversationList)
async def list_conversations(
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
    user: UserQuery = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> ConversationList:
    """최근에 대화한 순서. 메시지를 하나도 보내지 않은 대화(파일만 올린 대화)는 빠진다."""
    require_scope(key, "chat")
    page = await sessions.list_sessions(
        key.workspace_id,
        agent_id=key.agent_id,
        source=SessionSource.API,
        external_user=user,
        non_empty=True,
        limit=limit,
        offset=parse_cursor(cursor) or 0,
    )
    return ConversationList(
        items=[conversation_out(item) for item in page.items],
        next_cursor=str(page.next_offset) if page.next_offset is not None else None,
    )


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
    user: UserQuery = None,
) -> ConversationDetail:
    await sessions.check_session(key.workspace_id, conversation_id, chat_scope(key, user))
    detail = await sessions.get_session(key.workspace_id, conversation_id)
    return ConversationDetail(
        **conversation_out(detail).model_dump(),
        active_run=detail.active_run,
        last_run=detail.last_run,
    )


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def rename_conversation(
    conversation_id: uuid.UUID,
    body: ConversationPatch,
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
) -> ConversationOut:
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
    user: UserQuery = None,
) -> None:
    """진행 중인 런을 멈추고 작업공간을 지운다. 결과 파일 기록은 로그로 남는다."""
    await sessions.check_session(key.workspace_id, conversation_id, chat_scope(key, user))
    await sessions.delete_session(key.workspace_id, conversation_id)
