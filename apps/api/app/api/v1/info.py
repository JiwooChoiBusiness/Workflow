"""키에 묶인 대상의 정보와 매개변수(설계 6.4, 2단계 설계 6.3). 에이전트 키와 워크플로우 키가 각자의
모양으로 받는다."""

from fastapi import APIRouter

from app.api.console.workflows import WorkflowServiceDep
from app.api.v1.common import AgentServiceDep, agent_of, flow_of
from app.auth.dependencies import ApiKeyAuth
from app.schemas.service_api import (
    AgentInfoOut,
    AgentParametersOut,
    FileUploadLimits,
    FlowInfoOut,
    FlowParametersOut,
)
from app.schemas.session import MAX_MESSAGE_FILES
from app.schemas.webapp import FlowField
from app.services.api_keys import AuthenticatedApiKey
from app.services.sessions import MAX_UPLOAD_BYTES
from app.services.workflow_files import MAX_WF_FILE_BYTES
from app.services.workflow_webapp import start_fields
from app.services.workflows import WorkflowService

router = APIRouter(tags=["service-api"])


@router.get("/info", response_model=AgentInfoOut | FlowInfoOut)
async def get_info(
    key: ApiKeyAuth, agents: AgentServiceDep, workflows: WorkflowServiceDep
) -> AgentInfoOut | FlowInfoOut:
    """키에 묶인 에이전트(또는 워크플로우)와 그 발행 버전."""
    if key.agent_id is None:
        return await _flow_info(key, workflows)
    agent = await agents.get_agent(key.workspace_id, agent_of(key))
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


async def _flow_info(key: AuthenticatedApiKey, workflows: WorkflowService) -> FlowInfoOut:
    workflow = await workflows.get_workflow(key.workspace_id, flow_of(key, "workflow", "chatflow"))
    return FlowInfoOut(
        workflow_id=workflow.id,
        slug=workflow.slug,
        name=workflow.name,
        icon=workflow.icon,
        description=workflow.description,
        kind=workflow.kind,
        published=workflow.published_version_id is not None,
        version_id=workflow.published_version_id,
        version_number=workflow.published_version_number,
        published_at=workflow.published_at,
        webapp_enabled=workflow.webapp_enabled,
    )


@router.get("/parameters", response_model=AgentParametersOut | FlowParametersOut)
async def get_parameters(
    key: ApiKeyAuth, agents: AgentServiceDep, workflows: WorkflowServiceDep
) -> AgentParametersOut | FlowParametersOut:
    """발행 버전의 입력·한도와 업로드 한도. 에이전트는 승인 모드·도구 그룹, 워크플로우는 start
    필드(user_input_form)다. 발행 전이면 null·빈 목록이다."""
    if key.agent_id is None:
        return await _flow_parameters(key, workflows)
    agent = await agents.get_agent(key.workspace_id, agent_of(key))
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


async def _flow_parameters(
    key: AuthenticatedApiKey, workflows: WorkflowService
) -> FlowParametersOut:
    workflow = await workflows.get_workflow(key.workspace_id, flow_of(key, "workflow", "chatflow"))
    version = (
        await workflows.get_version(key.workspace_id, workflow.id, workflow.published_version_id)
        if workflow.published_version_id is not None
        else None
    )
    fields = start_fields(version.graph) if version is not None else []
    webapp = version.settings.webapp if version is not None else None
    return FlowParametersOut(
        kind=workflow.kind,
        user_input_form=[FlowField.model_validate(field.model_dump()) for field in fields],
        limits=version.settings.limits if version is not None else None,
        file_upload=FileUploadLimits(
            max_bytes=MAX_WF_FILE_BYTES, max_files_per_message=MAX_MESSAGE_FILES
        ),
        response_modes=["streaming", "blocking"],
        scopes=list(key.scopes),
        opening_statement=webapp.opening_statement if webapp is not None else None,
        suggested_questions=list(webapp.suggested_questions) if webapp is not None else [],
    )
