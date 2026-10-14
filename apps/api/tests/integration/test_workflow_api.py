"""워크플로우 콘솔 API(2단계 설계 6.1, 10장 M12).

생성·초안·검증·미리보기 런·SSE·노드 런 기록을 보고, 승인 대기·재개·중지·대기 만료는 가짜 agent
노드(StubAgentRunner)로 확인한다. 이 노드는 M14의 agent 노드처럼 엔진 포트의 과업 서비스로 실제
자식 런(1단계 과업 런)을 만들고 기다린다. 자식 런의 모델 응답은 가짜 어댑터 각본이다.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.agent.task_service import AgentTaskSpec
from app.config import Settings
from app.db.models import WfRun
from app.db.types import utcnow
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import Usage
from app.schemas.events import NodeError
from app.services.runs import RunMaintenance
from app.workflow import nodes as workflow_nodes
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    NodeContext,
    NodeResult,
    NodeRunner,
    OutputSpec,
)

Events = Callable[[str], list[dict[str, Any]]]


class StubAgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    instruction: str = "워크플로우 과업"


class StubAgentRunner(NodeRunner[StubAgentConfig]):
    """agent 노드 자리. 자식 과업 런을 만들고 끝나거나 입력을 기다릴 때까지 기다린다."""

    type = "agent"
    Config = StubAgentConfig
    error_strategies = ALL_STRATEGIES
    counts_usage = False
    fixed_outputs = (
        OutputSpec("text", "string"),
        OutputSpec("status", "string"),
        OutputSpec("child_run_id", "string"),
    )

    async def run(
        self, ctx: NodeContext, config: StubAgentConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        tasks = ctx.ports.tasks
        assert tasks is not None
        if state is not None and state.get("child_run_id"):
            child = uuid.UUID(str(state["child_run_id"]))
        else:
            child = await tasks.start(
                AgentTaskSpec(
                    # 포트가 이 런의 워크스페이스로 바꾼다.
                    workspace_id=uuid.UUID(int=0),
                    agent_id=config.agent_id,
                    instruction=config.instruction,
                    correlation={"node_run_id": str(ctx.node_run_id), "node_id": ctx.node_id},
                )
            )
        result = await tasks.wait(child)
        kept = {"child_run_id": str(child)}
        if result.status == "waiting_input":
            return NodeResult(
                status="waiting_input",
                pending=result.pending_interaction,
                state=kept,
                child_run_id=child,
            )
        if result.status == "succeeded":
            return NodeResult(
                status="succeeded",
                outputs={"text": result.text, "status": result.status, "child_run_id": str(child)},
                usage=result.usage,
                child_run_id=child,
            )
        error = result.error
        return NodeResult(
            status="failed",
            error=NodeError(
                code=error.code if error is not None else result.status,
                message=error.message if error is not None else result.status,
            ),
            child_run_id=child,
        )


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def stub_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = workflow_nodes.default_registry().copy()
    registry.register(StubAgentRunner())
    monkeypatch.setattr(workflow_nodes, "_DEFAULT", registry)


@pytest.fixture
async def app(stub_registry: None, scripted_app: FastAPI) -> AsyncIterator[FastAPI]:
    yield scripted_app


@pytest.fixture
async def api(logged_in: AsyncClient) -> AsyncClient:
    return logged_in


def node(node_id: str, node_type: str, config: dict[str, Any], x: float = 0) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "title": node_id,
        "position": {"x": x, "y": 0},
        "config": config,
    }


def graph(nodes: list[dict[str, Any]], *pairs: str) -> dict[str, Any]:
    edges = []
    for index, pair in enumerate(pairs, start=1):
        source, target = pair.split(">")
        source, _, handle = source.partition(":")
        edges.append(
            {
                "id": f"e{index}",
                "source": source,
                "target": target,
                "source_handle": handle or "out",
            }
        )
    return {"nodes": nodes, "edges": edges}


GREETING = graph(
    [
        node(
            "start",
            "start",
            {"fields": [{"name": "name", "type": "string", "label": "이름", "required": True}]},
        ),
        node(
            "template_1",
            "template",
            {
                "template": "안녕하세요, {{ name }}님",
                "vars": [{"name": "name", "var": "start.name"}],
            },
            300,
        ),
        node("end_1", "end", {"outputs": [{"name": "greeting", "var": "template_1.output"}]}, 600),
    ],
    "start>template_1",
    "template_1>end_1",
)


def agent_graph(agent_id: str) -> dict[str, Any]:
    return graph(
        [
            node("start", "start", {"fields": []}),
            node("agent_1", "agent", {"agent_id": agent_id}, 300),
            node("end_1", "end", {"outputs": [{"name": "answer", "var": "agent_1.text"}]}, 600),
        ],
        "start>agent_1",
        "agent_1>end_1",
    )


async def create_workflow(api: AsyncClient, name: str = "인사", **fields: Any) -> dict[str, Any]:
    response = await api.post("/api/console/workflows", json={"name": name, **fields})
    assert response.status_code == 201, response.text
    return response.json()


async def save_draft(
    api: AsyncClient, workflow_id: str, body_graph: dict[str, Any], **fields: Any
) -> dict[str, Any]:
    current = (await api.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    body = {
        "graph": body_graph,
        "env_vars": [],
        "conversation_vars": [],
        "settings": current["settings"],
        "updated_at": current["updated_at"],
        **fields,
    }
    response = await api.put(f"/api/console/workflows/{workflow_id}/draft", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def published_agent(api: AsyncClient) -> str:
    agent = (await api.post("/api/console/agents", json={"name": "노드 에이전트"})).json()
    published = await api.post(f"/api/console/agents/{agent['id']}/publish", json={})
    assert published.status_code == 201, published.text
    return agent["id"]


async def wait_status(api: AsyncClient, run_id: str, *statuses: str, timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = (await api.get(f"/api/console/wf-runs/{run_id}")).json()
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.05)
    raise AssertionError(f"워크플로우 런이 {statuses}가 되지 않았다: {run['status']}")


async def wait_child(api: AsyncClient, run_id: str, *statuses: str, timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = (await api.get(f"/api/console/runs/{run_id}")).json()
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.05)
    raise AssertionError(f"자식 런이 {statuses}가 되지 않았다: {run['status']}")


def types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events if event["type"] != "ping"]


async def test_workflow_crud_draft_and_validation(api: AsyncClient) -> None:
    created = await create_workflow(api, "고객 인사")
    workflow_id = created["id"]
    assert created["kind"] == "workflow" and created["slug"] == "workflow"
    listed = (await api.get("/api/console/workflows", params={"kind": "workflow"})).json()
    assert [item["id"] for item in listed["items"]] == [workflow_id]
    assert (await api.get("/api/console/workflows", params={"kind": "chatflow"})).json()[
        "items"
    ] == []

    draft = (await api.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    assert [n["type"] for n in draft["graph"]["nodes"]] == ["start", "end"]
    assert draft["issues"] == [] and draft["version_number"] is None

    saved = await save_draft(
        api,
        workflow_id,
        GREETING,
        env_vars=[
            {"name": "API_BASE", "value": "https://example.com", "secret": False},
        ],
    )
    assert saved["issues"] == []
    assert saved["updated_at"] != draft["updated_at"]
    assert saved["env_vars"] == [
        {"name": "API_BASE", "secret": False, "value": "https://example.com", "has_value": True}
    ]
    stale = {
        "graph": GREETING,
        "env_vars": [],
        "conversation_vars": [],
        "settings": draft["settings"],
        "updated_at": draft["updated_at"],
    }
    conflict = await api.put(f"/api/console/workflows/{workflow_id}/draft", json=stale)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "draft_conflict"

    broken = graph(
        [node("start", "start", {"fields": []}), node("end_1", "end", {"outputs": []})],
    )
    check = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/validate", json={"graph": broken}
    )
    assert check.status_code == 200
    assert check.json()["ok"] is False
    assert any(issue["node_id"] == "end_1" for issue in check.json()["issues"])
    stored = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/validate",
        headers={"X-Requested-With": "fetch"},
    )
    assert stored.json() == {"ok": True, "issues": []}

    patched = await api.patch(
        f"/api/console/workflows/{workflow_id}", json={"name": "새 이름", "slug": "greet"}
    )
    assert patched.json()["name"] == "새 이름" and patched.json()["slug"] == "greet"
    deleted = await api.delete(
        f"/api/console/workflows/{workflow_id}", headers={"X-Requested-With": "fetch"}
    )
    assert deleted.status_code == 204
    assert (await api.get(f"/api/console/workflows/{workflow_id}")).status_code == 404


async def test_node_types_describe_the_registry(api: AsyncClient) -> None:
    response = await api.get("/api/console/node-types")

    assert response.status_code == 200
    by_type = {item["type"]: item for item in response.json()["items"]}
    assert by_type["start"]["implemented"] is True
    assert by_type["llm"]["implemented"] is True
    assert by_type["iteration"]["implemented"] is True
    assert by_type["if_else"]["branching"] is True


async def test_preview_run_streams_events_and_records_node_runs(
    api: AsyncClient, sse: Events
) -> None:
    workflow_id = (await create_workflow(api))["id"]
    await save_draft(api, workflow_id, GREETING)

    response = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/runs", json={"inputs": {"name": "지우"}}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    run_id = response.headers["x-run-id"]
    assert "x-session-id" not in response.headers
    events = sse(response.text)
    assert types(events) == [
        "workflow.queued",
        "workflow.started",
        "node.started",
        "node.completed",
        "node.started",
        "node.completed",
        "node.started",
        "node.completed",
        "workflow.completed",
    ]
    assert all(event["run_id"] == run_id and event["session_id"] is None for event in events)
    assert "conversation_id" not in events[0]
    completed = events[-1]["data"]
    assert completed["status"] == "succeeded"
    assert completed["outputs"] == {"greeting": "안녕하세요, 지우님"}
    assert completed["truncated"] is False

    run = (await api.get(f"/api/console/wf-runs/{run_id}")).json()
    assert run["status"] == "succeeded" and run["trigger"] == "preview"
    assert run["outputs"] == {"greeting": "안녕하세요, 지우님"}
    assert run["node_run_count"] == 3 and run["ended_at"] is not None
    assert [(n["node_id"], n["status"]) for n in run["node_runs"]] == [
        ("start", "succeeded"),
        ("template_1", "succeeded"),
        ("end_1", "succeeded"),
    ]
    template = run["node_runs"][1]
    detail = (await api.get(f"/api/console/wf-runs/{run_id}/node-runs/{template['id']}")).json()
    assert detail["outputs"] == {"output": "안녕하세요, 지우님"}
    assert detail["inputs"] == {"name": "지우"}

    log = (await api.get(f"/api/console/wf-runs/{run_id}/event-log")).json()
    assert [item["type"] for item in log["items"]] == types(events)
    replay = sse((await api.get(f"/api/console/wf-runs/{run_id}/events", params={"after": 2})).text)
    assert [event["seq"] for event in replay] == [event["seq"] for event in events[2:]]
    listed = (await api.get(f"/api/console/workflows/{workflow_id}/runs")).json()
    assert [item["id"] for item in listed["items"]] == [run_id]


async def test_blocking_preview_run_returns_the_final_run(api: AsyncClient) -> None:
    workflow_id = (await create_workflow(api))["id"]
    await save_draft(api, workflow_id, GREETING)

    response = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"name": "A"}, "response_mode": "blocking"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "succeeded" and body["outputs"] == {"greeting": "안녕하세요, A님"}
    assert response.headers["x-run-id"] == body["id"]


async def test_preview_run_rejects_broken_drafts_bad_inputs_and_chatflows(
    api: AsyncClient,
) -> None:
    workflow_id = (await create_workflow(api))["id"]
    await save_draft(api, workflow_id, GREETING)

    missing = await api.post(f"/api/console/workflows/{workflow_id}/draft/runs", json={})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_inputs"
    assert missing.json()["error"]["details"] == {"name": "필수 입력이다"}

    broken = graph(
        [
            node("start", "start", {"fields": []}),
            node("end_1", "end", {"outputs": [{"name": "x", "var": "nowhere.value"}]}),
        ],
        "start>end_1",
    )
    await save_draft(api, workflow_id, broken)
    invalid = await api.post(f"/api/console/workflows/{workflow_id}/draft/runs", json={})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "draft_invalid"
    assert invalid.json()["error"]["details"][0]["node_id"] == "end_1"

    chatflow = await create_workflow(api, "대화", kind="chatflow")
    wrong = await api.post(f"/api/console/workflows/{chatflow['id']}/draft/runs", json={})
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "wrong_kind"


async def test_waiting_run_resumes_through_input(
    app: FastAPI, api: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await published_agent(api)
    workflow_id = (await create_workflow(api, "승인 흐름"))["id"]
    await save_draft(api, workflow_id, agent_graph(agent_id))
    script.add(
        [*fake_tool_call("shell", {"command": "echo wf"}), fake_end("tool_use")],
        fake_text("승인 뒤 끝냈다"),
    )

    response = await api.post(f"/api/console/workflows/{workflow_id}/draft/runs", json={})
    events = sse(response.text)
    run_id = response.headers["x-run-id"]

    assert types(events)[-2:] == ["workflow.waiting_input", "workflow.completed"]
    assert events[-1]["data"]["status"] == "waiting_input"
    run = (await api.get(f"/api/console/wf-runs/{run_id}")).json()
    assert run["status"] == "waiting_input" and run["ended_at"] is None
    pending = run["pending_interaction"]
    assert pending["node_id"] == "agent_1"
    assert pending["interaction"]["kind"] == "approval"
    child_id = pending["child_run_id"]
    agent_node = next(item for item in run["node_runs"] if item["node_id"] == "agent_1")
    assert agent_node["status"] == "waiting_input" and agent_node["child_run_id"] == child_id
    child = (await api.get(f"/api/console/runs/{child_id}")).json()
    assert child["status"] == "waiting_input" and child["trigger"] == "task"

    resumed = await api.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"decision": "approve", "response_mode": "streaming"},
    )
    assert resumed.status_code == 200
    after = sse(resumed.text)
    assert types(after)[:3] == ["workflow.queued", "workflow.started", "workflow.resumed"]
    assert after[1]["data"]["resumed"] is True
    restarted = next(event for event in after if event["type"] == "node.started")
    assert restarted["data"]["node_run_id"] == agent_node["id"]
    assert restarted["data"]["child_run_id"] == child_id
    assert after[-1]["data"]["status"] == "succeeded"
    assert after[-1]["data"]["outputs"] == {"answer": "승인 뒤 끝냈다"}
    final = (await api.get(f"/api/console/wf-runs/{run_id}")).json()
    assert final["status"] == "succeeded" and final["pending_interaction"] is None
    assert (await api.get(f"/api/console/runs/{child_id}")).json()["status"] == "succeeded"
    # 대기 전환 직전에 쓴 체크포인트는 런이 성공하면 지운다(4.4).
    async with app.state.db.read() as session:
        state = await session.scalar(select(WfRun.state).where(WfRun.id == uuid.UUID(run_id)))
    assert state is None


async def test_answering_the_child_directly_resumes_the_workflow(
    api: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await published_agent(api)
    workflow_id = (await create_workflow(api, "직접 승인"))["id"]
    await save_draft(api, workflow_id, agent_graph(agent_id))
    script.add(
        [*fake_tool_call("shell", {"command": "echo direct"}), fake_end("tool_use")],
        fake_text("자식 화면에서 승인"),
    )
    response = await api.post(f"/api/console/workflows/{workflow_id}/draft/runs", json={})
    run_id = response.headers["x-run-id"]
    child_id = (await api.get(f"/api/console/wf-runs/{run_id}")).json()["pending_interaction"][
        "child_run_id"
    ]

    approved = await api.post(f"/api/console/runs/{child_id}/input", json={"decision": "approve"})

    assert approved.status_code == 202
    final = await wait_status(api, run_id, "succeeded", "failed")
    assert final["status"] == "succeeded"
    assert final["outputs"] == {"answer": "자식 화면에서 승인"}


async def test_stopping_a_waiting_run_cancels_the_child(
    api: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await published_agent(api)
    workflow_id = (await create_workflow(api, "중지"))["id"]
    await save_draft(api, workflow_id, agent_graph(agent_id))
    script.add([*fake_tool_call("shell", {"command": "echo stop"}), fake_end("tool_use")])
    response = await api.post(f"/api/console/workflows/{workflow_id}/draft/runs", json={})
    run_id = response.headers["x-run-id"]
    child_id = (await api.get(f"/api/console/wf-runs/{run_id}")).json()["pending_interaction"][
        "child_run_id"
    ]

    stopped = await api.post(f"/api/console/wf-runs/{run_id}/stop", json={})

    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
    assert (await api.get(f"/api/console/runs/{child_id}")).json()["status"] == "cancelled"
    replay = sse((await api.get(f"/api/console/wf-runs/{run_id}/events")).text)
    assert replay[-1]["type"] == "workflow.completed"
    assert replay[-1]["data"]["status"] == "cancelled"
    again = await api.post(f"/api/console/wf-runs/{run_id}/stop", json={})
    assert again.status_code == 409 and again.json()["error"]["code"] == "run_not_active"


async def test_child_input_timeout_requeues_and_fails_the_workflow(
    app: FastAPI, api: AsyncClient, script: Any
) -> None:
    agent_id = await published_agent(api)
    workflow_id = (await create_workflow(api, "만료"))["id"]
    await save_draft(api, workflow_id, agent_graph(agent_id))
    script.add([*fake_tool_call("shell", {"command": "echo late"}), fake_end("tool_use")])
    response = await api.post(f"/api/console/workflows/{workflow_id}/draft/runs", json={})
    run_id = response.headers["x-run-id"]
    child_id = (await api.get(f"/api/console/wf-runs/{run_id}")).json()["pending_interaction"][
        "child_run_id"
    ]
    state = app.state
    later = utcnow() + timedelta(hours=state.settings.waiting_input_ttl_hours + 1)
    maintenance = RunMaintenance(
        state.db,
        state.settings,
        bus=state.bus,
        storage=state.storage,
        clock=lambda: later,
        parents=state.wf_parents,
    )

    assert await maintenance.expire_waiting() == 1

    child = (await api.get(f"/api/console/runs/{child_id}")).json()
    assert child["status"] == "cancelled" and child["error"]["code"] == "input_timeout"
    final = await wait_status(api, run_id, "failed", "succeeded", "cancelled")
    assert final["status"] == "failed"
    assert final["error"]["code"] == "input_timeout" and final["error"]["node_id"] == "agent_1"


async def test_publish_versions_and_restore(api: AsyncClient) -> None:
    workflow_id = (await create_workflow(api))["id"]
    broken = graph(
        [
            node("start", "start", {"fields": []}),
            node("end_1", "end", {"outputs": [{"name": "x", "var": "nowhere.value"}]}),
        ],
        "start>end_1",
    )
    await save_draft(api, workflow_id, broken)
    refused = await api.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "draft_invalid"

    await save_draft(api, workflow_id, GREETING)
    first = await api.post(
        f"/api/console/workflows/{workflow_id}/publish", json={"change_note": "처음"}
    )
    assert first.status_code == 201, first.text
    assert first.json()["version"]["version_number"] == 1
    second = (await api.post(f"/api/console/workflows/{workflow_id}/publish", json={})).json()
    assert second["workflow"]["published_version_number"] == 2

    versions = (await api.get(f"/api/console/workflows/{workflow_id}/versions")).json()["items"]
    assert [(item["version_number"], item["is_current"]) for item in versions] == [
        (2, True),
        (1, False),
    ]
    detail = (
        await api.get(f"/api/console/workflows/{workflow_id}/versions/{versions[1]['id']}")
    ).json()
    assert detail["status"] == "published" and detail["change_note"] == "처음"
    restored = await api.post(
        f"/api/console/workflows/{workflow_id}/versions/{versions[1]['id']}/restore", json={}
    )
    assert restored.json()["published_version_number"] == 1


async def test_uploaded_file_becomes_a_file_input(api: AsyncClient, sse: Events) -> None:
    workflow_id = (await create_workflow(api, "파일"))["id"]
    file_graph = graph(
        [
            node(
                "start",
                "start",
                {"fields": [{"name": "doc", "type": "file", "label": "문서", "required": True}]},
            ),
            node("end_1", "end", {"outputs": [{"name": "doc", "var": "start.doc"}]}, 300),
        ],
        "start>end_1",
    )
    await save_draft(api, workflow_id, file_graph)
    uploaded = await api.post(
        f"/api/console/workflows/{workflow_id}/files",
        files={"file": ("보고서.txt", "내용".encode(), "text/plain")},
        headers={"X-Requested-With": "fetch"},
    )
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]

    response = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"doc": file_id}, "response_mode": "blocking"},
    )

    doc = response.json()["outputs"]["doc"]
    assert doc["file_id"] == file_id and doc["filename"] == "보고서.txt"
    assert doc["source"] == "wf_file" and doc["storage_key"].startswith("wf-files/")
    unknown = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"doc": str(uuid.uuid4())}},
    )
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "file_not_found"


async def test_llm_node_streams_deltas_and_counts_usage(
    api: AsyncClient, script: Any, sse: Events
) -> None:
    script.add(
        [
            *fake_text("스트리밍으로 오는 답이다", chunk_size=4),
            fake_end(usage=Usage(input_tokens=30, output_tokens=6)),
        ]
    )
    workflow_id = (await create_workflow(api, "LLM 흐름"))["id"]
    llm_graph = graph(
        [
            node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            node("llm_1", "llm", {"prompt": [{"role": "user", "text": "{{ start.q }}"}]}, 300),
            node("end_1", "end", {"outputs": [{"name": "answer", "var": "llm_1.text"}]}, 600),
        ],
        "start>llm_1",
        "llm_1>end_1",
    )
    await save_draft(api, workflow_id, llm_graph)

    response = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/runs", json={"inputs": {"q": "질문"}}
    )

    events = sse(response.text)
    deltas = [event["data"] for event in events if event["type"] == "node.text_delta"]
    assert len(deltas) > 1 and {delta["node_id"] for delta in deltas} == {"llm_1"}
    assert "".join(delta["text"] for delta in deltas) == "스트리밍으로 오는 답이다"
    [used] = [event["data"] for event in events if event["type"] == "node.usage"]
    assert (used["model"], used["input_tokens"], used["output_tokens"]) == ("fake-model", 30, 6)
    completed = events[-1]["data"]
    assert completed["outputs"] == {"answer": "스트리밍으로 오는 답이다"}
    assert completed["usage_total"]["input_tokens"] == 30
    run_id = response.headers["x-run-id"]
    run = (await api.get(f"/api/console/wf-runs/{run_id}")).json()
    assert run["usage"]["input_tokens"] == 30 and run["usage"]["output_tokens"] == 6
    log = (await api.get(f"/api/console/wf-runs/{run_id}/event-log")).json()
    logged = [item["type"] for item in log["items"]]
    # 델타는 영속하지 않고 사용량 이벤트는 남는다.
    assert "node.usage" in logged and "node.text_delta" not in logged
    stats = await api.get("/api/console/stats/usage", params={"group_by": "workflow"})
    assert stats.status_code == 200, stats.text
    row = next(item for item in stats.json()["items"] if item["key"] == workflow_id)
    assert (row["label"], row["runs"], row["input_tokens"]) == ("LLM 흐름", 1, 30)
