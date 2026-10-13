import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.console.sessions import SessionServiceDep
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.logs import WorkspaceOverviewList
from app.schemas.session import (
    WorkspaceKillOut,
    WorkspaceKillRequest,
    WorkspaceStatusOut,
)
from app.services.agents import AgentService
from app.services.workspaces import WorkspaceOverviewService

router = APIRouter(tags=["workspaces"])


def get_agent_service(db: DbDep, settings: SettingsDep) -> AgentService:
    return AgentService(db, settings)


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]


@router.get("/workspaces", response_model=WorkspaceOverviewList)
async def list_workspaces(
    request: Request, auth: CurrentAuth, db: DbDep, settings: SettingsDep
) -> WorkspaceOverviewList:
    """세션 작업공간 목록: 세션·에이전트, 크기(런이 끝날 때 잰 값), 실행 중인 도구 프로세스와
    그 메모리 최고 요청치. 정리는 세션별 workspace/kill·clean-packages·reset으로 한다."""
    service = WorkspaceOverviewService(db, settings, backend=request.app.state.sandbox)
    return await service.list_workspaces(auth.workspace_id)


@router.get("/sessions/{session_id}/workspace", response_model=WorkspaceStatusOut)
async def workspace_status(
    session_id: uuid.UUID, auth: CurrentAuth, service: SessionServiceDep
) -> WorkspaceStatusOut:
    """경로, 디스크, 큰 폴더 상위 10개, 실행 중인 도구 프로세스(3.14)."""
    return await service.workspace_status(auth.workspace_id, session_id)


@router.post("/sessions/{session_id}/workspace/kill", response_model=WorkspaceKillOut)
async def kill_workspace_processes(
    session_id: uuid.UUID,
    auth: CurrentAuth,
    service: SessionServiceDep,
    body: WorkspaceKillRequest | None = None,
) -> WorkspaceKillOut:
    exec_id = body.exec_id if body is not None else None
    return await service.kill_processes(auth.workspace_id, session_id, exec_id)


@router.post("/sessions/{session_id}/workspace/clean-packages", response_model=WorkspaceStatusOut)
async def clean_workspace_packages(
    session_id: uuid.UUID, auth: CurrentAuth, service: SessionServiceDep
) -> WorkspaceStatusOut:
    return await service.clean_packages(auth.workspace_id, session_id)


@router.post("/sessions/{session_id}/workspace/reset", response_model=WorkspaceStatusOut)
async def reset_workspace(
    session_id: uuid.UUID,
    auth: CurrentAuth,
    service: SessionServiceDep,
    agents: AgentServiceDep,
) -> WorkspaceStatusOut:
    """작업공간 초기화. 폴더를 지우고 첨부·스킬을 다시 채운다. 메시지는 남는다."""
    return await service.reset_workspace(auth.workspace_id, session_id, agents)
