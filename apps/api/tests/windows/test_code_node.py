"""code 노드의 Windows 상한(2단계 설계 9.1, 10장 M13).

실제 Job Object로 메모리 256MB·프로세스 8개 상한을 보고, 런이 도는 동안 하우스키핑이 런 작업 폴더
(DATA_DIR/wf-workspaces/<run_id>)를 지우지 않는지 본다. 기본 실행에서 빠지고 `uv run pytest -m
windows`로 돈다.
"""

import asyncio
import sys
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import WfRun, WfVersion, Workflow
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id
from app.housekeeping import Retention, WorkflowRetention
from app.runner.supervisor import CancelSignal
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.schemas.workflow import Graph
from app.storage.local_fs import LocalFsStorage
from app.workflow.engine import Engine, RunContext, RunOutcome, WfEvent
from app.workflow.frames import Checkpoint
from app.workflow.ports import NodeSettings

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object"),
]


@dataclass
class Ports:
    sandbox: LocalWorkspaceBackend
    model: None = None
    http: None = None
    files: None = None
    tasks: None = None
    node_settings: NodeSettings = NodeSettings()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.perf_counter()

    async def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        return None


@dataclass
class Env:
    root: Path
    settings: Settings
    sandbox: LocalWorkspaceBackend


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    settings = Settings.model_validate({"data_dir": tmp_path / "data"})
    sandbox = LocalWorkspaceBackend(settings, workspaces_dir=tmp_path / "data" / "wf-workspaces")
    yield Env(tmp_path, settings, sandbox)
    await sandbox.aclose()


def code_graph(code: str, outputs: list[str], timeout_sec: int | None = None) -> Graph:
    config: dict[str, Any] = {"code": code, "outputs": [{"name": n} for n in outputs]}
    if timeout_sec is not None:
        config["timeout_sec"] = timeout_sec
    return Graph.model_validate(
        {
            "nodes": [
                {"id": "start", "type": "start", "config": {"fields": []}},
                {"id": "code_1", "type": "code", "config": config},
                {
                    "id": "end_1",
                    "type": "end",
                    "config": {"outputs": [{"name": n, "var": f"code_1.{n}"} for n in outputs]},
                },
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "code_1"},
                {"id": "e2", "source": "code_1", "target": "end_1"},
            ],
        }
    )


async def run_graph(
    env: Env, graph: Graph, run_id: uuid.UUID | None = None
) -> tuple[RunOutcome, list[WfEvent]]:
    context = RunContext(
        run_id=run_id or uuid.uuid4(),
        graph=graph,
        kind="workflow",
        ports=Ports(env.sandbox),
        cancel=CancelSignal(),
    )
    events: list[WfEvent] = []
    run = Engine().run(context)
    async with run:
        async for event in run:
            events.append(event)
    return run.outcome, events


def code_record(events: list[WfEvent]) -> Any:
    return next(
        e.node_run
        for e in events
        if e.type == "node.completed" and e.data["node_id"] == "code_1" and e.node_run
    )


async def test_memory_over_256mb_ends_with_memory_limit(env: Env) -> None:
    ok, _ = await run_graph(
        env, code_graph("def main():\n    return {'n': len(bytearray(150 * 2**20))}\n", ["n"])
    )
    over, events = await run_graph(
        env, code_graph("def main():\n    return {'n': len(bytearray(300 * 2**20))}\n", ["n"])
    )

    assert ok.status == "succeeded" and ok.outputs == {"n": 150 * 2**20}
    assert over.error is not None and over.error.code == "memory_limit"
    assert code_record(events).process["peak_memory_mb"] is not None


async def test_process_count_is_capped_at_eight(env: Env) -> None:
    code = (
        "import subprocess, sys\n"
        "def main():\n"
        "    started = []\n"
        "    error = ''\n"
        "    try:\n"
        "        for _ in range(12):\n"
        "            started.append(subprocess.Popen(\n"
        "                [sys.executable, '-c', 'import time; time.sleep(5)']))\n"
        "    except OSError as exc:\n"
        "        error = str(exc)\n"
        "    finally:\n"
        "        for proc in started:\n"
        "            proc.kill()\n"
        "    return {'started': len(started), 'error': error}\n"
    )

    outcome, _ = await run_graph(env, code_graph(code, ["started", "error"]))

    assert outcome.status == "succeeded"
    # 래퍼 프로세스 자신이 한 자리를 쓴다.
    assert outcome.outputs["started"] <= 7 and outcome.outputs["error"]


async def test_timeout_kills_the_whole_process_tree(env: Env) -> None:
    code = (
        "import subprocess, sys, time\n"
        "def main():\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "    time.sleep(60)\n"
        "    return {}\n"
    )
    started = time.monotonic()

    outcome, events = await run_graph(env, code_graph(code, [], timeout_sec=2))

    assert outcome.error is not None and outcome.error.code == "timeout"
    assert time.monotonic() - started < 20
    assert code_record(events).process["timed_out"] is True


async def test_output_limit_and_non_dict_returns(env: Env) -> None:
    big, _ = await run_graph(
        env, code_graph("def main():\n    return {'v': 'x' * 300_000}\n", ["v"])
    )
    listed, _ = await run_graph(env, code_graph("def main():\n    return ['v']\n", ["v"]))

    assert big.error is not None and big.error.code == "output_too_large"
    assert listed.error is not None and listed.error.code == "invalid_output"


async def seed_running_run(db: Database) -> uuid.UUID:
    workspace_id, workflow_id, version_id, run_id = new_id(), new_id(), new_id(), new_id()
    async with db.write() as session:
        workspace = WorkspaceRepository(session).add(name="w", slug="w", settings={})
        workspace.id = workspace_id
        await session.flush()
        session.add(
            Workflow(
                id=workflow_id, workspace_id=workspace_id, slug="wf", name="wf", kind="workflow"
            )
        )
        await session.flush()
        session.add(
            WfVersion(
                id=version_id,
                workspace_id=workspace_id,
                workflow_id=workflow_id,
                status="draft",
                graph_hash="0" * 64,
            )
        )
        await session.flush()
        session.add(
            WfRun(
                id=run_id,
                workspace_id=workspace_id,
                workflow_id=workflow_id,
                wf_version_id=version_id,
                kind="workflow",
                trigger="preview",
                status="running",
                graph_hash="0" * 64,
            )
        )
    return run_id


async def test_housekeeping_during_a_run_keeps_the_run_folder(env: Env) -> None:
    db = Database(env.root / "app.db")
    await upgrade_head(db)
    try:
        run_id = await seed_running_run(db)
        storage = LocalFsStorage(env.settings.data_dir / "storage")
        workflows = WorkflowRetention(db, storage=storage, backend=env.sandbox, retention_days=90)
        agent_backend = LocalWorkspaceBackend(env.settings)
        agents = Retention(db, storage=storage, backend=agent_backend)
        orphan = env.sandbox.workspaces_dir / str(uuid.uuid4())
        orphan.mkdir(parents=True)
        code = (
            "import time\n"
            "def main():\n"
            "    time.sleep(3)\n"
            "    with open('outputs/done.txt', 'w') as file:\n"
            "        file.write('ok')\n"
            "    return {'ok': True}\n"
        )
        task = asyncio.create_task(run_graph(env, code_graph(code, ["ok"]), run_id))
        folder = env.sandbox.workspaces_dir / str(run_id)
        deadline = time.monotonic() + 10
        while not folder.is_dir() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)

        removed = await workflows.purge_workspaces()
        await agents.purge_orphan_workspaces()
        outcome, _ = await task

        assert removed == 1 and not orphan.exists()
        assert outcome.status == "succeeded" and outcome.outputs == {"ok": True}
        assert (folder / "outputs" / "done.txt").read_text() == "ok"
        await agent_backend.aclose()
    finally:
        await db.close()
