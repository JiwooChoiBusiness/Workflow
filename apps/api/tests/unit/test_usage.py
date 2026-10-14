"""사용량 집계(services/usage.py, repositories/usage.py): 날짜 경계, 모델, 비용 합."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import (
    Agent,
    AgentVersion,
    ChatSession,
    Message,
    Run,
    WfNodeRun,
    WfRun,
    WfVersion,
    Workflow,
)
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id
from app.schemas.usage import UsageTotals
from app.services.errors import ServiceError
from app.services.usage import UsageService, sum_totals

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "usage.db")
    await upgrade_head(database)
    yield database
    await database.close()


class Seed:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.workspace_id = new_id()
        self.agents: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
        self.sessions: dict[str, uuid.UUID] = {}
        self.seq = 0

    async def setup(self, *names: str) -> None:
        async with self.db.write() as session:
            workspace = WorkspaceRepository(session).add(name="w", slug="w", settings={})
            workspace.id = self.workspace_id
            await session.flush()
            for name in names:
                agent = Agent(id=new_id(), workspace_id=self.workspace_id, slug=name, name=name)
                session.add(agent)
                await session.flush()
                version = AgentVersion(
                    id=new_id(),
                    workspace_id=self.workspace_id,
                    agent_id=agent.id,
                    status="draft",
                    approval_mode="all_shell",
                )
                session.add(version)
                await session.flush()
                chat = ChatSession(
                    id=new_id(),
                    workspace_id=self.workspace_id,
                    agent_id=agent.id,
                    agent_version_id=version.id,
                    source="preview",
                )
                session.add(chat)
                self.agents[name] = (agent.id, version.id)
                self.sessions[name] = chat.id

    async def run(
        self,
        agent: str,
        queued_at: datetime,
        *,
        usage: dict[str, Any],
        model: dict[str, Any] | None = None,
    ) -> None:
        _, version_id = self.agents[agent]
        session_id = self.sessions[agent]
        self.seq += 1
        async with self.db.write() as session:
            message = Message(
                id=new_id(),
                workspace_id=self.workspace_id,
                session_id=session_id,
                seq=self.seq,
                role="user",
            )
            session.add(message)
            await session.flush()
            session.add(
                Run(
                    id=new_id(),
                    workspace_id=self.workspace_id,
                    session_id=session_id,
                    agent_version_id=version_id,
                    trigger="chat",
                    status="succeeded",
                    user_message_id=message.id,
                    model=model or {},
                    approval_mode="all_shell",
                    usage=usage,
                    queued_at=queued_at,
                )
            )


def tokens(inp: int, out: int, **extra: Any) -> dict[str, Any]:
    return {"input_tokens": inp, "output_tokens": out, **extra}


async def test_days_follow_the_requested_time_zone(db: Database) -> None:
    seed = Seed(db)
    await seed.setup("a")
    # UTC 14:59:59.9은 서울(+9) 23:59:59.9로 아직 20일, 15:00은 21일이다.
    await seed.run("a", datetime(2026, 10, 20, 14, 59, 59, 900_000, tzinfo=UTC), usage=tokens(1, 1))
    await seed.run("a", datetime(2026, 10, 20, 15, 0, tzinfo=UTC), usage=tokens(10, 1))
    await seed.run("a", datetime(2026, 10, 19, 23, 0, tzinfo=UTC), usage=tokens(100, 1))
    service = UsageService(db, clock=lambda: NOW)

    seoul = await service.report(
        seed.workspace_id,
        since=datetime(2026, 10, 1, tzinfo=UTC),
        until=datetime(2026, 10, 30, tzinfo=UTC),
        tz_offset_minutes=540,
    )
    utc = await service.report(
        seed.workspace_id,
        since=datetime(2026, 10, 1, tzinfo=UTC),
        until=datetime(2026, 10, 30, tzinfo=UTC),
        tz_offset_minutes=0,
    )

    assert [(row.key, row.input_tokens) for row in seoul.items] == [
        ("2026-10-20", 101),
        ("2026-10-21", 10),
    ]
    assert [(row.key, row.input_tokens) for row in utc.items] == [
        ("2026-10-19", 100),
        ("2026-10-20", 11),
    ]
    assert seoul.total.input_tokens == utc.total.input_tokens == 111


async def test_models_costs_and_estimates(db: Database) -> None:
    seed = Seed(db)
    await seed.setup("a", "b")
    at = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
    await seed.run(
        "a",
        at,
        usage=tokens(1000, 100, cost_usd=0.5, reasoning_tokens=7, cached_input_tokens=3),
        model={"model_id": "m-1", "used": "m-2", "fallback_used": True},
    )
    await seed.run("a", at, usage=tokens(10, 1, estimated=True), model={"model_id": "m-1"})
    # 모델을 부르기 전에 끝난 런: 토큰도 모델도 없다.
    await seed.run("b", at, usage={}, model={"model_id": None})
    await seed.run("b", at, usage=tokens(5, 5, cost_usd=0.25), model={"used": "m-2"})
    service = UsageService(db, clock=lambda: NOW)

    by_model = await service.report(seed.workspace_id, group_by="model", tz_offset_minutes=0)
    rows = {row.key: row for row in by_model.items}

    # 폴백으로 바뀐 런은 마지막으로 쓴 모델(used)에 들어간다.
    assert set(rows) == {"m-2", "m-1", None}
    assert (rows["m-2"].runs, rows["m-2"].input_tokens, rows["m-2"].cost_usd) == (2, 1005, 0.75)
    assert (rows["m-2"].reasoning_tokens, rows["m-2"].cached_input_tokens) == (7, 3)
    assert rows["m-1"].estimated is True and rows["m-1"].unpriced_runs == 1
    assert rows["m-1"].cost_usd is None
    assert (rows[None].runs, rows[None].input_tokens, rows[None].unpriced_runs) == (1, 0, 0)
    assert [row.key for row in by_model.items][0] == "m-2"

    by_agent = await service.report(seed.workspace_id, group_by="agent", tz_offset_minutes=0)
    agents = {row.label: row for row in by_agent.items}
    assert agents["a"].key == str(seed.agents["a"][0])
    assert (agents["a"].runs, agents["b"].runs) == (2, 2)
    assert agents["b"].cost_usd == 0.25

    for report in (by_model, by_agent):
        assert report.total.runs == 4
        assert report.total.input_tokens == 1015
        assert report.total.cost_usd == 0.75
        assert report.total.estimated is True


async def test_range_defaults_and_dates(db: Database) -> None:
    seed = Seed(db)
    await seed.setup("a")
    service = UsageService(db, clock=lambda: NOW, local_offset=lambda: 540)

    default = await service.report(seed.workspace_id)
    assert default.until == NOW and (default.until - default.since).days == 30
    assert default.tz_offset_minutes == 540

    dated = await service.report(
        seed.workspace_id, since=date(2026, 10, 1), until=date(2026, 10, 2)
    )
    # 날짜는 집계 시간대(+9)의 0시다.
    assert dated.since == datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
    assert dated.until == datetime(2026, 10, 1, 15, 0, tzinfo=UTC)

    with pytest.raises(ServiceError) as info:
        await service.report(seed.workspace_id, since=NOW, until=NOW)
    assert info.value.code == "invalid_range"


def test_sum_totals_keeps_cost_null_until_some_run_has_one() -> None:
    assert sum_totals([]) == UsageTotals()
    rows = [UsageTotals(runs=1, input_tokens=3), UsageTotals(runs=2, cost_usd=None, estimated=True)]
    assert sum_totals(rows).cost_usd is None
    assert sum_totals(rows).estimated is True
    rows.append(UsageTotals(runs=1, cost_usd=0.1, unpriced_runs=0))
    total = sum_totals(rows)
    assert (total.runs, total.input_tokens, total.cost_usd) == (4, 3, 0.1)


class WfSeed:
    """워크플로우 런과 모델 노드 런. 노드 런 순서대로 process.model이 models다."""

    def __init__(self, seed: Seed) -> None:
        self.seed = seed
        self.workflows: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}

    async def workflow(self, name: str, *, deleted: bool = False) -> uuid.UUID:
        workspace_id = self.seed.workspace_id
        async with self.seed.db.write() as session:
            workflow = Workflow(
                id=new_id(),
                workspace_id=workspace_id,
                slug=name,
                name=name,
                kind="workflow",
                deleted_at=NOW if deleted else None,
            )
            session.add(workflow)
            await session.flush()
            version = WfVersion(
                id=new_id(),
                workspace_id=workspace_id,
                workflow_id=workflow.id,
                status="draft",
                graph_hash="0" * 64,
            )
            session.add(version)
            self.workflows[name] = (workflow.id, version.id)
            return workflow.id

    async def run(
        self,
        name: str,
        queued_at: datetime,
        *,
        usage: dict[str, Any],
        models: list[str | None],
    ) -> None:
        workflow_id, version_id = self.workflows[name]
        workspace_id = self.seed.workspace_id
        async with self.seed.db.write() as session:
            run = WfRun(
                id=new_id(),
                workspace_id=workspace_id,
                workflow_id=workflow_id,
                wf_version_id=version_id,
                kind="workflow",
                trigger="preview",
                status="succeeded",
                usage=usage,
                graph_hash="0" * 64,
                queued_at=queued_at,
            )
            session.add(run)
            await session.flush()
            for index, model in enumerate(models):
                session.add(
                    WfNodeRun(
                        id=new_id(),
                        workspace_id=workspace_id,
                        wf_run_id=run.id,
                        node_id=f"llm_{index}",
                        node_type="llm",
                        index=index,
                        status="succeeded",
                        process={"model": model} if model else {},
                        started_at=queued_at,
                    )
                )
            # agent 노드 런은 자식 런의 사용량을 보여 주기만 하고 모델 집계에 들어가지 않는다.
            session.add(
                WfNodeRun(
                    id=new_id(),
                    workspace_id=workspace_id,
                    wf_run_id=run.id,
                    node_id="agent_1",
                    node_type="agent",
                    index=len(models),
                    status="succeeded",
                    process={"model": "child-model"},
                    started_at=queued_at,
                )
            )


async def test_workflow_runs_join_every_grouping_with_the_same_total(db: Database) -> None:
    seed = Seed(db)
    await seed.setup("a")
    flows = WfSeed(seed)
    first = await flows.workflow("분류")
    gone = await flows.workflow("옛 흐름", deleted=True)
    at = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
    await seed.run("a", at, usage=tokens(100, 10), model={"used": "m-1"})
    await flows.run("분류", at, usage=tokens(40, 4, cost_usd=0.1), models=["m-1", "m-2"])
    await flows.run(
        "분류", datetime(2026, 10, 19, 1, 0, tzinfo=UTC), usage=tokens(5, 1), models=["m-1", None]
    )
    await flows.run("옛 흐름", at, usage={}, models=[])
    service = UsageService(db, clock=lambda: NOW)

    by_day = await service.report(seed.workspace_id, group_by="day", tz_offset_minutes=0)
    by_model = await service.report(seed.workspace_id, group_by="model", tz_offset_minutes=0)
    by_agent = await service.report(seed.workspace_id, group_by="agent", tz_offset_minutes=0)
    by_flow = await service.report(seed.workspace_id, group_by="workflow", tz_offset_minutes=0)

    assert [(r.key, r.runs, r.input_tokens) for r in by_day.items] == [
        ("2026-10-19", 1, 5),
        ("2026-10-20", 3, 140),
    ]
    models = {row.key: (row.runs, row.input_tokens) for row in by_model.items}
    # 워크플로우 런은 마지막으로 모델을 부른 노드의 모델로 센다. agent 노드는 보지 않는다.
    assert models == {"m-1": (2, 105), "m-2": (1, 40), None: (1, 0)}
    agents = [(row.key, row.label, row.runs, row.input_tokens) for row in by_agent.items]
    assert agents == [
        (str(seed.agents["a"][0]), "a", 1, 100),
        (None, None, 3, 45),
    ]
    flows_rows = [(r.key, r.label, r.deleted, r.runs, r.input_tokens) for r in by_flow.items]
    assert flows_rows == [
        (None, None, False, 1, 100),
        (str(first), "분류", False, 2, 45),
        (str(gone), "옛 흐름", True, 1, 0),
    ]
    for report in (by_day, by_model, by_agent, by_flow):
        assert report.total.runs == 4
        assert report.total.input_tokens == 145
        assert report.total.cost_usd == 0.1
        assert report.total.unpriced_runs == 2
