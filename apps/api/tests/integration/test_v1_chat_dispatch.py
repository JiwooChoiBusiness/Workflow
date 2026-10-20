"""서비스 API의 키 대상 분기(2단계 설계 6.3, 12장, A7).

/chat-messages·/conversations·/messages·/files·/info·/parameters는 경로 하나를 키 대상에 따라
나눈다. 에이전트 키는 1단계 세션 경로 그대로, 챗플로우 키는 그 키로 만든 챗플로우 대화로
간다. 경로가 받지 않는 대상의 키는 400 wrong_target이다. 챗플로우 대화는 키와 user로 범위가
정해진다.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
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
SHELL = [*fake_tool_call("shell", {"command": "echo api"}), fake_end("tool_use")]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


@pytest.fixture
def api(make_client: Callable[[], AsyncClient]) -> AsyncClient:
    # 로그인 쿠키가 없는 클라이언트. 서비스 API는 키로만 인증한다.
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


async def publish(console: AsyncClient, graph: dict[str, Any], *, kind: str, name: str) -> str:
    created = await console.post("/api/console/workflows", json={"name": name, "kind": kind})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    current = (await console.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    saved = await console.put(
        f"/api/console/workflows/{workflow_id}/draft",
        json={
            "graph": graph,
            "env_vars": [],
            "conversation_vars": [],
            "settings": {
                **current["settings"],
                "webapp": {
                    "opening_statement": "무엇이든 물어보세요",
                    "suggested_questions": ["안녕"],
                },
            },
            "updated_at": current["updated_at"],
        },
    )
    assert saved.status_code == 200 and saved.json()["issues"] == [], saved.text
    published = await console.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text
    return workflow_id


def chat_graph(prefix: str = "챗") -> dict[str, Any]:
    return {
        "nodes": [
            node("start", "start", {"fields": [{"name": "tone", "type": "string"}]}),
            node(
                "llm_1",
                "llm",
                {
                    "prompt": [{"role": "user", "text": "{{ sys.query }} {{ start.tone }}"}],
                    "memory": {"enabled": True, "window_turns": 5},
                },
                300,
            ),
            node("answer_1", "answer", {"text": prefix + ": {{ llm_1.text }}"}, 600),
        ],
        "edges": chain("start", "llm_1", "answer_1"),
    }


def flow_graph() -> dict[str, Any]:
    return {
        "nodes": [
            node("start", "start", {"fields": [{"name": "name", "type": "string"}]}),
            node(
                "template_1",
                "template",
                {"template": "안녕, {{ name }}", "vars": [{"name": "name", "var": "start.name"}]},
                300,
            ),
            node("end_1", "end", {"outputs": [{"name": "greeting", "var": "template_1.output"}]}),
        ],
        "edges": chain("start", "template_1", "end_1"),
    }


async def workflow_key(console: AsyncClient, workflow_id: str, **body: Any) -> dict[str, str]:
    response = await console.post(
        f"/api/console/workflows/{workflow_id}/api-keys", json={"name": "자동화", **body}
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['key']}"}


async def agent_key(console: AsyncClient, **draft: Any) -> tuple[str, dict[str, str]]:
    agent = (await console.post("/api/console/agents", json={"name": "API 도우미"})).json()
    current = (await console.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {key: current[key] for key in AGENT_DRAFT_FIELDS}
    body.update({"env_vars": [], **draft})
    saved = await console.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    assert saved.status_code == 200, saved.text
    assert (
        await console.post(f"/api/console/agents/{agent['id']}/publish", json={})
    ).status_code == 201
    response = await console.post(
        f"/api/console/agents/{agent['id']}/api-keys", json={"name": "자동화"}
    )
    assert response.status_code == 201, response.text
    return agent["id"], {"Authorization": f"Bearer {response.json()['key']}"}


def error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def test_chat_messages_follow_the_key_target(
    logged_in: AsyncClient, api: AsyncClient, script: Any, sse: Events
) -> None:
    _, agent = await agent_key(logged_in, approval_mode="off")
    chat_id = await publish(logged_in, chat_graph(), kind="chatflow", name="챗 상담")
    chat = await workflow_key(logged_in, chat_id)
    flow_id = await publish(logged_in, flow_graph(), kind="workflow", name="인사")
    flow = await workflow_key(logged_in, flow_id)
    script.add(fake_text("에이전트 답"), [*fake_text("챗플로우 답", chunk_size=3), fake_end()])

    by_agent = await api.post("/api/v1/chat-messages", headers=agent, json={"query": "안녕"})
    by_chat = await api.post(
        "/api/v1/chat-messages", headers=chat, json={"query": "안녕", "user": "u-1"}
    )
    by_flow = await api.post("/api/v1/chat-messages", headers=flow, json={"query": "안녕"})

    # 에이전트 키는 1단계 세션 경로(run.* 이벤트, 세션 봉투) 그대로다.
    agent_events = sse(by_agent.text)
    assert agent_events[0]["type"] == "run.queued" and agent_events[-1]["type"] == "run.completed"
    assert "x-session-id" in by_agent.headers
    assert all("conversation_id" not in event for event in agent_events)
    # 챗플로우 키는 workflow.* 이벤트이고 봉투에 conversation_id가 있다.
    chat_events = sse(by_chat.text)
    assert chat_events[0]["type"] == "workflow.queued"
    assert chat_events[-1]["type"] == "workflow.completed"
    conversation_id = by_chat.headers["x-conversation-id"]
    assert {event["conversation_id"] for event in chat_events} == {conversation_id}
    assert all(event["session_id"] is None for event in chat_events)
    answer = [event["data"]["text"] for event in chat_events if event["type"] == "answer.delta"]
    assert "".join(answer) == "챗: 챗플로우 답"
    assert error(by_flow) == (400, "wrong_target")
    assert by_flow.json()["error"]["details"] == {
        "key_target": "workflow",
        "expected": ["chatflow"],
    }


async def test_chatflow_conversations_messages_and_blocking_results(
    logged_in: AsyncClient, api: AsyncClient, script: Any
) -> None:
    chat_id = await publish(logged_in, chat_graph(), kind="chatflow", name="챗 상담")
    key = await workflow_key(logged_in, chat_id)
    other_key = await workflow_key(logged_in, chat_id)
    script.add(fake_text("첫째"), fake_text("둘째"))

    first = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={
            "query": "질문 하나",
            "user": "u-1",
            "inputs": {"tone": "짧게"},
            "response_mode": "blocking",
        },
    )

    assert first.status_code == 200, first.text
    result = first.json()
    assert set(result) == {
        "conversation_id",
        "message_id",
        "run_id",
        "answer",
        "usage",
        "status",
        "pending_interaction",
        "error",
    }
    assert (result["status"], result["answer"]) == ("succeeded", "챗: 첫째")
    conversation_id = result["conversation_id"]
    second = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={
            "query": "질문 둘",
            "user": "u-1",
            "conversation_id": conversation_id,
            "response_mode": "blocking",
        },
    )
    assert second.json()["conversation_id"] == conversation_id
    # 같은 키의 다른 사용자, 같은 워크플로우의 다른 키에는 이 대화가 없다.
    for headers, params in ((key, {"user": "u-2"}), (other_key, {"user": "u-1"})):
        missing = await api.get(
            f"/api/v1/conversations/{conversation_id}", headers=headers, params=params
        )
        assert missing.status_code == 404
    hijack = await api.post(
        "/api/v1/chat-messages",
        headers=other_key,
        json={"query": "끼어들기", "conversation_id": conversation_id, "response_mode": "blocking"},
    )
    assert hijack.status_code == 404

    listed = (await api.get("/api/v1/conversations", headers=key, params={"user": "u-1"})).json()
    assert [item["id"] for item in listed["items"]] == [conversation_id]
    item = listed["items"][0]
    assert (item["name"], item["external_user"], item["message_count"]) == ("질문 하나", "u-1", 4)
    assert "wf_version_id" in item and "agent_version_id" not in item
    assert (await api.get("/api/v1/conversations", headers=other_key)).json()["items"] == []
    detail = (
        await api.get(
            f"/api/v1/conversations/{conversation_id}", headers=key, params={"user": "u-1"}
        )
    ).json()
    assert detail["active_run"] is None and detail["last_run"]["id"] == second.json()["run_id"]
    messages = (
        await api.get(
            "/api/v1/messages",
            headers=key,
            params={"conversation_id": conversation_id, "user": "u-1", "limit": 3},
        )
    ).json()
    assert [(m["role"], m["text"]) for m in messages["items"]] == [
        ("assistant", "챗: 첫째"),
        ("user", "질문 둘"),
        ("assistant", "챗: 둘째"),
    ]
    assert messages["has_more"] is True and messages["next_cursor"] == "2"
    earlier = (
        await api.get(
            "/api/v1/messages",
            headers=key,
            params={"conversation_id": conversation_id, "cursor": messages["next_cursor"]},
        )
    ).json()
    assert [m["text"] for m in earlier["items"]] == ["질문 하나"]
    renamed = await api.patch(
        f"/api/v1/conversations/{conversation_id}", headers=key, json={"name": "이름 바꿈"}
    )
    assert renamed.json()["name"] == "이름 바꿈"
    run = (await api.get(f"/api/v1/workflows/run/{result['run_id']}", headers=key)).json()
    assert run["conversation_id"] == conversation_id and run["status"] == "succeeded"

    deleted = await api.delete(f"/api/v1/conversations/{conversation_id}", headers=key)

    assert deleted.status_code == 204
    assert (
        await api.get(f"/api/v1/conversations/{conversation_id}", headers=key)
    ).status_code == 404


async def test_chatflow_runs_wait_resume_and_stop_through_chat_message_paths(
    logged_in: AsyncClient, api: AsyncClient, script: Any, sse: Events
) -> None:
    agent = (await logged_in.post("/api/console/agents", json={"name": "셸 담당"})).json()
    current = (await logged_in.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {name: current[name] for name in AGENT_DRAFT_FIELDS}
    body.update({"env_vars": [], "approval_mode": "all_shell"})
    await logged_in.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    await logged_in.post(f"/api/console/agents/{agent['id']}/publish", json={})
    graph = {
        "nodes": [
            node("start", "start", {"fields": []}),
            node("agent_1", "agent", {"agent_id": agent["id"], "instruction": "{{ sys.query }}"}),
            node("answer_1", "answer", {"text": "끝: {{ agent_1.text }}"}, 600),
        ],
        "edges": chain("start", "agent_1", "answer_1"),
    }
    chat_id = await publish(logged_in, graph, kind="chatflow", name="승인 챗")
    key = await workflow_key(logged_in, chat_id)
    script.add(SHELL, fake_text("확인 끝"), SHELL)

    waiting = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={"query": "점검", "user": "u-1", "response_mode": "blocking"},
    )

    result = waiting.json()
    assert result["status"] == "waiting_input"
    assert result["pending_interaction"]["interaction"]["kind"] == "approval"
    run_id = result["run_id"]
    replay = sse((await api.get(f"/api/v1/chat-messages/{run_id}/events", headers=key)).text)
    assert replay[-1]["type"] == "workflow.completed"
    assert replay[-1]["data"]["status"] == "waiting_input"
    wrong_user = await api.post(
        f"/api/v1/chat-messages/{run_id}/input",
        headers=key,
        json={"decision": "approve", "user": "u-2"},
    )
    assert wrong_user.status_code == 404

    approved = await api.post(
        f"/api/v1/chat-messages/{run_id}/input",
        headers=key,
        json={"decision": "approve", "user": "u-1", "response_mode": "blocking"},
    )

    assert approved.status_code == 200, approved.text
    assert (approved.json()["status"], approved.json()["answer"]) == ("succeeded", "끝: 확인 끝")
    again = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={
            "query": "다시",
            "user": "u-1",
            "conversation_id": result["conversation_id"],
            "response_mode": "blocking",
        },
    )
    assert again.json()["status"] == "waiting_input"
    stopped = await api.post(
        f"/api/v1/chat-messages/{again.json()['run_id']}/stop", headers=key, json={"user": "u-1"}
    )
    assert stopped.json() == {"result": "success", "status": "cancelled"}


async def test_info_parameters_files_and_agent_only_paths(
    logged_in: AsyncClient, api: AsyncClient, script: Any
) -> None:
    agent_id, agent = await agent_key(logged_in)
    chat_id = await publish(logged_in, chat_graph(), kind="chatflow", name="챗 상담")
    chat = await workflow_key(logged_in, chat_id)

    agent_info = (await api.get("/api/v1/info", headers=agent)).json()
    chat_info = (await api.get("/api/v1/info", headers=chat)).json()
    chat_parameters = (await api.get("/api/v1/parameters", headers=chat)).json()

    assert agent_info["agent_id"] == agent_id
    assert (chat_info["workflow_id"], chat_info["kind"], chat_info["published"]) == (
        chat_id,
        "chatflow",
        True,
    )
    assert [field["name"] for field in chat_parameters["user_input_form"]] == ["tone"]
    assert chat_parameters["scopes"] == ["chat"]
    assert chat_parameters["opening_statement"] == "무엇이든 물어보세요"
    assert chat_parameters["suggested_questions"] == ["안녕"]
    assert (await api.get("/api/v1/parameters", headers=agent)).json()["scopes"] == [
        "chat",
        "tasks",
    ]
    # 에이전트에만 있는 경로는 챗플로우 키를 받지 않는다.
    task = await api.post(
        "/api/v1/agent-tasks", headers=chat, json={"instruction": "x", "response_mode": "blocking"}
    )
    assert error(task) == (400, "wrong_target")
    runs = await api.get(f"/api/v1/runs/{chat_id}", headers=chat)
    assert error(runs) == (400, "wrong_target")
    flows = await api.get("/api/v1/workflows/logs", headers=agent)
    assert error(flows) == (400, "wrong_target")

    script.add(fake_text("파일 받음"))
    created = await api.post(
        "/api/v1/chat-messages",
        headers=chat,
        json={"query": "시작", "response_mode": "blocking"},
    )
    conversation_id = created.json()["conversation_id"]
    upload = await api.post(
        "/api/v1/files/upload",
        headers=chat,
        files={"file": ("자료.txt", "내용".encode(), "text/plain")},
        data={"conversation_id": conversation_id},
    )
    assert upload.status_code == 201, upload.text
    uploaded = upload.json()
    assert uploaded["conversation_id"] == conversation_id and uploaded["filename"] == "자료.txt"
    loose = await api.post(
        "/api/v1/files/upload", headers=chat, files={"file": ("a.txt", b"a", "text/plain")}
    )
    assert loose.json()["conversation_id"] is None
    downloaded = await api.get(f"/api/v1/files/{uploaded['file_id']}/download", headers=chat)
    assert downloaded.status_code == 200 and downloaded.content.decode("utf-8") == "내용"
    # 대화에 붙지 않은 업로드와 에이전트 키의 내려받기는 범위 밖이다.
    assert (
        await api.get(f"/api/v1/files/{loose.json()['file_id']}/download", headers=chat)
    ).status_code == 404
    assert (
        await api.get(f"/api/v1/files/{uploaded['file_id']}/download", headers=agent)
    ).status_code == 404
    agent_upload = await api.post(
        "/api/v1/files/upload", headers=agent, files={"file": ("b.txt", b"b", "text/plain")}
    )
    assert agent_upload.status_code == 201 and agent_upload.json()["conversation_id"]
