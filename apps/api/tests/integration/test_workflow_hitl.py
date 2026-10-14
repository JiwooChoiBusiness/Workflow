"""agent 노드의 승인 대기·재개와 단일 노드 실행(2단계 설계 3.7, 6.1, 10장 M14, 완료 기준 A4).

실제 agent 노드가 1단계 과업 런을 자식으로 만들고, 자식 런의 모델 응답은 가짜 어댑터 각본이다.
승인·거절, 자식이 먼저 끝난 경우, 병렬 대기의 순차 처리, 중지, 재기동, 단일 노드 실행 API를 본다.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app import sysinfo
from app.agent.interactions import InteractionReply
from app.config import Settings
from app.db.models import ChatSession, Message, Run
from app.db.repositories.workspaces import WorkspaceRepository
from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text, fake_tool_call
from app.logsetup import configure_logging
from app.main import create_app
from app.services.runs import RunService

Events = Callable[[str], list[dict[str, Any]]]
BASE_URL = "http://127.0.0.1:8500"
EMAIL = "owner@example.com"
PASSWORD = "correct-horse-battery"  # noqa: S105  테스트 계정
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
SHELL = [*fake_tool_call("shell", {"command": "echo wf"}), fake_end("tool_use")]
SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
async def app(scripted_app: FastAPI) -> AsyncIterator[FastAPI]:
    yield scripted_app


@pytest.fixture
async def api(logged_in: AsyncClient) -> AsyncClient:
    return logged_in


def node(node_id: str, node_type: str, config: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "title": node_id, "config": config, **fields}


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


def agent_node(node_id: str, agent_id: str, **config: Any) -> dict[str, Any]:
    return node(
        node_id,
        "agent",
        {"agent_id": agent_id, "instruction": "{{ start.q }}를 확인한다", **config},
    )


def agent_flow(agent_id: str, **config: Any) -> dict[str, Any]:
    return graph(
        [
            node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            agent_node("agent_1", agent_id, **config),
            node("end_1", "end", {"outputs": [{"name": "answer", "var": "agent_1.text"}]}),
        ],
        "start>agent_1",
        "agent_1>end_1",
    )


async def make_agent(
    api: AsyncClient, name: str = "확인 담당", *, publish: bool = True, **draft: Any
) -> str:
    agent = (await api.post("/api/console/agents", json={"name": name})).json()
    current = (await api.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {key: current[key] for key in DRAFT_FIELDS}
    body.update({"env_vars": [], "approval_mode": "all_shell", **draft})
    saved = await api.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    assert saved.status_code == 200, saved.text
    if publish:
        published = await api.post(f"/api/console/agents/{agent['id']}/publish", json={})
        assert published.status_code == 201, published.text
    return agent["id"]


async def make_workflow(api: AsyncClient, body_graph: dict[str, Any], **settings: Any) -> str:
    created = await api.post("/api/console/workflows", json={"name": "검토 흐름"})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    current = (await api.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    body = {
        "graph": body_graph,
        "env_vars": [],
        "conversation_vars": [],
        "settings": {**current["settings"], **settings},
        "updated_at": current["updated_at"],
    }
    saved = await api.put(f"/api/console/workflows/{workflow_id}/draft", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["issues"] == [], saved.json()["issues"]
    return workflow_id


async def wait_status(api: AsyncClient, path: str, *statuses: str, timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    body: Any = None
    while time.monotonic() < deadline:
        body = (await api.get(path)).json()
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path}가 {statuses}가 되지 않았다: {body}")


def types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events if event["type"] != "ping"]


async def start_waiting(
    api: AsyncClient, sse: Events, workflow_id: str, q: str = "보고서"
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    response = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/runs", json={"inputs": {"q": q}}
    )
    assert response.status_code == 200, response.text
    events = sse(response.text)
    run_id = response.headers["x-run-id"]
    run = (await api.get(f"/api/console/wf-runs/{run_id}")).json()
    assert run["status"] == "waiting_input", run
    return run_id, events, run


async def test_agent_node_waits_for_approval_and_the_same_run_finishes(
    app: FastAPI, api: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await make_agent(api)
    workflow_id = await make_workflow(api, agent_flow(agent_id))
    script.add(SHELL, fake_text("셸로 확인했다"))

    # 입력 속 {{ }}는 지시를 한 번 렌더할 때 값으로만 들어가고 과업 서비스가 다시 채우지 않는다.
    run_id, events, run = await start_waiting(api, sse, workflow_id, q="{{ 7 * 7 }}")

    assert types(events)[-2:] == ["workflow.waiting_input", "workflow.completed"]
    pending = run["pending_interaction"]
    assert pending["node_id"] == "agent_1" and pending["interaction"]["kind"] == "approval"
    child_id = pending["child_run_id"]
    progress = next(event["data"] for event in events if event["type"] == "node.progress")
    assert progress["child_run_id"] == child_id
    session_id = progress["child_session_id"]
    child = (await api.get(f"/api/console/runs/{child_id}")).json()
    assert child["status"] == "waiting_input" and child["trigger"] == "task"
    assert child["session_id"] == session_id and child["parent_run_id"] is None
    agent_run = next(item for item in run["node_runs"] if item["node_id"] == "agent_1")
    assert agent_run["status"] == "waiting_input" and agent_run["child_run_id"] == child_id
    async with app.state.db.read() as session:
        row = await session.get(Run, uuid.UUID(child_id))
        chat = await session.get(ChatSession, uuid.UUID(session_id))
        first = await session.scalar(
            select(Message).where(Message.run_id == uuid.UUID(child_id)).order_by(Message.seq)
        )
    assert row is not None and chat is not None and first is not None
    correlation = row.input["correlation"]
    assert correlation["workflow_run_id"] == run_id and correlation["node_id"] == "agent_1"
    assert correlation["node_run_id"] == agent_run["id"]
    assert chat.title == "검토 흐름 → {{ 7 * 7 }}를 확인한다"
    assert chat.meta["workflow_run_id"] == run_id
    assert first.text is not None and first.text.startswith("{{ 7 * 7 }}를 확인한다")

    resumed = await api.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"decision": "approve", "response_mode": "streaming"},
    )

    after = sse(resumed.text)
    assert types(after)[:3] == ["workflow.queued", "workflow.started", "workflow.resumed"]
    restarted = next(event["data"] for event in after if event["type"] == "node.started")
    assert restarted["node_run_id"] == agent_run["id"]
    assert (restarted["child_run_id"], restarted["child_session_id"]) == (child_id, session_id)
    assert after[-1]["data"]["status"] == "succeeded"
    assert after[-1]["data"]["outputs"] == {"answer": "셸로 확인했다"}
    final = (await api.get(f"/api/console/wf-runs/{run_id}")).json()
    assert final["status"] == "succeeded" and final["node_run_count"] == 3
    # 자식 사용량은 런 사용량에 더하지 않는다(3.10).
    assert final["usage"]["input_tokens"] == 0
    child = (await api.get(f"/api/console/runs/{child_id}")).json()
    assert child["status"] == "succeeded" and child["usage"]["input_tokens"] > 0


async def test_rejecting_lets_the_child_finish_and_the_run_succeeds(
    api: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await make_agent(api)
    workflow_id = await make_workflow(api, agent_flow(agent_id))
    script.add(SHELL, fake_text("거절되어 셸 없이 끝냈다"))
    run_id, _, _ = await start_waiting(api, sse, workflow_id)

    rejected = await api.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"decision": "reject", "note": "셸은 쓰지 않는다", "response_mode": "blocking"},
    )

    assert rejected.status_code == 200, rejected.text
    body = rejected.json()
    assert body["status"] == "succeeded"
    assert body["outputs"] == {"answer": "거절되어 셸 없이 끝냈다"}


async def test_child_that_finished_before_the_workflow_was_requeued_still_gives_its_result(
    app: FastAPI, api: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await make_agent(api)
    workflow_id = await make_workflow(api, agent_flow(agent_id))
    script.add(SHELL, fake_text("자식이 먼저 끝났다"))
    run_id, _, run = await start_waiting(api, sse, workflow_id)
    child_id = run["pending_interaction"]["child_run_id"]
    state = app.state
    async with state.db.read() as session:
        [workspace] = await WorkspaceRepository(session).list_all()
    # 워크플로우 런을 다시 넣는 훅 없이 자식에만 답한다(재투입 전에 서버가 멈춘 것과 같다).
    runs = RunService(
        state.db,
        state.settings,
        bus=state.bus,
        supervisor=state.supervisor,
        storage=state.storage,
        backend=state.sandbox,
    )
    await runs.provide_input(
        workspace.id, uuid.UUID(child_id), InteractionReply(decision="approve"), subscribe=False
    )
    await wait_status(api, f"/api/console/runs/{child_id}", "succeeded")
    assert (await api.get(f"/api/console/wf-runs/{run_id}")).json()["status"] == "waiting_input"

    late = await api.post(f"/api/console/wf-runs/{run_id}/input", json={"decision": "approve"})

    # 자식은 이미 답을 받았지만 워크플로우 런은 다시 들어가 그 결과를 받는다.
    assert late.status_code == 409 and late.json()["error"]["code"] == "run_not_waiting"
    final = await wait_status(api, f"/api/console/wf-runs/{run_id}", "succeeded", "failed")
    assert final["status"] == "succeeded"
    assert final["outputs"] == {"answer": "자식이 먼저 끝났다"}


async def test_two_parallel_agents_pause_the_run_one_after_another(
    api: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await make_agent(api)
    flow = graph(
        [
            node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            agent_node("agent_a", agent_id),
            agent_node("agent_b", agent_id),
            node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "a", "var": "agent_a.text"},
                        {"name": "b", "var": "agent_b.text"},
                    ]
                },
            ),
        ],
        "start>agent_a",
        "start>agent_b",
        "agent_a>end_1",
        "agent_b>end_1",
    )
    workflow_id = await make_workflow(api, flow)
    script.add(SHELL, SHELL, fake_text("첫 승인 뒤 끝"), fake_text("두 번째 승인 뒤 끝"))

    run_id, _, run = await start_waiting(api, sse, workflow_id)

    assert run["pending_interaction"]["node_id"] == "agent_a"
    second = await api.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"decision": "approve", "response_mode": "blocking"},
    )
    assert second.json()["status"] == "waiting_input"
    assert second.json()["pending_interaction"]["node_id"] == "agent_b"
    done = await api.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"decision": "approve", "response_mode": "blocking"},
    )
    assert done.json()["status"] == "succeeded"
    assert done.json()["outputs"] == {"a": "첫 승인 뒤 끝", "b": "두 번째 승인 뒤 끝"}
    log = (await api.get(f"/api/console/wf-runs/{run_id}/event-log")).json()["items"]
    assert [item["type"] for item in log].count("workflow.waiting_input") == 2


async def test_stopping_a_waiting_run_cancels_the_child(
    api: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await make_agent(api)
    workflow_id = await make_workflow(api, agent_flow(agent_id))
    script.add(SHELL)
    run_id, _, run = await start_waiting(api, sse, workflow_id)
    child_id = run["pending_interaction"]["child_run_id"]

    stopped = await api.post(f"/api/console/wf-runs/{run_id}/stop", json={})

    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
    assert (await api.get(f"/api/console/runs/{child_id}")).json()["status"] == "cancelled"


async def test_json_result_files_and_approval_off_setting(
    app: FastAPI, api: AsyncClient, script: Any
) -> None:
    agent_id = await make_agent(api)
    flow = graph(
        [
            node(
                "start",
                "start",
                {
                    "fields": [
                        {"name": "q", "type": "string"},
                        {"name": "docs", "type": "file_list"},
                    ]
                },
            ),
            agent_node(
                "agent_1",
                agent_id,
                files_var="start.docs",
                result={"format": "json", "schema": SCHEMA},
            ),
            node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "result", "var": "agent_1.result"},
                        {"name": "status", "var": "agent_1.status"},
                        {"name": "files", "var": "agent_1.files"},
                    ]
                },
            ),
        ],
        "start>agent_1",
        "agent_1>end_1",
    )
    # 버전 설정 approval_mode=off면 자식의 셸도 승인 없이 돈다(9.1).
    workflow_id = await make_workflow(api, flow, approval_mode="off")
    uploaded = await api.post(
        f"/api/console/workflows/{workflow_id}/files",
        files={"file": ("자료.txt", "내용".encode(), "text/plain")},
        headers={"X-Requested-With": "fetch"},
    )
    file_id = uploaded.json()["id"]
    script.add(
        SHELL,
        [
            *fake_tool_call("write_file", {"path": "outputs/요약.md", "content": "# 요약"}),
            fake_end("tool_use"),
        ],
        [*fake_tool_call("submit_result", {"summary": "짧게"}), fake_end("tool_use")],
    )

    response = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"q": "자료", "docs": [file_id]}, "response_mode": "blocking"},
    )

    body = response.json()
    assert body["status"] == "succeeded", body
    outputs = body["outputs"]
    assert outputs["result"] == {"summary": "짧게"} and outputs["status"] == "succeeded"
    # 자식이 남긴 파일은 세션 파일 참조로 나온다. 다음 노드가 파일 변수로 쓴다.
    [made] = outputs["files"]
    assert (made["filename"], made["source"]) == ("요약.md", "session_file")
    assert made["storage_key"].startswith("session-files/")
    run = (await api.get(f"/api/console/wf-runs/{body['id']}")).json()
    child_id = next(item for item in run["node_runs"] if item["node_id"] == "agent_1")[
        "child_run_id"
    ]
    async with app.state.db.read() as session:
        child = await session.get(Run, uuid.UUID(child_id))
    assert child is not None and child.approval_mode == "off"
    # 워크플로우 파일은 자식 작업공간 task/files/로 복사된다.
    [task_file] = child.input["files"]
    assert task_file["wf_file_id"] == file_id and task_file["path"] == "task/files/자료.txt"


async def test_preview_uses_the_draft_of_an_unpublished_agent(
    app: FastAPI, api: AsyncClient, script: Any
) -> None:
    agent_id = await make_agent(api, "초안 담당", publish=False, approval_mode="off")
    workflow_id = await make_workflow(api, agent_flow(agent_id))
    script.add(fake_text("초안으로 답했다"))

    response = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"q": "x"}, "response_mode": "blocking"},
    )

    assert response.json()["status"] == "succeeded", response.json()
    assert response.json()["outputs"] == {"answer": "초안으로 답했다"}
    draft = (await api.get(f"/api/console/agents/{agent_id}/draft")).json()
    run = (await api.get(f"/api/console/wf-runs/{response.json()['id']}")).json()
    child_id = next(item for item in run["node_runs"] if item["node_id"] == "agent_1")[
        "child_run_id"
    ]
    child = (await api.get(f"/api/console/runs/{child_id}")).json()
    assert child["agent_version_id"] == draft["id"]


async def test_iteration_preview_run_records_inner_node_runs(api: AsyncClient) -> None:
    flow = graph(
        [
            node("start", "start", {"fields": [{"name": "docs", "type": "file_list"}]}),
            node(
                "iteration_1",
                "iteration",
                {
                    "input_var": "start.docs",
                    "output_var": "template_2.output",
                    "parallel": True,
                    "max_parallel": 2,
                },
            ),
            node("iteration_start_1", "iteration_start", {}, parent_id="iteration_1"),
            node(
                "template_2",
                "template",
                {
                    "template": "{{ i }}:{{ doc }}",
                    "vars": [
                        {"name": "doc", "var": "iteration_start_1.item"},
                        {"name": "i", "var": "iteration_start_1.index"},
                    ],
                },
                parent_id="iteration_1",
            ),
            node("end_1", "end", {"outputs": [{"name": "names", "var": "iteration_1.output"}]}),
        ],
        "start>iteration_1",
        "iteration_1>end_1",
        "iteration_start_1>template_2",
    )
    workflow_id = await make_workflow(api, flow)
    ids = []
    for name in ("가.txt", "나.txt", "다.txt"):
        uploaded = await api.post(
            f"/api/console/workflows/{workflow_id}/files",
            files={"file": (name, b"x", "text/plain")},
            headers={"X-Requested-With": "fetch"},
        )
        ids.append(uploaded.json()["id"])

    response = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"docs": ids}, "response_mode": "blocking"},
    )

    body = response.json()
    assert body["status"] == "succeeded", body
    assert body["outputs"] == {"names": ["0:가.txt", "1:나.txt", "2:다.txt"]}
    run = (await api.get(f"/api/console/wf-runs/{body['id']}")).json()
    assert [item["node_id"] for item in run["node_runs"]] == ["start", "iteration_1", "end_1"]
    inner = (
        await api.get(
            f"/api/console/wf-runs/{body['id']}/node-runs", params={"iteration": "iteration_1"}
        )
    ).json()["items"]
    assert sorted(item["iteration_index"] for item in inner) == [0, 1, 2]
    assert {item["node_id"] for item in inner} == {"template_2"}
    log = (await api.get(f"/api/console/wf-runs/{body['id']}/event-log")).json()["items"]
    logged = [item["type"] for item in log]
    assert logged.count("iteration.next") == 3 and "iteration.completed" in logged


def single_flow(agent_id: str) -> dict[str, Any]:
    return graph(
        [
            node(
                "start",
                "start",
                {
                    "fields": [
                        {"name": "q", "type": "string"},
                        {"name": "score", "type": "number"},
                    ]
                },
            ),
            node(
                "template_1",
                "template",
                {"template": "[{{ x }}]", "vars": [{"name": "x", "var": "start.q"}]},
            ),
            node(
                "if_else_1",
                "if_else",
                {
                    "cases": [
                        {"id": "hi", "conditions": [{"var": "start.score", "op": "≥", "value": 50}]}
                    ]
                },
            ),
            node(
                "agent_1",
                "agent",
                {"agent_id": agent_id, "instruction": "{{ template_1.output }} 확인"},
            ),
            node("end_1", "end", {"outputs": [{"name": "v", "var": "agent_1.text"}]}),
            node("end_2", "end", {"outputs": [{"name": "v", "var": "template_1.output"}]}),
        ],
        "start>template_1",
        "template_1>if_else_1",
        "template_1>agent_1",
        "agent_1>end_1",
        "if_else_1:case_hi>end_2",
        "if_else_1:else>end_2",
    )


async def test_single_node_runs_for_template_if_else_and_agent(
    api: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await make_agent(api)
    workflow_id = await make_workflow(api, single_flow(agent_id))
    base = f"/api/console/workflows/{workflow_id}/draft/nodes"

    template = await api.post(f"{base}/template_1/runs", json={"inputs": {"start.q": "값"}})

    assert template.status_code == 200
    events = sse(template.text)
    assert types(events) == [
        "workflow.queued",
        "workflow.started",
        "node.started",
        "node.completed",
        "workflow.completed",
    ]
    assert events[-1]["data"]["outputs"] == {"output": "[값]"}
    run_id = template.headers["x-run-id"]
    run = (await api.get(f"/api/console/wf-runs/{run_id}")).json()
    assert run["trigger"] == "single_node"
    assert run["inputs"] == {"node_id": "template_1", "values": {"start.q": "값"}}

    branch = await api.post(
        f"{base}/if_else_1/runs",
        json={"inputs": {"start.score": 70}, "response_mode": "blocking"},
    )
    assert branch.json()["status"] == "succeeded"
    branched = (await api.get(f"/api/console/wf-runs/{branch.json()['id']}")).json()
    assert [(item["node_id"], item["branch"]) for item in branched["node_runs"]] == [
        ("if_else_1", "case_hi")
    ]

    script.add(SHELL, fake_text("혼자 돌아 확인했다"))
    agent = await api.post(f"{base}/agent_1/runs", json={"inputs": {"template_1.output": "[x]"}})
    agent_run_id = agent.headers["x-run-id"]
    waiting = (await api.get(f"/api/console/wf-runs/{agent_run_id}")).json()
    assert waiting["status"] == "waiting_input"
    finished = await api.post(
        f"/api/console/wf-runs/{agent_run_id}/input",
        json={"decision": "approve", "response_mode": "blocking"},
    )
    body = finished.json()
    assert body["status"] == "succeeded"
    assert body["outputs"]["text"] == "혼자 돌아 확인했다"

    # 단일 노드 실행은 로그 목록에 기본으로 나오지 않는다(11.2).
    listed = (await api.get(f"/api/console/workflows/{workflow_id}/runs")).json()["items"]
    assert listed == []
    single = (
        await api.get(
            f"/api/console/workflows/{workflow_id}/runs", params={"trigger": "single_node"}
        )
    ).json()["items"]
    assert len(single) == 3


async def test_single_node_run_rejects_bad_requests(api: AsyncClient) -> None:
    agent_id = await make_agent(api)
    flow = single_flow(agent_id)
    # 다른 노드의 오류는 단일 실행을 막지 않는다.
    flow["nodes"][5]["config"] = {"outputs": [{"name": "v", "var": "nowhere.value"}]}
    created = await api.post("/api/console/workflows", json={"name": "깨진 초안"})
    workflow_id = created.json()["id"]
    current = (await api.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    await api.put(
        f"/api/console/workflows/{workflow_id}/draft",
        json={
            "graph": flow,
            "env_vars": [],
            "conversation_vars": [],
            "settings": current["settings"],
            "updated_at": current["updated_at"],
        },
    )
    base = f"/api/console/workflows/{workflow_id}/draft/nodes"

    ok = await api.post(
        f"{base}/template_1/runs", json={"inputs": {"start.q": "x"}, "response_mode": "blocking"}
    )
    broken = await api.post(f"{base}/end_2/runs", json={})
    missing = await api.post(f"{base}/nope/runs", json={})
    start = await api.post(f"{base}/start/runs", json={})
    bad = await api.post(
        f"{base}/template_1/runs",
        json={
            "inputs": {
                "template_1.output": "x",
                "env.KEY": "x",
                "start.nope": 1,
                "start.q.deep": "x",
                "아님": 1,
            }
        },
    )

    assert ok.json()["status"] == "succeeded"
    assert broken.status_code == 422 and broken.json()["error"]["code"] == "draft_invalid"
    assert broken.json()["error"]["details"][0]["node_id"] == "end_2"
    assert missing.status_code == 404
    assert start.status_code == 422 and start.json()["error"]["code"] == "node_not_runnable"
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_inputs"
    assert set(bad.json()["error"]["details"]) == {
        "template_1.output",
        "env.KEY",
        "start.nope",
        "start.q.deep",
        "아님",
    }


async def test_single_node_run_converts_file_ids_for_file_variables(
    app: FastAPI, api: AsyncClient, script: Any
) -> None:
    agent_id = await make_agent(api, approval_mode="off")
    flow = graph(
        [
            node("start", "start", {"fields": [{"name": "docs", "type": "file_list"}]}),
            node(
                "agent_1",
                "agent",
                {"agent_id": agent_id, "instruction": "파일을 본다", "files_var": "start.docs"},
            ),
            node("end_1", "end", {"outputs": [{"name": "v", "var": "agent_1.text"}]}),
        ],
        "start>agent_1",
        "agent_1>end_1",
    )
    workflow_id = await make_workflow(api, flow)
    uploaded = await api.post(
        f"/api/console/workflows/{workflow_id}/files",
        files={"file": ("메모.txt", "메모".encode(), "text/plain")},
        headers={"X-Requested-With": "fetch"},
    )
    file_id = uploaded.json()["id"]
    script.add(fake_text("파일을 봤다"))

    response = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/nodes/agent_1/runs",
        json={"inputs": {"start.docs": [file_id]}, "response_mode": "blocking"},
    )

    body = response.json()
    assert body["status"] == "succeeded", body
    [doc] = body["inputs"]["values"]["start.docs"]
    assert doc["file_id"] == file_id and doc["source"] == "wf_file"
    unknown = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/nodes/agent_1/runs",
        json={"inputs": {"start.docs": [str(uuid.uuid4())]}},
    )
    assert unknown.status_code == 422 and unknown.json()["error"]["code"] == "file_not_found"


@asynccontextmanager
async def serve(
    settings: Settings, script: Any, monkeypatch: pytest.MonkeyPatch, *, first: bool = False
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    application = create_app(settings)
    application.state.llm = FakeAdapter([script] * 1000)
    try:
        async with application.router.lifespan_context(application):
            async with AsyncClient(
                transport=ASGITransport(app=application), base_url=BASE_URL
            ) as client:
                if first:
                    setup = await client.post(
                        "/api/console/setup", json={"email": EMAIL, "password": PASSWORD}
                    )
                    assert setup.status_code == 201, setup.text
                login = await client.post(
                    "/api/console/auth/login", json={"email": EMAIL, "password": PASSWORD}
                )
                assert login.status_code == 200, login.text
                yield application, client
    finally:
        configure_logging("INFO")


async def test_waiting_run_and_child_survive_a_restart_and_resume(
    short_settings: Settings, script: Any, monkeypatch: pytest.MonkeyPatch, sse: Events
) -> None:
    async with serve(short_settings, script, monkeypatch, first=True) as (_, client):
        agent_id = await make_agent(client)
        workflow_id = await make_workflow(client, agent_flow(agent_id))
        script.add(SHELL)
        run_id, _, run = await start_waiting(client, sse, workflow_id)
        child_id = run["pending_interaction"]["child_run_id"]

    script.add(fake_text("재기동 뒤에 끝냈다"))
    async with serve(short_settings, script, monkeypatch) as (_, client):
        kept = (await client.get(f"/api/console/wf-runs/{run_id}")).json()
        assert kept["status"] == "waiting_input"
        assert (await client.get(f"/api/console/runs/{child_id}")).json()[
            "status"
        ] == "waiting_input"

        finished = await client.post(
            f"/api/console/wf-runs/{run_id}/input",
            json={"decision": "approve", "response_mode": "blocking"},
        )

        assert finished.json()["status"] == "succeeded", finished.json()
        assert finished.json()["outputs"] == {"answer": "재기동 뒤에 끝냈다"}
