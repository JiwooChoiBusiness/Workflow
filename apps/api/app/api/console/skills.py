import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.uploads import multipart_openapi, single_upload, upload_chunks
from app.auth.dependencies import CurrentAuth, DbDep, SettingsDep
from app.schemas.skill import SkillDetailOut, SkillList, SkillVersionOut
from app.services.skills import SkillService
from app.skills.package import MAX_PACKAGE_BYTES
from app.storage import make_storage

router = APIRouter(tags=["skills"])


def get_skill_service(db: DbDep, settings: SettingsDep) -> SkillService:
    return SkillService(db, make_storage(settings))


SkillServiceDep = Annotated[SkillService, Depends(get_skill_service)]


@router.get("/skills", response_model=SkillList)
async def list_library_skills(auth: CurrentAuth, service: SkillServiceDep) -> SkillList:
    return await service.list_library(auth.workspace_id)


@router.post(
    "/skills", status_code=201, response_model=SkillDetailOut, openapi_extra=multipart_openapi()
)
async def create_library_skill(
    request: Request, auth: CurrentAuth, service: SkillServiceDep
) -> SkillDetailOut:
    async with single_upload(request, max_bytes=MAX_PACKAGE_BYTES, what="스킬 패키지") as upload:
        return await service.create(auth.workspace_id, upload_chunks(upload))


@router.get("/agents/{agent_id}/skills", response_model=SkillList)
async def list_embedded_skills(
    agent_id: uuid.UUID, auth: CurrentAuth, service: SkillServiceDep
) -> SkillList:
    return await service.list_embedded(auth.workspace_id, agent_id)


@router.post(
    "/agents/{agent_id}/skills",
    status_code=201,
    response_model=SkillDetailOut,
    openapi_extra=multipart_openapi(),
)
async def create_embedded_skill(
    agent_id: uuid.UUID, request: Request, auth: CurrentAuth, service: SkillServiceDep
) -> SkillDetailOut:
    async with single_upload(request, max_bytes=MAX_PACKAGE_BYTES, what="스킬 패키지") as upload:
        return await service.create(auth.workspace_id, upload_chunks(upload), agent_id=agent_id)


@router.get("/skills/{skill_id}", response_model=SkillDetailOut)
async def get_skill(
    skill_id: uuid.UUID, auth: CurrentAuth, service: SkillServiceDep
) -> SkillDetailOut:
    return await service.get(auth.workspace_id, skill_id)


@router.delete("/skills/{skill_id}", status_code=204)
async def delete_skill(skill_id: uuid.UUID, auth: CurrentAuth, service: SkillServiceDep) -> None:
    await service.delete(auth.workspace_id, skill_id)


@router.post(
    "/skills/{skill_id}/versions",
    status_code=201,
    response_model=SkillVersionOut,
    openapi_extra=multipart_openapi(),
)
async def add_skill_version(
    skill_id: uuid.UUID, request: Request, auth: CurrentAuth, service: SkillServiceDep
) -> SkillVersionOut:
    async with single_upload(request, max_bytes=MAX_PACKAGE_BYTES, what="스킬 패키지") as upload:
        return await service.add_version(auth.workspace_id, skill_id, upload_chunks(upload))


@router.get(
    "/skills/{skill_id}/versions/{version_id}/files/{path:path}",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/plain": {}, "application/octet-stream": {}}}},
)
async def get_skill_file(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    path: str,
    auth: CurrentAuth,
    service: SkillServiceDep,
) -> StreamingResponse:
    member = await service.open_file(auth.workspace_id, skill_id, version_id, path)
    # 스크립트·HTML도 브라우저가 실행하지 않도록 텍스트는 text/plain, 나머지는 내려받기로만 준다
    # (11장 S9).
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
    if member.is_text:
        media_type = "text/plain; charset=utf-8"
        headers["Content-Disposition"] = "inline"
    else:
        media_type = "application/octet-stream"
        name = path.rsplit("/", 1)[-1]
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(name)}"
    return StreamingResponse(member.chunks, media_type=media_type, headers=headers)
