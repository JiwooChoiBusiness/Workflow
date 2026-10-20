"""워크플로우 서비스 API(2단계 설계 6.3, A7)와 워크플로우 API 키(4.1).

워크플로우 키로 발행 버전을 /workflows/run(streaming·blocking)으로 실행하고, 런 조회·목록·중지·
응답은 그 키로 만든 런에만 닿는다. 키는 대상에 맞는 권한으로 만들어지고, 워크플로우를 지우면
폐기된다.
"""

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.db.models import WfFile
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call

Events = Callable[[str], list[dict[str, Any]]]
FETCH = {"X-Requested-With": "fetch"}
AGENT_DRAFT_FIELDS = (
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
SHELL = [*fake_tool_call("shell", {"command": "echo flow"}), fake_end("tool_use")]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


@pytest.fixture
def api(make_client: Callable[[], AsyncClient]) -> AsyncClient:
    return make_client()


def node(node_id: str, node_type: str, config: dict[str, Any], x: float = 0) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "title": node_id,
        "position": {"x": x, "y": 0},
        "config": config,
    }


def chain(*ids: str) -> list[dict[str, Any]]:
    return [
        {"id": f"e{index}", "source": source, "target": target}
        for index, (source, target) in enumerate(zip(ids, ids[1:], strict=False), start=1)
    ]


def greeting() -> dict[str, Any]:
    return {
        "nodes": [
            node(
                "start",
                "start",
                {
                    "fields": [
                        {"name": "name", "type": "string", "required": True},
                        {"name": "doc", "type": "file"},
                    ]
                },
            ),
            node(
                "template_1",
                "template",
                {"template": "안녕, {{ name }}", "vars": [{"name": "name", "var": "start.name"}]},
                300,
            ),
            node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "greeting", "var": "template_1.output"},
                        {"name": "doc", "var": "start.doc"},
                    ]
                },
                600,
            ),
        ],
        "edges": chain("start", "template_1", "end_1"),
    }


async def workflow(
    console: AsyncClient, graph: dict[str, Any], *, kind: str = "workflow", publish: bool = True
) -> str:
    created = await console.post("/api/console/workflows", json={"name": "자동 흐름", "kind": kind})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    current = (await console.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    saved = await console.put(
        f"/api/console/workflows/{workflow_id}/draft",
        json={
            "graph": graph,
            "env_vars": [],
            "conversation_vars": [],
            "settings": current["settings"],
            "updated_at": current["updated_at"],
        },
    )
    assert saved.status_code == 200 and saved.json()["issues"] == [], saved.text
    if publish:
        published = await console.post(f"/api/console/workflows/{workflow_id}/publish", json={})
        assert published.status_code == 201, published.text
    return workflow_id


async def key_for(console: AsyncClient, workflow_id: str, **body: Any) -> dict[str, Any]:
    response = await console.post(
        f"/api/console/workflows/{workflow_id}/api-keys", json={"name": "자동화", **body}
    )
    assert response.status_code == 201, response.text
    return response.json()


def bearer(created: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {created['key']}"}


def error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def test_workflow_api_keys_follow_the_target(logged_in: AsyncClient) -> None:
    flow_id = await workflow(logged_in, greeting(), publish=False)
    chat_id = (
        await logged_in.post("/api/console/workflows", json={"name": "챗", "kind": "chatflow"})
    ).json()["id"]

    flow_key = await key_for(logged_in, flow_id)
    chat_key = await key_for(logged_in, chat_id)
    wrong = await logged_in.post(
        f"/api/console/workflows/{flow_id}/api-keys", json={"name": "k", "scopes": ["tasks"]}
    )
    missing = await logged_in.post(
        f"/api/console/workflows/{uuid.uuid4()}/api-keys", json={"name": "k"}
    )

    assert (flow_key["workflow_id"], flow_key["agent_id"]) == (flow_id, None)
    assert flow_key["scopes"] == ["workflow"] and chat_key["scopes"] == ["chat"]
    assert wrong.status_code == 422
    assert wrong.json()["error"]["details"][0]["type"] == "scope_not_allowed"
    assert missing.status_code == 404
    listed = (await logged_in.get(f"/api/console/workflows/{flow_id}/api-keys")).json()["items"]
    assert [item["id"] for item in listed] == [flow_key["id"]]
    assert "key" not in listed[0]
    # 다른 워크플로우의 키는 이 경로로 폐기할 수 없다.
    other = await logged_in.delete(
        f"/api/console/workflows/{chat_id}/api-keys/{flow_key['id']}", headers=FETCH
    )
    assert other.status_code == 404
    revoked = await logged_in.delete(
        f"/api/console/workflows/{flow_id}/api-keys/{flow_key['id']}", headers=FETCH
    )
    assert revoked.status_code == 204
    after = (await logged_in.get(f"/api/console/workflows/{flow_id}/api-keys")).json()["items"]
    assert after[0]["revoked_at"] is not None


async def test_workflows_run_streams_or_blocks_with_a_workflow_key(
    app: FastAPI, logged_in: AsyncClient, api: AsyncClient, sse: Events
) -> None:
    flow_id = await workflow(logged_in, greeting())
    key = bearer(await key_for(logged_in, flow_id))
    other = bearer(await key_for(logged_in, flow_id))
    upload = await api.post(
        "/api/v1/files/upload", headers=key, files={"file": ("첨부.txt", b"x", "text/plain")}
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["file_id"]

    streamed = await api.post(
        "/api/v1/workflows/run",
        headers=key,
        json={"inputs": {"name": "지우", "doc": file_id}, "user": "u-1"},
    )
    blocking = await api.post(
        "/api/v1/workflows/run",
        headers=key,
        json={"inputs": {"name": "민수"}, "user": "u-2", "response_mode": "blocking"},
    )

    events = sse(streamed.text)
    assert events[0]["type"] == "workflow.queued" and events[-1]["type"] == "workflow.completed"
    assert events[-1]["data"]["outputs"]["greeting"] == "안녕, 지우"
    assert events[-1]["data"]["outputs"]["doc"]["file_id"] == file_id
    run_id = streamed.headers["x-run-id"]
    async with app.state.db.read() as session:
        bound = await session.get(WfFile, uuid.UUID(file_id))
    assert bound is not None and str(bound.wf_run_id) == run_id
    assert blocking.status_code == 200, blocking.text
    result = blocking.json()
    assert (result["status"], result["outputs"]) == (
        "succeeded",
        {"greeting": "안녕, 민수", "doc": None},
    )
    assert result["elapsed_ms"] >= 0 and result["error"] is None
    assert result["conversation_id"] is None
    detail = (await logged_in.get(f"/api/console/wf-runs/{run_id}")).json()
    assert (detail["trigger"], detail["external_user"]) == ("api", "u-1")
    polled = (await api.get(f"/api/v1/workflows/run/{run_id}", headers=key)).json()
    assert polled["workflow_run_id"] == run_id and polled["status"] == "succeeded"
    # 다른 키·다른 사용자의 런은 없는 것으로 본다.
    assert (await api.get(f"/api/v1/workflows/run/{run_id}", headers=other)).status_code == 404
    assert (
        await api.get(f"/api/v1/workflows/run/{run_id}", headers=key, params={"user": "u-2"})
    ).status_code == 404
    logs = (await api.get("/api/v1/workflows/logs", headers=key, params={"limit": 1})).json()
    assert [item["workflow_run_id"] for item in logs["items"]] == [result["workflow_run_id"]]
    assert logs["next_cursor"] == "1"
    rest = (
        await api.get("/api/v1/workflows/logs", headers=key, params={"cursor": logs["next_cursor"]})
    ).json()
    assert [item["workflow_run_id"] for item in rest["items"]] == [run_id]
    mine = (await api.get("/api/v1/workflows/logs", headers=key, params={"user": "u-1"})).json()
    assert [item["workflow_run_id"] for item in mine["items"]] == [run_id]
    assert (await api.get("/api/v1/workflows/logs", headers=other)).json()["items"] == []
    info = (await api.get("/api/v1/info", headers=key)).json()
    assert (info["kind"], info["version_number"]) == ("workflow", 1)
    parameters = (await api.get("/api/v1/parameters", headers=key)).json()
    assert [field["name"] for field in parameters["user_input_form"]] == ["name", "doc"]
    assert parameters["scopes"] == ["workflow"] and parameters["opening_statement"] is None


async def test_run_errors_and_wrong_targets(logged_in: AsyncClient, api: AsyncClient) -> None:
    flow_id = await workflow(logged_in, greeting())
    key = bearer(await key_for(logged_in, flow_id))
    draft_only = await workflow(logged_in, greeting(), publish=False)
    unpublished = bearer(await key_for(logged_in, draft_only))
    chat_id = (
        await logged_in.post("/api/console/workflows", json={"name": "챗", "kind": "chatflow"})
    ).json()["id"]
    await logged_in.post(f"/api/console/workflows/{chat_id}/publish", json={})
    chat = bearer(await key_for(logged_in, chat_id))
    agent = (await logged_in.post("/api/console/agents", json={"name": "에이전트"})).json()
    await logged_in.post(f"/api/console/agents/{agent['id']}/publish", json={})
    agent_key = await logged_in.post(
        f"/api/console/agents/{agent['id']}/api-keys", json={"name": "k"}
    )
    agent_headers = bearer(agent_key.json())

    invalid = await api.post("/api/v1/workflows/run", headers=key, json={"inputs": {}})
    not_published = await api.post(
        "/api/v1/workflows/run", headers=unpublished, json={"inputs": {"name": "a"}}
    )
    by_chat = await api.post("/api/v1/workflows/run", headers=chat, json={"inputs": {}})
    by_agent = await api.post("/api/v1/workflows/run", headers=agent_headers, json={"inputs": {}})
    task = await api.post(
        "/api/v1/agent-tasks", headers=key, json={"instruction": "x", "response_mode": "blocking"}
    )
    conversations = await api.get("/api/v1/conversations", headers=key)

    assert error(invalid) == (422, "invalid_inputs")
    assert error(not_published) == (409, "workflow_not_published")
    assert error(by_chat) == (400, "wrong_target")
    assert by_chat.json()["error"]["details"] == {
        "key_target": "chatflow",
        "expected": ["workflow"],
    }
    assert error(by_agent) == (400, "wrong_target")
    assert error(task) == (400, "wrong_target")
    assert error(conversations) == (400, "wrong_target")
    # 워크플로우를 지우면 그 키도 바로 폐기된다.
    await logged_in.delete(f"/api/console/workflows/{flow_id}", headers=FETCH)
    gone = await api.get("/api/v1/info", headers=key)
    assert error(gone) == (401, "invalid_api_key")


async def test_waiting_workflow_runs_resume_or_stop_through_the_api(
    logged_in: AsyncClient, api: AsyncClient, script: Any
) -> None:
    agent = (await logged_in.post("/api/console/agents", json={"name": "셸 담당"})).json()
    current = (await logged_in.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {name: current[name] for name in AGENT_DRAFT_FIELDS}
    body.update({"env_vars": [], "approval_mode": "all_shell"})
    await logged_in.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    await logged_in.post(f"/api/console/agents/{agent['id']}/publish", json={})
    flow_id = await workflow(
        logged_in,
        {
            "nodes": [
                node("start", "start", {"fields": []}),
                node("agent_1", "agent", {"agent_id": agent["id"], "instruction": "점검한다"}),
                node("end_1", "end", {"outputs": [{"name": "answer", "var": "agent_1.text"}]}),
            ],
            "edges": chain("start", "agent_1", "end_1"),
        },
    )
    key = bearer(await key_for(logged_in, flow_id))
    script.add(SHELL, fake_text("점검 끝"), SHELL)

    waiting = await api.post(
        "/api/v1/workflows/run", headers=key, json={"inputs": {}, "response_mode": "blocking"}
    )

    first = waiting.json()
    assert first["status"] == "waiting_input"
    assert first["pending_interaction"]["interaction"]["kind"] == "approval"
    queued = await api.post(
        f"/api/v1/workflows/run/{first['workflow_run_id']}/input",
        headers=key,
        json={"decision": "approve"},
    )
    assert queued.status_code == 202
    assert queued.json()["workflow_run_id"] == first["workflow_run_id"]
    done = (await api.get(f"/api/v1/workflows/run/{first['workflow_run_id']}", headers=key)).json()
    for _ in range(100):
        if done["status"] == "succeeded":
            break
        await asyncio.sleep(0.1)
        done = (
            await api.get(f"/api/v1/workflows/run/{first['workflow_run_id']}", headers=key)
        ).json()
    assert done["status"] == "succeeded" and done["outputs"] == {"answer": "점검 끝"}

    second = (
        await api.post(
            "/api/v1/workflows/run", headers=key, json={"inputs": {}, "response_mode": "blocking"}
        )
    ).json()
    stopped = await api.post(f"/api/v1/workflows/run/{second['workflow_run_id']}/stop", headers=key)
    assert stopped.json() == {"result": "success", "status": "cancelled"}
