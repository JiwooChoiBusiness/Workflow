import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.uploads import multipart_openapi, single_upload, upload_chunks
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.agent_file import AgentFileList, AgentFileOut
from app.services.files import MAX_AGENT_FILE_BYTES, AgentFileService
from app.storage import make_storage

router = APIRouter(prefix="/agents/{agent_id}/files", tags=["files"])


def get_file_service(db: DbDep, settings: SettingsDep) -> AgentFileService:
    return AgentFileService(db, make_storage(settings))


FileServiceDep = Annotated[AgentFileService, Depends(get_file_service)]


@router.get("", response_model=AgentFileList)
async def list_files(
    agent_id: uuid.UUID, auth: CurrentAuth, service: FileServiceDep
) -> AgentFileList:
    return await service.list_files(auth.workspace_id, agent_id)


@router.post("", status_code=201, response_model=AgentFileOut, openapi_extra=multipart_openapi())
async def upload_file(
    agent_id: uuid.UUID, request: Request, auth: CurrentAuth, service: FileServiceDep
) -> AgentFileOut:
    async with single_upload(request, max_bytes=MAX_AGENT_FILE_BYTES, what="첨부 파일") as upload:
        return await service.upload(
            auth.workspace_id,
            agent_id,
            filename=upload.filename,
            content_type=upload.content_type,
            chunks=upload_chunks(upload),
        )


@router.delete("/{file_id}", status_code=204)
async def delete_file(
    agent_id: uuid.UUID, file_id: uuid.UUID, auth: CurrentAuth, service: FileServiceDep
) -> None:
    await service.delete(auth.workspace_id, agent_id, file_id)
