"""웹앱·서비스 API가 다룰 수 있는 세션의 범위(설계 6.1, 11장 S11).

콘솔은 범위 없이 모든 세션을 다룬다. 웹앱은 발행되어 webapp_enabled인 에이전트의 webapp 세션만,
서비스 API는 키에 묶인 에이전트의 api·task 세션만 다룬다. 범위 밖의 자원은 없는 것(404)으로 본다.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatSession
from app.db.repositories.agents import AgentRepository
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
