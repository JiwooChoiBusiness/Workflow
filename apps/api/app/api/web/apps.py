from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.console.agents import get_agent_service
from app.api.console.sessions import SessionServiceDep
from app.auth.dependencies import CurrentAuth
from app.db.models.enums import SessionSource
from app.schemas.session import SessionCreate, SessionList, SessionOut
from app.schemas.webapp import WebAppOut
from app.services.agents import AgentService

router = APIRouter(prefix="/apps", tags=["webapp"])

AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]


@router.get("/{slug}", response_model=WebAppOut)
async def get_app(slug: str, auth: CurrentAuth, agents: AgentServiceDep) -> WebAppOut:
    """발행되어 webapp_enabled인 에이전트의 공개 정보. 아니면 404 app_not_found."""
    return await agents.get_webapp(auth.workspace_id, slug)


@router.get("/{slug}/sessions", response_model=SessionList)
async def list_app_sessions(
    slug: str,
    auth: CurrentAuth,
    agents: AgentServiceDep,
    sessions: SessionServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SessionList:
    """이 웹앱의 대화 목록(source=webapp). 미리보기·API 세션은 섞이지 않는다."""
    app = await agents.get_webapp(auth.workspace_id, slug)
    return await sessions.list_sessions(
        auth.workspace_id,
        agent_id=app.agent_id,
        source=SessionSource.WEBAPP,
        limit=limit,
        offset=offset,
    )


@router.post("/{slug}/sessions", status_code=201, response_model=SessionOut)
async def create_app_session(
    slug: str,
    auth: CurrentAuth,
    agents: AgentServiceDep,
    sessions: SessionServiceDep,
    body: SessionCreate | None = None,
) -> SessionOut:
    """새 대화. 지금의 발행 버전을 쓴다."""
    app = await agents.get_webapp(auth.workspace_id, slug)
    return await sessions.create_published(
        auth.workspace_id,
        app.agent_id,
        source="webapp",
        title=body.title if body is not None else None,
    )
