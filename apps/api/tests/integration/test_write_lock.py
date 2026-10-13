import asyncio

from sqlalchemy import func, select

from app.config import Settings
from app.db.engine import Database
from app.db.models import Workspace
from app.db.repositories.workspaces import WorkspaceRepository


async def insert_workspace(db: Database, slug: str) -> None:
    async with db.write() as session:
        WorkspaceRepository(session).add(name=slug, slug=slug, settings={})
        await session.flush()
        # 트랜잭션을 연 채로 이벤트 루프에 양보해 다른 쓰기와 실제로 겹치게 한다.
        await asyncio.sleep(0)


async def count_workspaces(db: Database) -> int:
    async with db.read() as session:
        return await session.scalar(select(func.count()).select_from(Workspace)) or 0


async def test_fifty_concurrent_writes(db: Database) -> None:
    await asyncio.gather(*(insert_workspace(db, f"w{i}") for i in range(50)))

    assert await count_workspaces(db) == 50


async def test_reads_run_while_writes_queue(db: Database) -> None:
    writes = [insert_workspace(db, f"w{i}") for i in range(50)]
    reads = [count_workspaces(db) for _ in range(50)]

    results = await asyncio.gather(*writes, *reads)

    counts = [count for count in results if count is not None]
    assert all(0 <= count <= 50 for count in counts)
    assert await count_workspaces(db) == 50


async def test_two_engines_on_one_file(db: Database, settings: Settings) -> None:
    # 다른 프로세스(백업 CLI, reset-password)가 같은 파일에 쓰는 경우. 프로세스 안의 락은
    # 서로 모르므로 BEGIN IMMEDIATE와 busy_timeout만으로 버텨야 한다.
    other = Database(settings.data_dir / "app.db")
    try:
        await asyncio.gather(
            *(insert_workspace(db, f"a{i}") for i in range(25)),
            *(insert_workspace(other, f"b{i}") for i in range(25)),
        )
    finally:
        await other.close()

    assert await count_workspaces(db) == 50
