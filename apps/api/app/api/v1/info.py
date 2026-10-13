from fastapi import APIRouter

from app.api.v1.common import AgentServiceDep
from app.auth.dependencies import ApiKeyAuth
from app.schemas.service_api import AgentInfoOut, AgentParametersOut, FileUploadLimits
from app.schemas.session import MAX_MESSAGE_FILES
from app.services.sessions import MAX_UPLOAD_BYTES

router = APIRouter(tags=["service-api"])


@router.get("/info", response_model=AgentInfoOut)
async def get_info(key: ApiKeyAuth, agents: AgentServiceDep) -> AgentInfoOut:
    """키에 묶인 에이전트와 그 발행 버전."""
    agent = await agents.get_agent(key.workspace_id, key.agent_id)
    version = (
        await agents.get_version(key.workspace_id, agent.id, agent.published_version_id)
        if agent.published_version_id is not None
        else None
    )
    return AgentInfoOut(
        agent_id=agent.id,
        slug=agent.slug,
        name=agent.name,
        icon=agent.icon,
        description=agent.description,
        published=version is not None,
        version_id=version.id if version is not None else None,
        version_number=version.version_number if version is not None else None,
        published_at=agent.published_at,
        approval_mode=version.approval_mode if version is not None else None,
        webapp_enabled=agent.webapp_enabled,
    )


@router.get("/parameters", response_model=AgentParametersOut)
async def get_parameters(key: ApiKeyAuth, agents: AgentServiceDep) -> AgentParametersOut:
    """발행 버전의 승인 모드·한도·도구 그룹과 업로드 한도. 발행 전이면 null."""
    agent = await agents.get_agent(key.workspace_id, key.agent_id)
    version = (
        await agents.get_version(key.workspace_id, agent.id, agent.published_version_id)
        if agent.published_version_id is not None
        else None
    )
    return AgentParametersOut(
        approval_mode=version.approval_mode if version is not None else None,
        limits=version.limits if version is not None else None,
        tools=list(version.tools) if version is not None else [],
        file_upload=FileUploadLimits(
            max_bytes=MAX_UPLOAD_BYTES, max_files_per_message=MAX_MESSAGE_FILES
        ),
        response_modes=["streaming", "blocking"],
        scopes=list(key.scopes),
    )
