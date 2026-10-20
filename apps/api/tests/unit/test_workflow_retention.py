"""워크플로우 보존 정리(housekeeping.WorkflowRetention, 2단계 설계 4.4)."""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import (
    WfConversation,
    WfFile,
    WfMessage,
    WfNodeRun,
    WfRun,
    WfRunEvent,
    WfVersion,
    Workflow,
)
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.housekeeping import Housekeeping, WorkflowRetention
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.storage.local_fs import LocalFsStorage

NOW = utcnow()


class Seed:
    def __init__(self, db: Database, storage: LocalFsStorage) -> None:
        self.db = db
        self.storage = storage
        self.workspace_id = new_id()
        self.workflow_id = new_id()
        self.version_id = new_id()

    async def setup(self) -> None:
        async with self.db.write() as session:
            workspace = WorkspaceRepository(session).add(name="w", slug="w", settings={})
            workspace.id = self.workspace_id
            await session.flush()
            session.add(
                Workflow(
                    id=self.workflow_id,
                    workspace_id=self.workspace_id,
                    slug="wf",
                    name="wf",
                    kind="workflow",
                )
            )
            await session.flush()
            session.add(
                WfVersion(
                    id=self.version_id,
                    workspace_id=self.workspace_id,
                    workflow_id=self.workflow_id,
                    status="draft",
                    graph_hash="0" * 64,
                )
            )

    async def run(
        self,
        status: str,
        *,
        ended: datetime | None,
        trigger: str = "preview",
        state: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> uuid.UUID:
        run_id = new_id()
        async with self.db.write() as session:
            session.add(
                WfRun(
                    id=run_id,
                    workspace_id=self.workspace_id,
                    workflow_id=self.workflow_id,
                    wf_version_id=self.version_id,
                    kind="workflow",
                    trigger=trigger,
                    status=status,
                    state=state,
                    inputs=inputs or {},
                    graph_hash="0" * 64,
                    ended_at=ended,
                )
            )
            await session.flush()
            for index, iteration in enumerate((None, "iter_1")):
                session.add(
                    WfNodeRun(
                        id=new_id(),
                        workspace_id=self.workspace_id,
                        wf_run_id=run_id,
                        node_id=f"n{index}",
                        node_type="template",
                        index=index,
                        iteration_node_id=iteration,
                        iteration_index=0 if iteration else None,
                        status="succeeded",
                        inputs={"a": 1},
                        outputs={"b": 2},
                        started_at=NOW,
                    )
                )
            session.add(
                WfRunEvent(
                    workspace_id=self.workspace_id,
                    wf_run_id=run_id,
                    seq=1,
                    type="workflow.started",
                    payload={},
                )
            )
        return run_id

    async def output_file(self, run_id: uuid.UUID) -> tuple[uuid.UUID, str]:
        file_id = new_id()
        key = f"wf-files/{self.workspace_id}/{file_id}"
        path = self.storage.local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        async with self.db.write() as session:
            session.add(
                WfFile(
                    id=file_id,
                    workspace_id=self.workspace_id,
                    kind="output",
                    workflow_id=self.workflow_id,
                    wf_run_id=run_id,
                    filename="out.txt",
                    mime="text/plain",
                    size_bytes=1,
                    sha256="0" * 64,
                    storage_key=key,
                )
            )
        return file_id, key

    async def upload(
        self, *, age: timedelta, conversation_id: uuid.UUID | None = None
    ) -> tuple[uuid.UUID, str]:
        file_id = new_id()
        key = f"wf-files/{self.workspace_id}/{file_id}"
        path = self.storage.local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"u")
        async with self.db.write() as session:
            session.add(
                WfFile(
                    id=file_id,
                    workspace_id=self.workspace_id,
                    kind="upload",
                    workflow_id=self.workflow_id,
                    conversation_id=conversation_id,
                    filename="in.txt",
                    mime="text/plain",
                    size_bytes=1,
                    sha256="0" * 64,
                    storage_key=key,
                    created_at=NOW - age,
                )
            )
        return file_id, key

    async def conversation(
        self, inputs: list[dict[str, Any]], *, deleted: bool = False
    ) -> uuid.UUID:
        """user 메시지 inputs를 차례로 단 대화. 마지막 것이 다음 턴에 다시 쓰인다."""
        conversation_id = new_id()
        async with self.db.write() as session:
            session.add(
                WfConversation(
                    id=conversation_id,
                    workspace_id=self.workspace_id,
                    workflow_id=self.workflow_id,
                    wf_version_id=self.version_id,
                    source="preview",
                    deleted_at=NOW if deleted else None,
                )
            )
            await session.flush()
            for seq, values in enumerate(inputs, start=1):
                session.add(
                    WfMessage(
                        id=new_id(),
                        workspace_id=self.workspace_id,
                        conversation_id=conversation_id,
                        seq=seq * 2 - 1,
                        role="user",
                        text="q",
                        inputs=values,
                        files=[],
                    )
                )
        return conversation_id

    async def count(self, model: Any, run_id: uuid.UUID) -> int:
        column = model.wf_run_id if model is not WfRun else model.id
        async with self.db.read() as session:
            return await session.scalar(select(func.count()).where(column == run_id)) or 0


@pytest.fixture
async def parts(tmp_path: Path) -> AsyncIterator[tuple[Seed, WorkflowRetention, Path]]:
    db = Database(tmp_path / "keep.db")
    await upgrade_head(db)
    storage = LocalFsStorage(tmp_path / "storage")
    folder = tmp_path / "wf-workspaces"
    backend = LocalWorkspaceBackend(
        Settings.model_validate({"data_dir": tmp_path}), workspaces_dir=folder
    )
    seed = Seed(db, storage)
    await seed.setup()
    retention = WorkflowRetention(
        db, storage=storage, backend=backend, retention_days=90, clock=lambda: NOW
    )
    try:
        yield seed, retention, folder
    finally:
        await backend.aclose()
        await db.close()


async def test_old_events_go_and_iteration_records_are_emptied(
    parts: tuple[Seed, WorkflowRetention, Path],
) -> None:
    seed, retention, _ = parts
    old = await seed.run("succeeded", ended=NOW - timedelta(days=91))
    recent = await seed.run("succeeded", ended=NOW - timedelta(days=1))

    assert await retention.purge_events() == 1

    assert await seed.count(WfRunEvent, old) == 0 and await seed.count(WfRunEvent, recent) == 1
    async with seed.db.read() as session:
        rows = (await session.scalars(select(WfNodeRun).where(WfNodeRun.wf_run_id == old))).all()
    by_iteration = {row.iteration_node_id: row for row in rows}
    assert by_iteration[None].outputs == {"b": 2}
    assert by_iteration["iter_1"].inputs is None and by_iteration["iter_1"].outputs is None


async def test_failed_run_checkpoints_are_dropped_after_a_week(
    parts: tuple[Seed, WorkflowRetention, Path],
) -> None:
    seed, retention, _ = parts
    old = await seed.run("failed", ended=NOW - timedelta(days=8), state={"v": 1})
    recent = await seed.run("failed", ended=NOW - timedelta(days=2), state={"v": 1})
    waiting = await seed.run("waiting_input", ended=None, state={"v": 1})

    assert await retention.clear_states() == 1

    async with seed.db.read() as session:
        states = {row.id: row.state for row in (await session.scalars(select(WfRun))).all()}
    assert states[old] is None and states[recent] == {"v": 1} and states[waiting] == {"v": 1}


async def test_single_node_runs_and_old_output_files_are_deleted(
    parts: tuple[Seed, WorkflowRetention, Path],
) -> None:
    seed, retention, _ = parts
    single = await seed.run("succeeded", ended=NOW - timedelta(days=8), trigger="single_node")
    kept = await seed.run("succeeded", ended=NOW - timedelta(days=8))
    _, single_key = await seed.output_file(single)
    old_file, old_key = await seed.output_file(kept)

    assert await retention.purge_single_runs() == 1
    assert await retention.purge_output_files() == 0

    for model in (WfRun, WfNodeRun, WfRunEvent, WfFile):
        assert await seed.count(model, single) == 0
    assert await seed.count(WfRun, kept) == 1
    assert not seed.storage.local_path(single_key).exists()
    later = WorkflowRetention(
        seed.db,
        storage=seed.storage,
        backend=retention._backend,  # pyright: ignore[reportPrivateUsage]
        retention_days=90,
        clock=lambda: NOW + timedelta(days=90),
    )
    assert await later.purge_output_files() == 1
    assert await seed.count(WfFile, kept) == 0
    assert not seed.storage.local_path(old_key).exists() and old_file


async def test_run_folders_are_kept_only_while_needed(
    parts: tuple[Seed, WorkflowRetention, Path],
) -> None:
    seed, retention, folder = parts
    ended_long_ago = await seed.run("failed", ended=NOW - timedelta(hours=2))
    just_ended = await seed.run("failed", ended=NOW - timedelta(minutes=5))
    waiting = await seed.run("waiting_input", ended=None)
    unknown = new_id()
    for run_id in (ended_long_ago, just_ended, waiting, unknown):
        (folder / str(run_id) / ".run").mkdir(parents=True)
    (folder / "not-a-run").mkdir()

    assert await retention.purge_workspaces() == 2

    left = sorted(path.name for path in folder.iterdir())
    assert left == sorted([str(just_ended), str(waiting), "not-a-run"])


async def test_housekeeping_reports_workflow_jobs(
    parts: tuple[Seed, WorkflowRetention, Path], tmp_path: Path
) -> None:
    seed, retention, _ = parts
    backend = LocalWorkspaceBackend(Settings.model_validate({"data_dir": tmp_path}))
    housekeeping = Housekeeping(
        db=seed.db, backend=backend, memory_budget_mb=300, workflows=retention
    )
    try:
        summary = await housekeeping.run_hourly()
    finally:
        await backend.aclose()

    for name in (
        "wf_run_events",
        "wf_states",
        "wf_single_runs",
        "wf_output_files",
        "wf_workspaces",
        "wf_loose_uploads",
    ):
        assert summary[f"purged_{name}"] == 0


def ref(file_id: uuid.UUID) -> dict[str, Any]:
    return {
        "file_id": str(file_id),
        "filename": "in.txt",
        "mime": "text/plain",
        "size_bytes": 1,
        "storage_key": "k",
        "source": "wf_file",
    }


async def test_loose_uploads_go_after_a_week_unless_something_still_reads_them(
    parts: tuple[Seed, WorkflowRetention, Path],
) -> None:
    seed, retention, _ = parts
    week = timedelta(days=8)
    unused, unused_key = await seed.upload(age=week)
    fresh, _ = await seed.upload(age=timedelta(days=2))
    attached, _ = await seed.upload(age=week, conversation_id=new_id())
    waiting_input, _ = await seed.upload(age=week)
    finished_input, finished_key = await seed.upload(age=week)
    next_turn, _ = await seed.upload(age=week)
    earlier_turn, _ = await seed.upload(age=week)
    deleted_chat, _ = await seed.upload(age=week)
    await seed.run("waiting_input", ended=None, inputs={"doc": ref(waiting_input)})
    await seed.run("succeeded", ended=NOW - week, inputs={"doc": ref(finished_input)})
    await seed.conversation([{"doc": ref(earlier_turn)}, {"doc": ref(next_turn)}])
    await seed.conversation([{"doc": ref(deleted_chat)}], deleted=True)
    output, _ = await seed.output_file(await seed.run("succeeded", ended=NOW - week))
    # 끝나지 않은 런과 대화의 다음 턴이 읽을 파일을 남겨도 다음 묶음으로 넘어간다.
    small = WorkflowRetention(
        seed.db,
        storage=seed.storage,
        backend=retention._backend,  # pyright: ignore[reportPrivateUsage]
        retention_days=90,
        clock=lambda: NOW,
        batch_size=2,
    )

    assert await small.purge_loose_uploads() == 4

    async with seed.db.read() as session:
        left = set((await session.scalars(select(WfFile.id))).all())
    assert left == {fresh, attached, waiting_input, next_turn, output}
    assert not seed.storage.local_path(unused_key).exists()
    assert not seed.storage.local_path(finished_key).exists()
    assert unused not in left and earlier_turn not in left and deleted_chat not in left
    assert await retention.purge_loose_uploads() == 0
