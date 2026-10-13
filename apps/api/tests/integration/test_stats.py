"""사용량 집계 API(설계 6.2 GET /stats/usage). 합계가 런 목록의 run.usage 합과 같아야 한다."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ModelEvent, Usage

Events = Callable[[str], list[dict[str, Any]]]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


def answer(text: str, model: str, tokens_in: int, tokens_out: int) -> list[ModelEvent]:
    usage = Usage(input_tokens=tokens_in, output_tokens=tokens_out, reasoning_tokens=5)
    return [*fake_text(text), fake_end(usage=usage, model=model)]


async def send(client: AsyncClient, session_id: str, text: str) -> None:
    response = await client.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": text}
    )
    assert response.status_code == 200, response.text


async def set_prices(client: AsyncClient, prices: dict[str, dict[str, float]]) -> None:
    workspace = (await client.get("/api/console/workspace")).json()
    settings = {**workspace["settings"], "model_prices": prices}
    response = await client.patch("/api/console/workspace", json={"settings": settings})
    assert response.status_code == 200, response.text


async def usage(client: AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/api/console/stats/usage", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def run_usage_sum(client: AsyncClient) -> dict[str, Any]:
    runs = (await client.get("/api/console/runs", params={"limit": 200})).json()["items"]
    costs = [run["usage"]["cost_usd"] for run in runs if run["usage"]["cost_usd"] is not None]
    return {
        "runs": len(runs),
        "input_tokens": sum(run["usage"]["input_tokens"] for run in runs),
        "output_tokens": sum(run["usage"]["output_tokens"] for run in runs),
        "reasoning_tokens": sum(run["usage"]["reasoning_tokens"] for run in runs),
        "cost_usd": sum(costs),
    }


async def test_usage_is_grouped_and_matches_run_usage(
    logged_in: AsyncClient, make_session: Any, script: Any
) -> None:
    await set_prices(logged_in, {"gemini-a": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}})
    agent_a, session_a = await make_session(logged_in)
    agent_b, session_b = await make_session(logged_in, approval_mode="off")
    await logged_in.patch(f"/api/console/agents/{agent_b}", json={"name": "정리 담당"})

    script.add(answer("첫 답", "gemini-a", 1000, 200), answer("둘째 답", "gemini-b", 500, 50))
    await send(logged_in, session_a, "하나")
    await send(logged_in, session_a, "둘")
    # 스텝 둘짜리 런 하나. 런 usage는 두 스텝의 합이다.
    script.add(
        [
            *fake_tool_call("write_file", {"path": "outputs/a.txt", "content": "A"}),
            fake_end("tool_use", usage=Usage(input_tokens=300, output_tokens=30), model="gemini-a"),
        ],
        answer("썼다", "gemini-a", 400, 40),
    )
    await send(logged_in, session_b, "써")

    by_agent = await usage(logged_in, group_by="agent")
    rows = {row["key"]: row for row in by_agent["items"]}
    assert set(rows) == {agent_a, agent_b}
    assert (rows[agent_a]["runs"], rows[agent_a]["input_tokens"]) == (2, 1500)
    assert rows[agent_a]["output_tokens"] == 250
    assert rows[agent_a]["cost_usd"] == pytest.approx((1000 * 1 + 200 * 2) / 1e6)
    assert rows[agent_a]["unpriced_runs"] == 1
    assert rows[agent_b]["label"] == "정리 담당"
    assert (rows[agent_b]["runs"], rows[agent_b]["input_tokens"]) == (1, 700)
    assert rows[agent_b]["cost_usd"] == pytest.approx((700 * 1 + 70 * 2) / 1e6)
    # 토큰이 많은 에이전트가 먼저 온다.
    assert [row["key"] for row in by_agent["items"]] == [agent_a, agent_b]

    by_model = await usage(logged_in, group_by="model")
    models = {row["key"]: row for row in by_model["items"]}
    assert set(models) == {"gemini-a", "gemini-b"}
    assert (models["gemini-a"]["runs"], models["gemini-a"]["input_tokens"]) == (2, 1700)
    assert models["gemini-b"]["cost_usd"] is None
    assert models["gemini-b"]["unpriced_runs"] == 1

    by_day = await usage(logged_in, group_by="day", tz_offset_minutes=0)
    today = datetime.now(UTC).date().isoformat()
    assert [row["key"] for row in by_day["items"]] == [today]
    assert by_day["tz_offset_minutes"] == 0

    expected = await run_usage_sum(logged_in)
    for report in (by_agent, by_model, by_day):
        total = report["total"]
        assert total["runs"] == expected["runs"] == 3
        assert total["input_tokens"] == expected["input_tokens"] == 2200
        assert total["output_tokens"] == expected["output_tokens"] == 320
        assert total["reasoning_tokens"] == expected["reasoning_tokens"]
        assert total["cost_usd"] == pytest.approx(expected["cost_usd"])
        assert total["unpriced_runs"] == 1
        assert total["estimated"] is False


async def test_period_and_validation(
    logged_in: AsyncClient, make_session: Any, script: Any
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(answer("답", "gemini-a", 10, 2))
    await send(logged_in, session_id, "질문")
    now = datetime.now(UTC)

    default = await usage(logged_in)
    assert default["group_by"] == "day"
    span = datetime.fromisoformat(default["until"]) - datetime.fromisoformat(default["since"])
    assert span == timedelta(days=30)
    assert default["total"]["runs"] == 1

    later = await usage(
        logged_in,
        **{
            "from": (now + timedelta(hours=1)).isoformat(),
            "to": (now + timedelta(hours=2)).isoformat(),
        },
    )
    assert later["items"] == [] and later["total"]["runs"] == 0
    assert later["total"]["cost_usd"] is None

    # 날짜만 주면 tz_offset_minutes 시간대의 그날 0시다.
    yesterday = (now - timedelta(days=1)).date()
    tomorrow = (now + timedelta(days=1)).date()
    dated = await usage(
        logged_in,
        **{"from": yesterday.isoformat(), "to": tomorrow.isoformat()},
        tz_offset_minutes=0,
    )
    assert dated["total"]["runs"] == 1
    assert datetime.fromisoformat(dated["since"]) == datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=UTC
    )
    too_long = await logged_in.get(
        "/api/console/stats/usage", params={"from": "2000-01-01", "to": "2999-01-01"}
    )
    assert too_long.status_code == 422

    reversed_range = await logged_in.get(
        "/api/console/stats/usage", params={"from": now.isoformat(), "to": now.isoformat()}
    )
    assert reversed_range.status_code == 422
    assert reversed_range.json()["error"]["code"] == "invalid_range"
    bad_group = await logged_in.get("/api/console/stats/usage", params={"group_by": "week"})
    assert bad_group.status_code == 422
    bad_offset = await logged_in.get(
        "/api/console/stats/usage", params={"tz_offset_minutes": 24 * 60}
    )
    assert bad_offset.status_code == 422


async def test_usage_needs_login(client: AsyncClient, account: dict[str, Any]) -> None:
    response = await client.get("/api/console/stats/usage")

    assert response.status_code == 401
