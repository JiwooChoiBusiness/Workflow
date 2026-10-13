"""사용량 집계(services/usage.py, repositories/usage.py): 날짜 경계, 모델, 비용 합."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from app.db.engine import Database
from app.db.migrate import upgrade_head
from app.db.models import Agent, AgentVersion, ChatSession, Message, Run
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
