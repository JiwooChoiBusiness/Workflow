from app.db.models import Snapshot
from app.db.repositories.base import WorkspaceScopedRepository


class SnapshotRepository(WorkspaceScopedRepository[Snapshot]):
    model = Snapshot

    def add(self, row: Snapshot) -> Snapshot:
        return self._add(row)

    async def flush(self) -> None:
        await self._flush()
