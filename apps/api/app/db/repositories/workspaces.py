import uuid
from typing import Any

from sqlalchemy import select

from app.db.models import Workspace
from app.db.repositories.base import GlobalRepository


class WorkspaceRepository(GlobalRepository[Workspace]):
    model = Workspace

    async def get(self, workspace_id: uuid.UUID) -> Workspace | None:
        return await self.session.get(Workspace, workspace_id)

    async def get_default(self) -> Workspace | None:
        # 1단계는 워크스페이스가 하나다. 초기 설정에서 만든 행을 쓴다.
        return await self.session.scalar(select(Workspace).order_by(Workspace.created_at).limit(1))

    async def list_all(self) -> list[Workspace]:
        # 기동 복구와 하우스키핑이 워크스페이스마다 범위 리포지토리를 열 때 쓴다.
        return list(await self.session.scalars(select(Workspace).order_by(Workspace.created_at)))

    def add(self, *, name: str, slug: str, settings: dict[str, Any]) -> Workspace:
        workspace = Workspace(name=name, slug=slug, settings=settings)
        self.session.add(workspace)
        return workspace
