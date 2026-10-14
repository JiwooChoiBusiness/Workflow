"""웹앱·서비스 API가 다룰 수 있는 세션의 범위(설계 6.1, 11장 S11).

콘솔은 범위 없이 모든 세션을 다룬다. 웹앱은 발행되어 webapp_enabled인 에이전트의 webapp 세션만,
서비스 API는 키에 묶인 에이전트의 api·task 세션만 다룬다. 범위 밖의 자원은 없는 것(404)으로 본다.

call_agent가 만든 자식 세션(source=task, 다른 에이전트)은 그 자체로는 범위 밖이다. 자식 런의
parent_run_id를 따라 뿌리 런까지 올라가 뿌리 런의 세션이 범위 안이면 허용한다(오케스트레이션 6.3).
그래서 엔드포인트를 더하지 않아도 자식 세션의 기존 경로(이벤트·파일·중지·응답)가 열린다.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatSession
from app.db.repositories.agents import AgentRepository
from app.db.repositories.runs import RunRepository
from app.db.repositories.sessions import SessionRepository
from app.services.errors import not_found


@dataclass(frozen=True)
class SessionScope:
    sources: frozenset[str]
    agent_id: uuid.UUID | None = None
    # 주면 그 외부 사용자(session.external_user)의 세션만 다룬다(서비스 API의 user).
    external_user: str | None = None
    # 웹앱: 에이전트가 발행되어 있고 webapp_enabled여야 한다.
    webapp: bool = False

    def only(self, sources: Iterable[str]) -> "SessionScope":
        return replace(self, sources=self.sources & frozenset(sources))


async def in_scope(
    session: AsyncSession, workspace_id: uuid.UUID, chat: ChatSession, scope: SessionScope
) -> bool:
    if await _directly_in_scope(session, workspace_id, chat, scope):
        return True
    if chat.deleted_at is not None:
        return False
    root = await _root_session(session, workspace_id, chat)
    return root is not None and await _directly_in_scope(session, workspace_id, root, scope)


async def _root_session(
    session: AsyncSession, workspace_id: uuid.UUID, chat: ChatSession
) -> ChatSession | None:
    """call_agent가 만든 세션이면 그 호출 사슬의 뿌리 런 세션. 아니면 None."""
    runs = RunRepository(session, workspace_id)
    child = await runs.child_run_in_session(chat.id)
    if child is None:
        return None
    chain = await runs.parent_chain(child.id)
    if not chain:
        return None
    return await SessionRepository(session, workspace_id).get_active(chain[-1].session_id)


async def _directly_in_scope(
    session: AsyncSession, workspace_id: uuid.UUID, chat: ChatSession, scope: SessionScope
) -> bool:
    if chat.deleted_at is not None or chat.source not in scope.sources:
        return False
    if scope.agent_id is not None and chat.agent_id != scope.agent_id:
        return False
    if scope.external_user is not None and chat.external_user != scope.external_user:
        return False
    if scope.webapp:
        agent = await AgentRepository(session, workspace_id).get_active(chat.agent_id)
        if agent is None or not agent.webapp_enabled or agent.published_version_id is None:
            return False
    return True


async def scoped_session(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    session_id: uuid.UUID,
    scope: SessionScope | None,
) -> ChatSession:
    """살아 있고 범위 안의 세션. 아니면 404."""
    chat = await SessionRepository(session, workspace_id).get_active(session_id)
    if chat is None:
        raise not_found("세션")
    if scope is not None and not await in_scope(session, workspace_id, chat, scope):
        raise not_found("세션")
    return chat
