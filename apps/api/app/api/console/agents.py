import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.agent import (
    AgentCreate,
    AgentList,
    AgentOut,
    AgentPatch,
    PublishOut,
    PublishRequest,
)
from app.schemas.agent_version import AgentVersionList, AgentVersionOut, DraftUpdate
from app.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyList
from app.services.agents import AgentService
from app.services.api_keys import ApiKeyService

router = APIRouter(prefix="/agents", tags=["agents"])


def get_agent_service(request: Request, db: DbDep, settings: SettingsDep) -> AgentService:
    # model_id 검사는 /models가 받아 둔 라이브 목록을 쓴다(네트워크를 다시 부르지 않는다).
    # 발행 스냅샷은 샌드박스 백엔드가 만들고 storage에 둔다(3.12).
    state = request.app.state
    return AgentService(
        db, settings, models=state.llm_status, backend=state.sandbox, storage=state.storage
    )


def get_api_key_service(db: DbDep) -> ApiKeyService:
    return ApiKeyService(db)


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
ApiKeyServiceDep = Annotated[ApiKeyService, Depends(get_api_key_service)]


@router.get("", response_model=AgentList)
async def list_agents(auth: CurrentAuth, service: AgentServiceDep) -> AgentList:
    return AgentList(items=await service.list_agents(auth.workspace_id))


@router.post("", status_code=201, response_model=AgentOut)
async def create_agent(body: AgentCreate, auth: CurrentAuth, service: AgentServiceDep) -> AgentOut:
    return await service.create_agent(auth.workspace_id, body)


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: uuid.UUID, auth: CurrentAuth, service: AgentServiceDep) -> AgentOut:
    return await service.get_agent(auth.workspace_id, agent_id)


@router.patch("/{agent_id}", response_model=AgentOut)
async def patch_agent(
    agent_id: uuid.UUID, body: AgentPatch, auth: CurrentAuth, service: AgentServiceDep
) -> AgentOut:
    return await service.update_agent(auth.workspace_id, agent_id, body)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: uuid.UUID, auth: CurrentAuth, service: AgentServiceDep) -> None:
    await service.delete_agent(auth.workspace_id, agent_id)


@router.get("/{agent_id}/draft", response_model=AgentVersionOut)
async def get_draft(
    agent_id: uuid.UUID, auth: CurrentAuth, service: AgentServiceDep
) -> AgentVersionOut:
    return await service.get_draft(auth.workspace_id, agent_id)


@router.put("/{agent_id}/draft", response_model=AgentVersionOut)
async def save_draft(
    agent_id: uuid.UUID, body: DraftUpdate, auth: CurrentAuth, service: AgentServiceDep
) -> AgentVersionOut:
    return await service.save_draft(auth.workspace_id, agent_id, body)


@router.post("/{agent_id}/publish", status_code=201, response_model=PublishOut)
async def publish(
    agent_id: uuid.UUID,
    auth: CurrentAuth,
    service: AgentServiceDep,
    body: PublishRequest | None = None,
) -> PublishOut:
    """초안을 발행한다. 미리보기 작업공간이 있으면 zip 스냅샷을 함께 만든다(동기, 3.12)."""
    note = body.change_note if body is not None else None
    return await service.publish(auth.workspace_id, agent_id, note)


@router.get("/{agent_id}/versions", response_model=AgentVersionList)
async def list_versions(
    agent_id: uuid.UUID, auth: CurrentAuth, service: AgentServiceDep
) -> AgentVersionList:
    return await service.list_versions(auth.workspace_id, agent_id)


@router.get("/{agent_id}/versions/{version_id}", response_model=AgentVersionOut)
async def get_version(
    agent_id: uuid.UUID, version_id: uuid.UUID, auth: CurrentAuth, service: AgentServiceDep
) -> AgentVersionOut:
    return await service.get_version(auth.workspace_id, agent_id, version_id)


@router.post("/{agent_id}/versions/{version_id}/restore", response_model=AgentOut)
async def restore_version(
    agent_id: uuid.UUID, version_id: uuid.UUID, auth: CurrentAuth, service: AgentServiceDep
) -> AgentOut:
    return await service.restore_version(auth.workspace_id, agent_id, version_id)


@router.get("/{agent_id}/api-keys", response_model=ApiKeyList)
async def list_api_keys(
    agent_id: uuid.UUID, auth: CurrentAuth, service: ApiKeyServiceDep
) -> ApiKeyList:
    return await service.list_keys(auth.workspace_id, agent_id)


@router.post("/{agent_id}/api-keys", status_code=201, response_model=ApiKeyCreated)
async def create_api_key(
    agent_id: uuid.UUID,
    body: ApiKeyCreate,
    auth: CurrentAuth,
    service: ApiKeyServiceDep,
    response: Response,
) -> ApiKeyCreated:
    # 원문 키가 든 응답이 브라우저·프록시 캐시에 남지 않게 한다.
    response.headers["Cache-Control"] = "no-store"
    return await service.create(auth.workspace_id, agent_id, body)


@router.delete("/{agent_id}/api-keys/{key_id}", status_code=204)
async def revoke_api_key(
    agent_id: uuid.UUID, key_id: uuid.UUID, auth: CurrentAuth, service: ApiKeyServiceDep
) -> None:
    await service.revoke(auth.workspace_id, agent_id, key_id)
