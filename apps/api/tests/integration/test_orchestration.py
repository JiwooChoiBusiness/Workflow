"""지휘자 에이전트가 call_agent로 다른 에이전트를 부른다(오케스트레이션 O-A1·O-A2·O-A7).

가짜 어댑터 각본(tests/fixtures/fake_llm/orchestration_*.json)으로 부모·자식 턴을 차례로 낸다.
동시 런이 1이라 턴은 부모 → 자식 → 부모 순서로 쓰인다.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import TypeAdapter
from sqlalchemy import select

from app.agent.task_service import AgentTaskSpec
from app.config import Settings
from app.db.models import ChatSession, Run, SessionFile
from app.llm.fake_adapter import fake_text, fake_tool_call
from app.llm.types import ChatRequest, ModelEvent, TextBlock, ToolResultBlock
from app.services.runs import RunService
from app.services.tasks import TaskService

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fake_llm"
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
TURNS = TypeAdapter(list[list[ModelEvent]])
Events = Callable[[str], list[dict[str, Any]]]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


def load_turns(name: str) -> list[list[ModelEvent]]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return TURNS.validate_python(data["turns"])


async def make_agent(
    client: AsyncClient,
    name: str,
    slug: str,
    *,
    description: str = "",
    publish: bool = False,
    **draft: Any,
) -> dict[str, Any]:
    created = await client.post(
        "/api/console/agents", json={"name": name, "slug": slug, "description": description}
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    if draft:
        await save_draft(client, agent["id"], **draft)
    if publish:
        published = await client.post(f"/api/console/agents/{agent['id']}/publish", json={})
        assert published.status_code == 201, published.text
    return agent


async def save_draft(client: AsyncClient, agent_id: str, **changes: Any) -> dict[str, Any]:
    current = (await client.get(f"/api/console/agents/{agent_id}/draft")).json()
    body = {key: current[key] for key in DRAFT_FIELDS}
    body["env_vars"] = []
    body.update(changes)
    saved = await client.put(f"/api/console/agents/{agent_id}/draft", json=body)
    assert saved.status_code == 200, saved.text
    return saved.json()


async def preview_session(client: AsyncClient, agent_id: str) -> str:
    response = await client.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def ref(agent: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"agent_id": agent["id"], **fields}


async def wait_for(check: Callable[[], Any], timeout: float = 15) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("조건이 시간 안에 맞지 않았다")


def system_of(request: ChatRequest) -> str:
    return request.system


def tool_names(request: ChatRequest) -> list[str]:
    return [tool.name for tool in request.tools]


def first_user_text(request: ChatRequest) -> str:
    message = next(message for message in request.messages if message.role == "user")
    return "".join(block.text for block in message.blocks if isinstance(block, TextBlock))


def results_of(request: ChatRequest) -> dict[str, ToolResultBlock]:
    return {
        block.tool_use_id: block
        for message in request.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    }


async def test_conductor_calls_two_agents_and_combines(
    logged_in: AsyncClient, app: FastAPI, script: Any, sse: Events
) -> None:
    client = logged_in
    researcher = await make_agent(
        client, "조사 담당", "researcher", description="근거를 모아 정리한다", publish=True
    )
    writer = await make_agent(client, "작성", "writer", publish=True)
    conductor = await make_agent(client, "지휘자", "conductor")
    saved = await save_draft(
        client,
        conductor["id"],
        tools=["files", "ask_user"],
        sub_agents=[ref(researcher), ref(writer)],
    )
    # 설명이 빈 하위 에이전트는 저장 응답의 경고로 나온다.
    assert [(item["code"], item["agent_id"]) for item in saved["warnings"]] == [
        ("agent_description_empty", writer["id"])
    ]
    session_id = await preview_session(client, conductor["id"])
    script.add(*load_turns("orchestration_conductor.json"))

    events = sse(
        (
            await client.post(
                f"/api/console/sessions/{session_id}/messages", json={"text": "X를 조사해 써 줘"}
            )
        ).text
    )

    types = [event["type"] for event in events]
    assert types[-1] == "run.completed" and events[-1]["data"]["status"] == "succeeded"
    assert types.count("child_run.started") == 2 and types.count("child_run.completed") == 2
    assert "run.waiting_input" not in types
    # 하위 호출 대기 동안 run.completed 없이 같은 스트림에서 이어진다.
    assert types.count("run.completed") == 1
    assert len({event["run_id"] for event in events}) == 1
    started = [event["data"] for event in events if event["type"] == "child_run.started"]
    assert [item["agent_slug"] for item in started] == ["researcher", "writer"]
    assert {item["version"] for item in started} == {"published"}
    completed = {
        event["data"]["tool_call_id"]: event["data"]
        for event in events
        if event["type"] == "tool_call.completed"
    }
    assert completed["call_research"]["child_run_id"] == started[0]["child_run_id"]
    assert completed["call_research"]["status"] == "ok"
    final = next(event for event in reversed(events) if event["type"] == "message.completed")
    assert "합쳤다" in final["data"]["text"]

    parent_id = events[0]["run_id"]
    children = (await client.get("/api/console/runs", params={"parent_run_id": parent_id})).json()
    assert {item["id"] for item in children["items"]} == {
        started[0]["child_run_id"],
        started[1]["child_run_id"],
    }
    for item in children["items"]:
        assert item["parent_run_id"] == parent_id and item["depth"] == 1
        assert item["status"] == "succeeded" and item["session_source"] == "task"
        assert item["usage"]["input_tokens"] > 0
    listed = (await client.get("/api/console/runs", params={"session_id": session_id})).json()
    assert listed["items"][0]["child_count"] == 2

    requests: list[ChatRequest] = app.state.llm.requests
    parent_first, child_first = requests[0], requests[1]
    assert "call_agent" in tool_names(parent_first)
    call_tool = next(tool for tool in parent_first.tools if tool.name == "call_agent")
    assert call_tool.input_schema["properties"]["agent"]["enum"] == ["researcher", "writer"]
    assert "<available_agents>" in parent_first.system and "# 하위 에이전트" in parent_first.system
    # 자식에는 ask_user가 없고 과업 블록에 상위 에이전트 줄이 있다.
    assert "ask_user" not in tool_names(child_first)
    assert "상위 에이전트 지휘자" in child_first.system
    assert first_user_text(child_first).startswith("주제 X의 근거를 세 가지")
    resumed = results_of(requests[2])["call_research"]
    assert resumed.content.startswith("[하위 에이전트 researcher(조사 담당) 완료: succeeded")
    assert "근거 A, 근거 B, 근거 C" in resumed.content

    async with app.state.db.read() as session:
        child_session = (
            await session.execute(
                select(ChatSession).where(
                    ChatSession.id == uuid.UUID(started[0]["child_session_id"])
                )
            )
        ).scalar_one()
    assert child_session.title.startswith("지휘자 → 주제 X의")
    assert child_session.meta["parent_run_id"] == parent_id
    assert child_session.meta["session_mode"] == "ephemeral"


async def test_child_approval_flows_and_auto_approve_goes_up(
    logged_in: AsyncClient, app: FastAPI, script: Any, sse: Events
) -> None:
    client = logged_in
    shellbot = await make_agent(
        client,
        "셸 담당",
        "shellbot",
        description="셸로 확인한다",
        publish=True,
        approval_mode="all_shell",
    )
    conductor = await make_agent(client, "지휘자", "conductor")
    await save_draft(client, conductor["id"], sub_agents=[ref(shellbot)])
    session_id = await preview_session(client, conductor["id"])
    script.add(*load_turns("orchestration_child_shell.json"))

    stream = asyncio.create_task(
        client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "확인해 줘"})
    )

    async def child_waiting() -> Any:
        detail = (await client.get(f"/api/console/sessions/{session_id}")).json()
        child = (detail.get("active_run") or {}).get("child")
        if child is None:
            return None
        run = (await client.get(f"/api/console/runs/{child['run_id']}")).json()
        return (detail, run) if run["status"] == "waiting_input" else None

    detail, child_run = await wait_for(child_waiting)
    parent = detail["active_run"]
    # 밖에서 보면 부모는 call_agent를 실행하는 중이다(6.4).
    assert parent["status"] == "running" and parent["pending_interaction"] is None
    assert parent["child"]["agent_slug"] == "shellbot"
    assert parent["child"]["session_id"] == child_run["session_id"]
    assert child_run["pending_interaction"]["kind"] == "approval"
    assert child_run["parent_run_id"] == parent["id"]

    busy = await client.post(
        f"/api/console/runs/{parent['id']}/input", json={"decision": "approve"}
    )
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "child_in_progress"
    assert busy.json()["error"]["details"]["child_run_id"] == child_run["id"]
    again = await client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "또"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "run_in_progress"
    # 자식 세션의 스트림은 승인 대기까지의 기록을 주고 닫는다(1단계 그대로).
    child_events = sse(
        (
            await client.get(
                f"/api/console/sessions/{child_run['session_id']}/events",
                params={"run_id": child_run["id"]},
            )
        ).text
    )
    assert child_events[-1]["type"] == "run.completed"
    assert child_events[-1]["data"]["status"] == "waiting_input"

    approved = await client.post(
        f"/api/console/runs/{child_run['id']}/input",
        json={"decision": "approve", "session_auto_approve": True},
    )
    assert approved.status_code == 202, approved.text
    events = sse((await asyncio.wait_for(stream, 20)).text)

    assert events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["status"] == "succeeded"
    completed = next(event["data"] for event in events if event["type"] == "child_run.completed")
    assert completed["status"] == "succeeded" and completed["steps"] == 2
    # 자식 카드에서 켠 자동 승인은 부모 대화(세션)에도 켜진다(3.5).
    parent_session = (await client.get(f"/api/console/sessions/{session_id}")).json()
    assert parent_session["approval_override"] == "off"


async def test_header_auto_approve_reaches_open_child_sessions(
    logged_in: AsyncClient, app: FastAPI, script: Any
) -> None:
    client = logged_in
    shellbot = await make_agent(
        client, "셸 담당", "shellbot", publish=True, approval_mode="all_shell"
    )
    conductor = await make_agent(client, "지휘자", "conductor")
    await save_draft(client, conductor["id"], sub_agents=[ref(shellbot)])
    session_id = await preview_session(client, conductor["id"])
    script.add(*load_turns("orchestration_child_shell.json"))
    stream = asyncio.create_task(
        client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "확인"})
    )

    async def child_session() -> Any:
        detail = (await client.get(f"/api/console/sessions/{session_id}")).json()
        child = (detail.get("active_run") or {}).get("child")
        if child is None:
            return None
        run = (await client.get(f"/api/console/runs/{child['run_id']}")).json()
        return child if run["status"] == "waiting_input" else None

    child = await wait_for(child_session)
    patched = await client.patch(
        f"/api/console/sessions/{session_id}", json={"approval_override": "off"}
    )
    assert patched.status_code == 200
    detail = (await client.get(f"/api/console/sessions/{child['session_id']}")).json()
    assert detail["approval_override"] == "off"

    await client.post(f"/api/console/runs/{child['run_id']}/input", json={"decision": "approve"})
    events = (await asyncio.wait_for(stream, 20)).text
    assert '"status":"succeeded"' in events.rsplit("run.completed", 1)[1]


async def test_json_result_reaches_the_parent(
    logged_in: AsyncClient, app: FastAPI, script: Any, sse: Events
) -> None:
    client = logged_in
    schema = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["title", "count"],
        "additionalProperties": False,
    }
    extractor = await make_agent(client, "추출", "extractor", description="뽑는다", publish=True)
    conductor = await make_agent(client, "지휘자", "conductor")
    await save_draft(
        client,
        conductor["id"],
        sub_agents=[ref(extractor, result={"format": "json", "schema": schema})],
    )
    session_id = await preview_session(client, conductor["id"])
    script.add(*load_turns("orchestration_child_json.json"))

    events = sse(
        (
            await client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "뽑아"})
        ).text
    )

    assert events[-1]["data"]["status"] == "succeeded"
    requests: list[ChatRequest] = app.state.llm.requests
    child_request = requests[1]
    assert "submit_result" in tool_names(child_request)
    result = results_of(requests[2])["call_extract"]
    body = result.content.split("---\n", 1)[1]
    assert json.loads(body) == {"title": "분기 보고서", "count": 3}
    started = next(event["data"] for event in events if event["type"] == "child_run.started")
    assert started["result_format"] == "json"


async def test_service_api_chat_includes_child_calls(
    logged_in: AsyncClient,
    app: FastAPI,
    script: Any,
    make_client: Callable[[], AsyncClient],
    sse: Events,
) -> None:
    client = logged_in
    researcher = await make_agent(
        client, "조사 담당", "researcher", description="근거", publish=True
    )
    writer = await make_agent(client, "작성", "writer", description="쓴다", publish=True)
    conductor = await make_agent(
        client, "지휘자", "conductor", sub_agents=[ref(researcher), ref(writer)], publish=True
    )
    created = await client.post(
        f"/api/console/agents/{conductor['id']}/api-keys", json={"name": "외부"}
    )
    key = {"Authorization": f"Bearer {created.json()['key']}"}
    outsider = await make_agent(client, "무관", "outsider", publish=True)
    other = await client.post(
        f"/api/console/agents/{outsider['id']}/api-keys", json={"name": "다른"}
    )
    other_key = {"Authorization": f"Bearer {other.json()['key']}"}
    script.add(*load_turns("orchestration_conductor.json"))
    api = make_client()

    response = await api.post(
        "/api/v1/chat-messages",
        json={"query": "조사해 써 줘", "response_mode": "blocking", "user": "u1"},
        headers=key,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded" and "합쳤다" in body["answer"]
    children = (
        await client.get("/api/console/runs", params={"parent_run_id": body["run_id"]})
    ).json()["items"]
    assert len(children) == 2
    child_id = children[0]["id"]
    # 자식 과업 런은 키 범위로 열린다(부모 사슬의 뿌리 세션이 키의 대화다, 6.3).
    task = await api.get(f"/api/v1/agent-tasks/{child_id}", headers=key, params={"user": "u1"})
    assert task.status_code == 200, task.text
    assert task.json()["status"] == "succeeded"
    assert (await api.get(f"/api/v1/runs/{child_id}", headers=key)).status_code == 200
    assert (await api.get(f"/api/v1/agent-tasks/{child_id}", headers=other_key)).status_code == 404
    assert (
        await api.get(f"/api/v1/agent-tasks/{child_id}", headers=key, params={"user": "u2"})
    ).status_code == 404

    # streaming은 하위 호출 동안 끊기지 않고 한 응답으로 자식 이벤트와 최종 답까지 온다(O-A7).
    script.add(*load_turns("orchestration_conductor.json"))
    streamed = await api.post(
        "/api/v1/chat-messages", json={"query": "다시 조사해 써 줘", "user": "u1"}, headers=key
    )

    assert streamed.status_code == 200
    events = sse(streamed.text)
    types = [event["type"] for event in events]
    assert types[0] == "run.queued" and types.count("run.completed") == 1
    assert types[-1] == "run.completed" and events[-1]["data"]["status"] == "succeeded"
    assert types.count("child_run.started") == 2 and types.count("child_run.completed") == 2
    assert len({event["run_id"] for event in events}) == 1


async def test_grandchild_calls_respect_the_root_budget_and_depth(
    logged_in: AsyncClient, app: FastAPI, script: Any
) -> None:
    client = logged_in
    leaf = await make_agent(client, "잎", "leaf", description="끝", publish=True)
    grand = await make_agent(
        client, "손자", "grand", description="손자", sub_agents=[ref(leaf)], publish=True
    )
    middle = await make_agent(
        client, "중간", "middle", description="중간", sub_agents=[ref(grand)], publish=True
    )
    other = await make_agent(client, "다른", "other", description="다른", publish=True)
    conductor = await make_agent(client, "지휘자", "conductor")
    limits = (await client.get(f"/api/console/agents/{conductor['id']}/draft")).json()["limits"]
    await save_draft(
        client,
        conductor["id"],
        limits={**limits, "max_child_calls": 2},
        sub_agents=[ref(middle), ref(other)],
    )
    session_id = await preview_session(client, conductor["id"])
    seen: list[ChatRequest] = []

    def recorded(events: list[ModelEvent]) -> Callable[[ChatRequest], list[ModelEvent]]:
        def turn(request: ChatRequest) -> list[ModelEvent]:
            seen.append(request)
            return events

        return turn

    script.add(
        recorded(
            fake_tool_call("call_agent", {"agent": "middle", "task": "중간 일"}, call_id="r1")
        ),
        recorded(fake_tool_call("call_agent", {"agent": "grand", "task": "손자 일"}, call_id="m1")),
        recorded(fake_text("손자 결과")),
        recorded(fake_tool_call("call_agent", {"agent": "grand", "task": "또"}, call_id="m2")),
        recorded(fake_text("중간 결과")),
        recorded(fake_tool_call("call_agent", {"agent": "other", "task": "다른 일"}, call_id="r2")),
        recorded(fake_text("지휘자 끝")),
    )

    response = await client.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "깊게"}
    )

    assert '"status":"succeeded"' in response.text.rsplit("run.completed", 1)[1]
    root, middle_req, grand_req = seen[0], seen[1], seen[2]
    # 뿌리 한도 2에서 중간의 상한은 남은 수 1이다. 손자(깊이 2)에는 도구가 없다.
    assert "call_agent" in tool_names(middle_req)
    assert "call_agent" not in tool_names(grand_req)
    assert "하위 에이전트를 부를 수 없는 깊이" in grand_req.system
    assert "call_agent" in tool_names(root)
    m2 = results_of(seen[4])["m2"]
    assert m2.is_error and m2.content.startswith("child_limit")
    r2 = results_of(seen[6])["r2"]
    assert r2.is_error and r2.content.startswith("child_limit")
    async with app.state.db.read() as session:
        runs = (await session.execute(select(Run).where(Run.parent_run_id.is_not(None)))).scalars()
        tree = list(runs)
    assert len(tree) == 2
    middle_run = next(
        run for run in tree if run.trigger == "task" and run.limits["max_child_calls"] == 1
    )
    assert middle_run.input is not None and middle_run.input["correlation"]["depth"] == 1


async def test_files_are_staged_once_and_results_come_back(
    logged_in: AsyncClient, app: FastAPI, script: Any, sse: Events
) -> None:
    client = logged_in
    researcher = await make_agent(
        client, "조사 담당", "researcher", description="근거", publish=True, approval_mode="off"
    )
    conductor = await make_agent(client, "지휘자", "conductor")
    await save_draft(client, conductor["id"], sub_agents=[ref(researcher)])
    session_id = await preview_session(client, conductor["id"])
    seen: list[ChatRequest] = []

    def child_turn(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return fake_tool_call(
            "write_file", {"path": "outputs/result.md", "content": "자식 결과"}, call_id="cw"
        )

    script.add(
        fake_tool_call("write_file", {"path": "outputs/data.txt", "content": "원본"}, call_id="pw"),
        fake_tool_call(
            "call_agent",
            {"agent": "researcher", "task": "data.txt를 읽고 정리", "files": ["outputs/data.txt"]},
            call_id="pc",
        ),
        child_turn,
        fake_text("자식 끝"),
        fake_text("부모 끝"),
    )

    events = sse(
        (await client.post(f"/api/console/sessions/{session_id}/messages", json={"text": "f"})).text
    )

    assert events[-1]["data"]["status"] == "succeeded"
    assert "task/files/data.txt" in first_user_text(seen[0])
    requests: list[ChatRequest] = app.state.llm.requests
    child_run_id = next(e["data"] for e in events if e["type"] == "child_run.started")[
        "child_run_id"
    ]
    copied = f"agents/researcher/{child_run_id}/outputs/result.md"
    result = results_of(requests[4])["pc"]
    assert f"- {copied} (" in result.content
    workspace = app.state.settings.data_dir / "workspaces" / session_id
    assert (workspace / copied).read_text(encoding="utf-8") == "자식 결과"
    files = (await client.get(f"/api/console/sessions/{session_id}/files")).json()["items"]
    paths = [item["path"] for item in files]
    # 부모가 만든 파일을 넘겨도 세션 파일 목록에는 한 번만 있다(4.1의 3번).
    assert paths.count("outputs/data.txt") == 1
    assert copied in paths
    async with app.state.db.read() as session:
        staged = (
            await session.execute(select(SessionFile).where(SessionFile.path == "outputs/data.txt"))
        ).scalar_one()
    assert staged.storage_key is not None and "/.calls/pc/" in staged.storage_key


async def test_wait_does_not_return_while_waiting_for_a_child(
    logged_in: AsyncClient, app: FastAPI, script: Any
) -> None:
    client = logged_in
    shellbot = await make_agent(
        client, "셸 담당", "shellbot", publish=True, approval_mode="all_shell"
    )
    conductor = await make_agent(
        client, "지휘자", "conductor", sub_agents=[ref(shellbot)], publish=True
    )
    state = app.state
    runs = RunService(
        state.db,
        state.settings,
        bus=state.bus,
        supervisor=state.supervisor,
        storage=state.storage,
        backend=state.sandbox,
    )
    tasks = TaskService(state.db, state.settings, runs=runs, bus=state.bus)
    workspace_id = uuid.UUID((await client.get("/api/console/auth/me")).json()["workspace"]["id"])
    script.add(*load_turns("orchestration_child_shell.json"))

    run_id = await tasks.start(
        AgentTaskSpec(
            workspace_id=workspace_id, agent_id=uuid.UUID(conductor["id"]), instruction="확인"
        )
    )
    waiter = asyncio.create_task(tasks.wait(run_id, timeout=30))

    async def child_waiting() -> Any:
        run = (await client.get(f"/api/console/runs/{run_id}")).json()
        child = run.get("child")
        if child is None:
            return None
        child_run = (await client.get(f"/api/console/runs/{child['run_id']}")).json()
        return child if child_run["status"] == "waiting_input" else None

    child = await wait_for(child_waiting)
    await asyncio.sleep(0.3)
    # 하위 호출 대기는 HITL이 아니다. wait()는 돌아오지 않는다(3.2).
    assert not waiter.done()
    view = await tasks.view(workspace_id, run_id)
    assert view.status == "running" and view.pending_interaction is None
    await client.post(f"/api/console/runs/{child['run_id']}/input", json={"decision": "approve"})
    result = await asyncio.wait_for(waiter, 20)
    assert result.status == "succeeded"


async def test_webapp_scope_follows_the_parent_chain(
    logged_in: AsyncClient, app: FastAPI, script: Any, sse: Events
) -> None:
    client = logged_in
    researcher = await make_agent(
        client, "조사 담당", "researcher", description="근거", publish=True
    )
    writer = await make_agent(client, "작성", "writer", description="쓴다", publish=True)
    conductor = await make_agent(
        client, "지휘자", "conductor", sub_agents=[ref(researcher), ref(writer)], publish=True
    )
    patched = await client.patch(
        f"/api/console/agents/{conductor['id']}", json={"webapp_enabled": True}
    )
    assert patched.status_code == 200
    created = await client.post("/api/web/apps/conductor/sessions", json={})
    assert created.status_code == 201, created.text
    web_session = created.json()["id"]
    script.add(*load_turns("orchestration_conductor.json"))

    events = sse(
        (
            await client.post(
                f"/api/web/sessions/{web_session}/messages", json={"text": "조사해 써 줘"}
            )
        ).text
    )

    assert events[-1]["data"]["status"] == "succeeded"
    started = next(event["data"] for event in events if event["type"] == "child_run.started")
    child_session = started["child_session_id"]
    # 자식 세션(task)은 웹앱 범위 밖이지만 부모 사슬의 뿌리가 웹앱 세션이라 열린다(6.3).
    detail = await client.get(f"/api/web/sessions/{child_session}")
    assert detail.status_code == 200 and detail.json()["source"] == "task"
    replay = sse(
        (
            await client.get(
                f"/api/web/sessions/{child_session}/events",
                params={"run_id": started["child_run_id"]},
            )
        ).text
    )
    assert replay[-1]["type"] == "run.completed"
    assert (await client.get(f"/api/web/sessions/{child_session}/messages")).status_code == 200
    # 미리보기에서 부른 자식 세션은 웹앱 범위로 열리지 않는다.
    preview = await preview_session(client, conductor["id"])
    script.add(*load_turns("orchestration_conductor.json"))
    preview_events = sse(
        (await client.post(f"/api/console/sessions/{preview}/messages", json={"text": "x"})).text
    )
    other_child = next(
        event["data"] for event in preview_events if event["type"] == "child_run.started"
    )
    hidden = await client.get(f"/api/web/sessions/{other_child['child_session_id']}")
    assert hidden.status_code == 404
