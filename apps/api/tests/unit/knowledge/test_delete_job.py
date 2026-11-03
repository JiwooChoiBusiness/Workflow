"""문서 삭제 작업(3단계 설계 4.4, 5.4). index.db 행(1,000 유닛씩) → 자식·청크 행(1,000행씩) →
원본 파일 → 문서 행 순으로 지운다. 멈추면(종료) 그 자리에서 나가고 다시 돌면 이어서 지운다.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import KbDocument, KbSegment, KnowledgeBase
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id
from app.knowledge.units import FtsRow, VecRow, doc_id_of
from app.llm.embeddings import EmbeddingConfig, content_hash, pack_vector
from app.llm.fake_adapter import FakeAdapter, fake_embedding
from app.runner.supervisor import CancelSignal
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.services import knowledge_index_executor as executor_module
from app.services import knowledge_runtime
from app.services.knowledge_index_executor import KnowledgeIndexExecutor
from app.services.knowledge_runtime import KnowledgeRuntime, build_knowledge_runtime
from app.storage.base import kb_file_key
from app.storage.local_fs import LocalFsStorage

NOW = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
CONFIG = EmbeddingConfig(dimensions=8)


@dataclass
class World:
    db: Database
    runtime: KnowledgeRuntime
    executor: KnowledgeIndexExecutor
    storage: LocalFsStorage
    workspace_id: uuid.UUID
    kb_id: uuid.UUID
    document_id: uuid.UUID
    storage_key: str


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    settings = Settings.model_validate({"data_dir": tmp_path / "data"})
    db = Database(tmp_path / "app.db")
    await upgrade_head(db)
    runtime = build_knowledge_runtime(settings, FakeAdapter())
    assert await runtime.open()
    storage = LocalFsStorage(tmp_path / "storage")
    backend = LocalWorkspaceBackend(settings, workspaces_dir=tmp_path / "kb-work")
    workspace_id, kb_id, document_id = new_id(), new_id(), new_id()
    key = kb_file_key(workspace_id, document_id)
    await storage.write_file(key, _write(tmp_path / "source.txt"))
    async with db.write() as session:
        workspace = WorkspaceRepository(session).add(name="w", slug="w", settings={})
        workspace.id = workspace_id
        await session.flush()
        session.add(
            KnowledgeBase(
                id=kb_id,
                workspace_id=workspace_id,
                slug="kb",
                name="kb",
                doc_form="text_model",
                indexing_technique="high_quality",
                embedding=CONFIG.model_dump(),
                process_rule={},
                retrieval_model={},
                data_policy={},
            )
        )
        await session.flush()
        session.add(
            KbDocument(
                id=document_id,
                workspace_id=workspace_id,
                kb_id=kb_id,
                position=1,
                name="큰 문서",
                data_source_type="paste_text",
                filename="a.txt",
                mime="text/plain",
                size_bytes=1,
                sha256="0" * 64,
                storage_key=key,
                process_rule={},
                extract=None,
                indexing_status="completed",
                batch="b",
                deleted_at=NOW,
            )
        )
    executor = KnowledgeIndexExecutor(
        db=db,
        settings=settings,
        runtime=runtime,
        storage=storage,
        sandbox=backend,
        llm=FakeAdapter(),
    )
    try:
        yield World(db, runtime, executor, storage, workspace_id, kb_id, document_id, key)
    finally:
        await runtime.close()
        await backend.aclose()
        await db.close()


def _write(path: Path) -> Path:
    path.write_bytes(b"original")
    return path


async def add_units(world: World, count: int) -> None:
    rows: list[KbSegment] = []
    vec: list[VecRow] = []
    fts: list[FtsRow] = []
    for position in range(1, count + 1):
        content = f"청크 {position}"
        rows.append(
            KbSegment(
                id=new_id(),
                workspace_id=world.workspace_id,
                kb_id=world.kb_id,
                document_id=world.document_id,
                position=position,
                unit_no=position,
                content=content,
                content_hash=content_hash(content),
                status="completed",
                index_state={"fts": "ok", "vector": "ok"},
            )
        )
        vec.append(
            VecRow(position, pack_vector(fake_embedding(content, 8)), doc_id_of(world.document_id))
        )
        fts.append(FtsRow(position, content))
    async with world.db.write() as session:
        session.add_all(rows)
    await world.runtime.vectors.ensure(world.kb_id, CONFIG, workspace_id=world.workspace_id)
    await world.runtime.fts.ensure(world.kb_id, workspace_id=world.workspace_id)
    await world.runtime.vectors.upsert(world.kb_id, vec)
    await world.runtime.fts.upsert(world.kb_id, fts)


async def count(world: World, model: Any) -> int:
    async with world.db.read() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.workspace_id == world.workspace_id)
            )
            or 0
        )


async def test_index_rows_go_first_in_batches_then_rows_then_file(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_units(world, 2500)
    order: list[str] = []
    original_drop = knowledge_runtime.drop_units
    original_rows = executor_module.delete_document_rows

    async def drop(runtime: Any, kb_id: Any, units: Any, config: Any) -> None:
        order.append(f"index:{len(units)}")
        await original_drop(runtime, kb_id, units, config)

    async def rows(db: Any, workspace_id: Any, document_id: Any) -> int:
        order.append("rows")
        return await original_rows(db, workspace_id, document_id)

    monkeypatch.setattr(executor_module, "drop_units", drop)
    monkeypatch.setattr(executor_module, "delete_document_rows", rows)

    done = await world.executor.delete_document(
        world.workspace_id, world.document_id, CancelSignal()
    )

    assert done
    assert order == ["index:1000", "index:1000", "index:500", "rows"]
    assert await world.runtime.vectors.count(world.kb_id) == 0
    assert await world.runtime.fts.count(world.kb_id) == 0
    assert (await count(world, KbSegment), await count(world, KbDocument)) == (0, 0)
    assert await world.storage.size(world.storage_key) is None
    # 지운 유닛의 벡터는 30일 캐시로 간다.
    cached = await world.runtime.cache.lookup(CONFIG.key(), [content_hash("청크 7")])
    assert list(cached) == [content_hash("청크 7")]


async def test_a_stopped_delete_picks_up_where_it_left_off(world: World) -> None:
    await add_units(world, 1200)
    cancel = CancelSignal()
    cancel.set("shutdown")

    assert not await world.executor.delete_document(world.workspace_id, world.document_id, cancel)
    # 색인 행은 지웠지만 행과 원본은 남았다. 다시 돌면 끝낸다.
    assert await world.runtime.vectors.count(world.kb_id) == 0
    assert await count(world, KbSegment) == 1200
    assert await world.executor.delete_document(
        world.workspace_id, world.document_id, CancelSignal()
    )
    assert await count(world, KbDocument) == 0


async def test_a_document_that_is_not_deleted_is_left_alone(world: World) -> None:
    await add_units(world, 3)
    async with world.db.write() as session:
        document = await session.get(KbDocument, world.document_id)
        assert document is not None
        document.deleted_at = None

    assert not await world.executor.delete_document(
        world.workspace_id, world.document_id, CancelSignal()
    )
    assert await world.runtime.vectors.count(world.kb_id) == 3
