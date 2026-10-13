import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.console.sessions import SessionServiceDep
from app.api.v1.common import parse_cursor
from app.api.v1.conversations import chat_scope
from app.auth.dependencies import ApiKeyAuth
from app.schemas.service_api import ApiMessageList, ExternalUser

router = APIRouter(tags=["service-api"])


@router.get("/messages", response_model=ApiMessageList)
async def list_messages(
    conversation_id: uuid.UUID,
    key: ApiKeyAuth,
    sessions: SessionServiceDep,
    user: Annotated[ExternalUser | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> ApiMessageList:
    """대화의 메시지(블록·파일 포함), seq 오름차순. 처음에는 마지막 limit개를 준다."""
    await sessions.check_session(key.workspace_id, conversation_id, chat_scope(key, user))
    page = await sessions.list_messages(
        key.workspace_id, conversation_id, before_seq=parse_cursor(cursor), limit=limit
    )
    next_cursor = str(page.items[0].seq) if page.has_more and page.items else None
    return ApiMessageList(items=page.items, has_more=page.has_more, next_cursor=next_cursor)
