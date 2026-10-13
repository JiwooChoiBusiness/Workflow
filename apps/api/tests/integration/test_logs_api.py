"""로그 화면의 런·세션 목록 필터(설계 6.2 GET /runs, GET /sessions, 7.2 /logs)."""

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update

from app.config import Settings
from app.db.engine import Database
from app.db.models import ChatSession
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ErrorEvent

FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


async def send(client: AsyncClient, session_id: str, text: str) -> str:
    response = await client.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": text}
    )
    assert response.status_code == 200, response.text
    return response.headers["X-Run-Id"]


async def run_ids(client: AsyncClient, **params: Any) -> list[str]:
    response = await client.get("/api/console/runs", params=params)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def test_run_list_filters_and_session_context(
    logged_in: AsyncClient, make_session: Any, script: Any, db: Database
) -> None:
    agent_a, session_a = await make_session(logged_in)
    agent_b, session_b = await make_session(logged_in)
    await logged_in.patch(f"/api/console/agents/{agent_b}", json={"name": "API 담당"})
    # 서비스 API 세션처럼 보이게 소스만 바꾼다.
    async with db.write() as session:
        await session.execute(
            update(ChatSession)
            .where(ChatSession.id == uuid.UUID(session_b))
            .values(source="api", external_user="user-7")
        )
    script.add(
        fake_text("첫 답"),
        [ErrorEvent(code="model_request_invalid", message="잘못된 요청", retryable=False)],
        fake_text("API 답"),
    )
    ok_run = await send(logged_in, session_a, "첫 질문")
    failed_run = await send(logged_in, session_a, "두 번째")
    api_run = await send(logged_in, session_b, "API 질문")

    assert await run_ids(logged_in) == [api_run, failed_run, ok_run]
    assert await run_ids(logged_in, source="api") == [api_run]
    assert await run_ids(logged_in, source="preview") == [failed_run, ok_run]
    assert await run_ids(logged_in, agent_id=agent_a) == [failed_run, ok_run]
    assert await run_ids(logged_in, status="failed") == [failed_run]
    assert await run_ids(logged_in, agent_id=agent_a, status="succeeded") == [ok_run]
    assert await run_ids(logged_in, session_id=session_b) == [api_run]

    [item] = (await logged_in.get("/api/console/runs", params={"source": "api"})).json()["items"]
    assert item["agent_id"] == agent_b and item["agent_name"] == "API 담당"
    assert (item["session_source"], item["session_title"]) == ("api", "API 질문")
    assert item["session_deleted"] is False and item["agent_deleted"] is False
    assert item["usage"]["input_tokens"] > 0

    page = (await logged_in.get("/api/console/runs", params={"limit": 2})).json()
    assert len(page["items"]) == 2 and page["next_offset"] == 2
    rest = (await logged_in.get("/api/console/runs", params={"limit": 2, "offset": 2})).json()
    assert [entry["id"] for entry in rest["items"]] == [ok_run] and rest["next_offset"] is None

    sessions = (await logged_in.get("/api/console/sessions", params={"source": "api"})).json()
    assert [entry["id"] for entry in sessions["items"]] == [session_b]
    by_user = (
        await logged_in.get("/api/console/sessions", params={"external_user": "user-7"})
    ).json()
    assert [entry["id"] for entry in by_user["items"]] == [session_b]


async def test_period_filters_accept_times_without_zone(
    logged_in: AsyncClient, make_session: Any, script: Any
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(fake_text("답"))
    run_id = await send(logged_in, session_id, "질문")
    # 시간대 없는 시각은 서버 PC 시간대로 본다. 지금 기준 앞뒤 한 시간이면 반드시 들어간다.
    local_now = datetime.now()
    before = (local_now - timedelta(hours=1)).isoformat(timespec="seconds")
    after = (local_now + timedelta(hours=1)).isoformat(timespec="seconds")

    assert await run_ids(logged_in, since=before, until=after) == [run_id]
    assert await run_ids(logged_in, since=after) == []
    sessions = await logged_in.get("/api/console/sessions", params={"since": before})
    assert sessions.status_code == 200
    assert [entry["id"] for entry in sessions.json()["items"]] == [session_id]
    empty = await logged_in.get("/api/console/sessions", params={"until": before})
    assert empty.json()["items"] == []


async def test_runs_of_deleted_sessions_stay_in_the_log(
    logged_in: AsyncClient, make_session: Any, script: Any
) -> None:
    agent_id, session_id = await make_session(logged_in)
    script.add(fake_text("남는다"))
    run_id = await send(logged_in, session_id, "기록")

    deleted = await logged_in.delete(f"/api/console/sessions/{session_id}", headers=FETCH)
    assert deleted.status_code == 204

    items = (await logged_in.get("/api/console/runs", params={"agent_id": agent_id})).json()[
        "items"
    ]
    assert [item["id"] for item in items] == [run_id]
    assert items[0]["session_deleted"] is True
    # 런 상세와 영속 이벤트는 세션을 지운 뒤에도 볼 수 있다. 세션 목록에서는 빠진다.
    assert (await logged_in.get(f"/api/console/runs/{run_id}")).status_code == 200
    events = (await logged_in.get(f"/api/console/runs/{run_id}/events")).json()["items"]
    assert events[-1]["type"] == "run.completed"
    sessions = (await logged_in.get("/api/console/sessions")).json()["items"]
    assert session_id not in [item["id"] for item in sessions]


async def test_output_copies_use_the_short_storage_key(
    logged_in: AsyncClient, make_session: Any, script: Any, settings: Settings
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(
        [
            *fake_tool_call("write_file", {"path": "outputs/깊은/결과.txt", "content": "끝"}),
            fake_end("tool_use"),
        ],
        fake_text("남겼다"),
    )
    run_id = await send(logged_in, session_id, "결과를 남겨")

    stored = settings.data_dir / "storage" / "session-files"
    # session-files/<런>/<작업공간 상대 경로>. 세션 id 폴더는 없다(12.2 M6 이후 확정).
    assert (stored / run_id / "outputs" / "깊은" / "결과.txt").read_text(encoding="utf-8") == "끝"
    assert not (stored / session_id).exists()
    files = (await logged_in.get(f"/api/console/sessions/{session_id}/files")).json()["items"]
    [output] = [item for item in files if item["kind"] == "output"]
    download = await logged_in.get(
        f"/api/console/sessions/{session_id}/files/{output['id']}/download"
    )
    assert download.status_code == 200 and download.content.decode() == "끝"
