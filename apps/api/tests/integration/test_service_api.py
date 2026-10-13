"""서비스 API(설계 6.4, 수용 조건 A9).

Bearer API 키로 부르고, 키에 묶인 에이전트의 발행 버전과 그 대화만 다룬다. 모델은 가짜 어댑터의
각본을 따르고 도구는 실제 로컬 백엔드에서 돈다.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ModelEvent, TextBlock
from app.security.rate_limit import API_REQUESTS_PER_MINUTE

FETCH = {"X-Requested-With": "fetch"}
DRAFT_FIELDS = (
    "instructions",
    "model",
    "tools",
    "limits",
    "sandbox",
    "approval_mode",
    "skill_version_ids",
    "file_ids",
    "updated_at",
)
Events = Callable[[str], list[dict[str, Any]]]


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


async def published_agent(
    client: AsyncClient, name: str = "Api Helper", *, publish: bool = True, **draft: Any
) -> dict[str, Any]:
    agent = (await client.post("/api/console/agents", json={"name": name})).json()
    if draft:
        current = (await client.get(f"/api/console/agents/{agent['id']}/draft")).json()
        body = {key: current[key] for key in DRAFT_FIELDS}
        body.update(draft)
        saved = await client.put(f"/api/console/agents/{agent['id']}/draft", json=body)
        assert saved.status_code == 200, saved.text
    if publish:
        response = await client.post(f"/api/console/agents/{agent['id']}/publish", json={})
        assert response.status_code == 201, response.text
    return agent


async def api_key(client: AsyncClient, agent_id: str, **fields: Any) -> dict[str, str]:
    response = await client.post(
        f"/api/console/agents/{agent_id}/api-keys", json={"name": "자동화", **fields}
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['key']}"}


def types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def user_text(request: ChatRequest) -> str:
    return "".join(
        block.text
        for message in request.messages
        if message.role == "user"
        for block in message.blocks
        if isinstance(block, TextBlock)
    )


async def test_requests_need_a_valid_bearer_key(logged_in: AsyncClient, api: AsyncClient) -> None:
    agent = await published_agent(logged_in)
    key = await api_key(logged_in, agent["id"])

    missing = await api.get("/api/v1/info")
    wrong = await api.get("/api/v1/info", headers={"Authorization": "Bearer sk-wf-nope"})
    cookie_only = await logged_in.get("/api/v1/info")
    ok = await api.get("/api/v1/info", headers=key)

    assert (missing.status_code, missing.json()["error"]["code"]) == (401, "unauthorized")
    assert (wrong.status_code, wrong.json()["error"]["code"]) == (401, "invalid_api_key")
    assert cookie_only.status_code == 401
    assert ok.status_code == 200
    info = ok.json()
    assert (info["agent_id"], info["published"], info["version_number"]) == (agent["id"], True, 1)
    parameters = (await api.get("/api/v1/parameters", headers=key)).json()
    assert parameters["approval_mode"] == "all_shell"
    assert parameters["limits"]["max_steps"] == 30
    assert parameters["user_input_form"] == []
    assert parameters["scopes"] == ["chat", "tasks"]


async def test_chat_messages_stream_and_conversations_belong_to_the_user(
    logged_in: AsyncClient, api: AsyncClient, script: Any, sse: Events
) -> None:
    agent = await published_agent(logged_in, approval_mode="off")
    key = await api_key(logged_in, agent["id"])
    seen: list[ChatRequest] = []

    def answer(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return fake_text("API 답")

    script.add(answer, fake_text("두 번째 답"))

    response = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={"query": "안녕", "user": "u-1", "inputs": {"lang": "ko"}},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse(response.text)
    conversation_id = response.headers["x-session-id"]
    assert events[0]["type"] == "run.queued" and events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["status"] == "succeeded"
    assert all(event["session_id"] == conversation_id for event in events)
    assert '입력 값: {"lang": "ko"}' in user_text(seen[0])
    assert "외부 사용자: u-1" in user_text(seen[0])

    blocking = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={
            "query": "또",
            "user": "u-1",
            "conversation_id": conversation_id,
            "response_mode": "blocking",
        },
    )
    result = blocking.json()
    assert blocking.status_code == 200
    assert (result["conversation_id"], result["status"], result["answer"]) == (
        conversation_id,
        "succeeded",
        "두 번째 답",
    )
    assert result["usage"]["input_tokens"] > 0 and result["message_id"] is not None

    mine = (await api.get("/api/v1/conversations", headers=key, params={"user": "u-1"})).json()
    others = (await api.get("/api/v1/conversations", headers=key, params={"user": "u-2"})).json()
    everyone = (await api.get("/api/v1/conversations", headers=key)).json()
    assert [item["id"] for item in mine["items"]] == [conversation_id]
    assert mine["items"][0]["external_user"] == "u-1"
    assert mine["items"][0]["message_count"] == 4
    assert others["items"] == [] and len(everyone["items"]) == 1
    hidden = await api.get(
        f"/api/v1/conversations/{conversation_id}", headers=key, params={"user": "u-2"}
    )
    assert hidden.status_code == 404
    wrong_user = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={"query": "끼어들기", "user": "u-2", "conversation_id": conversation_id},
    )
    assert wrong_user.status_code == 404
    # 콘솔 로그에도 API 세션으로 남는다(A10).
    logs = (await logged_in.get("/api/console/sessions", params={"source": "api"})).json()
    assert logs["items"][0]["external_user"] == "u-1"
    assert logs["items"][0]["api_key_id"] is not None


async def test_approval_reconnect_runs_and_stop(
    logged_in: AsyncClient, api: AsyncClient, script: Any, sse: Events
) -> None:
    agent = await published_agent(logged_in)  # 승인 모드 all_shell
    key = await api_key(logged_in, agent["id"])
    script.add(
        [*fake_tool_call("shell", {"command": "echo api"}, call_id="call_1"), fake_end("tool_use")],
        fake_text("실행했다"),
        [*fake_tool_call("shell", {"command": "echo x"}, call_id="call_2"), fake_end("tool_use")],
    )
    first = await api.post("/api/v1/chat-messages", headers=key, json={"query": "실행해"})
    events = sse(first.text)
    run_id = first.headers["x-run-id"]
    assert types(events)[-2:] == ["run.waiting_input", "run.completed"]
    assert events[-2]["data"]["kind"] == "approval"
    waiting = (await api.get(f"/api/v1/runs/{run_id}", headers=key)).json()
    assert waiting["status"] == "waiting_input"
    assert waiting["pending_interaction"]["tool_name"] == "shell"

    accepted = await api.post(
        f"/api/v1/chat-messages/{run_id}/input", headers=key, json={"decision": "approve"}
    )
    assert accepted.status_code == 202
    replay = sse(
        (
            await api.get(
                f"/api/v1/chat-messages/{run_id}/events",
                headers=key,
                params={"after": events[-1]["seq"]},
            )
        ).text
    )

    assert replay[0]["type"] == "interaction.resolved"
    assert replay[-1]["type"] == "run.completed" and replay[-1]["data"]["status"] == "succeeded"
    done = (await api.get(f"/api/v1/runs/{run_id}", headers=key)).json()
    assert done["status"] == "succeeded"

    conversation_id = first.headers["x-session-id"]
    second = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={
            "query": "또 실행해",
            "conversation_id": conversation_id,
            "response_mode": "blocking",
        },
    )
    pending = second.json()
    assert pending["status"] == "waiting_input"
    assert pending["pending_interaction"]["kind"] == "approval"
    stopped = await api.post(f"/api/v1/chat-messages/{pending['run_id']}/stop", headers=key)
    again = await api.post(f"/api/v1/chat-messages/{pending['run_id']}/stop", headers=key)

    assert stopped.status_code == 200
    assert stopped.json() == {"result": "success", "status": "cancelled"}
    assert again.status_code == 409


async def test_conversation_detail_rename_messages_paging_and_delete(
    logged_in: AsyncClient, api: AsyncClient, script: Any, settings: Settings
) -> None:
    agent = await published_agent(logged_in, approval_mode="off")
    key = await api_key(logged_in, agent["id"])
    script.add(*(fake_text(f"답 {index}") for index in range(3)))
    conversation_id = ""
    for index in range(3):
        body: dict[str, Any] = {"query": f"질문 {index}", "response_mode": "blocking"}
        if conversation_id:
            body["conversation_id"] = conversation_id
        conversation_id = (await api.post("/api/v1/chat-messages", headers=key, json=body)).json()[
            "conversation_id"
        ]

    detail = (await api.get(f"/api/v1/conversations/{conversation_id}", headers=key)).json()
    renamed = await api.patch(
        f"/api/v1/conversations/{conversation_id}", headers=key, json={"name": "새 이름"}
    )
    page = (
        await api.get(
            "/api/v1/messages",
            headers=key,
            params={"conversation_id": conversation_id, "limit": 4},
        )
    ).json()
    older = (
        await api.get(
            "/api/v1/messages",
            headers=key,
            params={"conversation_id": conversation_id, "limit": 4, "cursor": page["next_cursor"]},
        )
    ).json()

    assert detail["name"] == "질문 0" and detail["last_run"]["status"] == "succeeded"
    assert renamed.status_code == 200 and renamed.json()["name"] == "새 이름"
    assert [item["text"] for item in page["items"]] == ["질문 1", "답 1", "질문 2", "답 2"]
    assert page["has_more"] is True
    assert [item["text"] for item in older["items"]] == ["질문 0", "답 0"]
    assert older["has_more"] is False and older["next_cursor"] is None
    assert page["items"][0]["blocks"][0]["type"] == "text"
    bad_cursor = await api.get(
        "/api/v1/messages",
        headers=key,
        params={"conversation_id": conversation_id, "cursor": "x"},
    )
    assert bad_cursor.status_code == 422

    deleted = await api.delete(f"/api/v1/conversations/{conversation_id}", headers=key)
    assert deleted.status_code == 204
    assert (
        await api.get(f"/api/v1/conversations/{conversation_id}", headers=key)
    ).status_code == 404
    assert not (settings.data_dir / "workspaces" / conversation_id).exists()


async def test_files_upload_attach_and_download(
    logged_in: AsyncClient, api: AsyncClient, script: Any, sse: Events
) -> None:
    agent = await published_agent(logged_in, approval_mode="off")
    key = await api_key(logged_in, agent["id"])
    other = await published_agent(logged_in, "Other")
    other_key = await api_key(logged_in, other["id"])
    uploaded = await api.post(
        "/api/v1/files/upload",
        headers=key,
        files={"file": ("입력.csv", b"a,b\n1,2\n", "text/csv")},
        data={"user": "u-1"},
    )
    assert uploaded.status_code == 201, uploaded.text
    file = uploaded.json()
    assert (file["filename"], file["size"], file["mime"]) == ("입력.csv", 8, "text/csv")
    # 파일만 올린 대화는 메시지가 생기기 전까지 목록에 나오지 않는다.
    assert (await api.get("/api/v1/conversations", headers=key)).json()["items"] == []
    seen: list[ChatRequest] = []

    def read_attachment(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return [
            *fake_tool_call("write_file", {"path": "outputs/sum.txt", "content": "3"}),
            fake_end("tool_use"),
        ]

    script.add(read_attachment, fake_text("합은 3"))

    response = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={"query": "첨부를 더해", "user": "u-1", "files": [file["file_id"]]},
    )

    events = sse(response.text)
    assert response.headers["x-session-id"] == file["conversation_id"]
    assert "입력.csv" in user_text(seen[0])
    changed = next(event for event in events if event["type"] == "file.changed")["data"]
    download = await api.get(
        f"/api/v1/files/{changed['session_file_id']}/download",
        headers=key,
        params={"user": "u-1"},
    )
    assert download.status_code == 200 and download.content == b"3"
    assert download.headers["content-disposition"].startswith("attachment;")
    foreign = await api.get(
        f"/api/v1/files/{changed['session_file_id']}/download", headers=other_key
    )
    assert foreign.status_code == 404
    reused = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={"query": "다시", "user": "u-1", "files": [file["file_id"]]},
    )
    assert reused.status_code == 422 and reused.json()["error"]["code"] == "file_not_found"


async def test_keys_only_reach_their_agent_and_scopes(
    logged_in: AsyncClient, api: AsyncClient, script: Any
) -> None:
    first = await published_agent(logged_in, "First", approval_mode="off")
    second = await published_agent(logged_in, "Second")
    unpublished = await published_agent(logged_in, "Draft only", publish=False)
    first_key = await api_key(logged_in, first["id"])
    second_key = await api_key(logged_in, second["id"])
    tasks_only = await api_key(logged_in, first["id"], scopes=["tasks"])
    draft_key = await api_key(logged_in, unpublished["id"])
    script.add(fake_text("첫 에이전트"))
    made = await api.post(
        "/api/v1/chat-messages",
        headers=first_key,
        json={"query": "안녕", "response_mode": "blocking"},
    )
    conversation_id = made.json()["conversation_id"]
    run_id = made.json()["run_id"]

    foreign_conversation = await api.get(
        f"/api/v1/conversations/{conversation_id}", headers=second_key
    )
    foreign_run = await api.get(f"/api/v1/runs/{run_id}", headers=second_key)
    foreign_messages = await api.get(
        "/api/v1/messages", headers=second_key, params={"conversation_id": conversation_id}
    )
    no_chat = await api.post("/api/v1/chat-messages", headers=tasks_only, json={"query": "x"})
    not_published = await api.post("/api/v1/chat-messages", headers=draft_key, json={"query": "x"})

    assert foreign_conversation.status_code == 404
    assert foreign_run.status_code == 404
    assert foreign_messages.status_code == 404
    assert (no_chat.status_code, no_chat.json()["error"]["code"]) == (403, "scope_not_allowed")
    assert not_published.status_code == 409
    assert not_published.json()["error"]["code"] == "agent_not_published"
    # 콘솔 미리보기 세션은 서비스 API에서 보이지 않는다.
    preview = await logged_in.post(f"/api/console/agents/{first['id']}/preview/sessions", json={})
    hidden = await api.get(f"/api/v1/conversations/{preview.json()['id']}", headers=first_key)
    assert hidden.status_code == 404


async def test_rate_limit_blocks_the_61st_request_per_key(
    logged_in: AsyncClient, api: AsyncClient
) -> None:
    agent = await published_agent(logged_in)
    key = await api_key(logged_in, agent["id"])
    other = await api_key(logged_in, agent["id"])

    statuses = [
        (await api.get("/api/v1/info", headers=key)).status_code
        for _ in range(API_REQUESTS_PER_MINUTE)
    ]
    blocked = await api.get("/api/v1/info", headers=key)
    other_key = await api.get("/api/v1/info", headers=other)

    assert statuses == [200] * API_REQUESTS_PER_MINUTE
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(blocked.headers["retry-after"]) <= 60
    assert other_key.status_code == 200
