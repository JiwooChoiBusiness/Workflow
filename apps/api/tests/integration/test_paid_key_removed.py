"""런을 만든 뒤 유료 키가 빠진 경우(3단계 설계 14.4, M24 검토).

API 쪽 설정(app.state.settings)에는 유료 키가 있고 실행기는 키 없이 떴다. 런 만들기는 통과하지만
실행기가 런을 잡을 때 paid_key_missing으로 실패시키고 무료 키로 넘어가지 않는다. 워크플로우 실행기와
에이전트 런 실행기를 둘 다 본다.
"""

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr

from app.config import Settings

DRAFT_FIELDS = (
    "instructions",
    "model",
    "tools",
    "limits",
    "sandbox",
    "approval_mode",
    "skill_version_ids",
    "file_ids",
    "sub_agents",
    "updated_at",
)


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    # 실행기가 받는 설정. 유료 키가 없다.
    return short_settings.model_copy(update={"llm_fake": True, "kb_embed_rpm": 6000})


@pytest.fixture
def app(scripted_app: FastAPI, settings: Settings) -> FastAPI:
    # API는 키가 있는 설정을 본다(런을 만든 뒤 .env에서 키를 뺀 것과 같다).
    scripted_app.state.settings = settings.model_copy(
        update={"gemini_paid_api_key": SecretStr("paid-" + "7" * 30)}
    )
    return scripted_app


async def paid_kb(client: AsyncClient) -> dict[str, Any]:
    created = await client.post(
        "/api/console/knowledge",
        json={
            "name": "급여 규정",
            "indexing_technique": "economy",
            "data_policy": {"acknowledged": True, "key_tier": "paid"},
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


async def test_the_workflow_executor_fails_a_paid_run_without_the_key(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    kb = await paid_kb(logged_in)
    created = await logged_in.post(
        "/api/console/workflows", json={"name": "급여 안내", "kind": "workflow"}
    )
    workflow_id = created.json()["id"]
    current = (await logged_in.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    graph = {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "title": "start",
                "config": {"fields": [{"name": "q", "type": "string"}]},
            },
            {
                "id": "knowledge_1",
                "type": "knowledge_retrieval",
                "title": "knowledge_1",
                "config": {"kb_ids": [kb["id"]], "query_var": "start.q"},
            },
            {
                "id": "end_1",
                "type": "end",
                "title": "end_1",
                "config": {"outputs": [{"name": "n", "var": "knowledge_1.count"}]},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "knowledge_1"},
            {"id": "e2", "source": "knowledge_1", "target": "end_1"},
        ],
    }
    saved = await logged_in.put(
        f"/api/console/workflows/{workflow_id}/draft",
        json={
            "graph": graph,
            "env_vars": [],
            "conversation_vars": [],
            "settings": current["settings"],
            "updated_at": current["updated_at"],
        },
    )
    assert saved.status_code == 200, saved.text

    response = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"q": "급여일"}, "response_mode": "blocking"},
    )

    assert response.status_code == 200, response.text
    run = response.json()
    assert run["limits"]["tier"] == "paid"
    assert run["status"] == "failed"
    assert run["error"]["code"] == "paid_key_missing"


async def test_the_agent_executor_fails_a_paid_run_without_the_key(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    kb = await paid_kb(logged_in)
    agent = (await logged_in.post("/api/console/agents", json={"name": "급여 도우미"})).json()
    current = (await logged_in.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {key: current[key] for key in DRAFT_FIELDS}
    body.update({"env_vars": [], "knowledge": {"kb_ids": [kb["id"]]}})
    saved = await logged_in.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    assert saved.status_code == 200, saved.text
    session = (
        await logged_in.post(f"/api/console/agents/{agent['id']}/preview/sessions", json={})
    ).json()

    response = await logged_in.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": "급여일?", "response_mode": "blocking"},
    )

    assert response.status_code == 200, response.text
    run = response.json()["run"]
    assert run["limits"]["tier"] == "paid"
    assert run["status"] == "failed"
    assert run["error"]["code"] == "paid_key_missing"
    assert app.state.llm.tiers == []
