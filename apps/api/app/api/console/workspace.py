from fastapi import APIRouter, HTTPException

from app.auth.dependencies import CurrentAuth, DbDep
from app.db.models import Workspace
from app.db.repositories.workspaces import WorkspaceRepository
from app.schemas.workspace import WorkspaceOut, WorkspacePatch, WorkspaceSettings

router = APIRouter(prefix="/workspace", tags=["workspace"])


def _out(workspace: Workspace) -> WorkspaceOut:
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        settings=WorkspaceSettings.model_validate(workspace.settings),
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


@router.get("", response_model=WorkspaceOut)
async def get_workspace(auth: CurrentAuth, db: DbDep) -> WorkspaceOut:
    async with db.read() as session:
        workspace = await WorkspaceRepository(session).get(auth.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404)
    return _out(workspace)


@router.patch("", response_model=WorkspaceOut)
async def patch_workspace(body: WorkspacePatch, auth: CurrentAuth, db: DbDep) -> WorkspaceOut:
    async with db.write() as session:
        workspace = await WorkspaceRepository(session).get(auth.workspace_id)
        if workspace is None:
            raise HTTPException(status_code=404)
        if body.name is not None:
            workspace.name = body.name
        if body.settings is not None:
            workspace.settings = body.settings.model_dump(mode="json")
        await session.flush()
    return _out(workspace)
