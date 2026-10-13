"""웹앱 API(설계 6.3, 수용 조건 A8).

발행되어 webapp_enabled인 에이전트만 /api/web으로 열리고, 웹앱 세션은 발행 버전을 쓰며 초안을 쓰는
미리보기 세션과 섞이지 않는다.
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ModelEvent

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


async def save_draft(client: AsyncClient, agent_id: str, **changes: Any) -> None:
    draft = (await client.get(f"/api/console/agents/{agent_id}/draft")).json()
    body = {key: draft[key] for key in DRAFT_FIELDS}
    body.update(changes)
    response = await client.put(f"/api/console/agents/{agent_id}/draft", json=body)
    assert response.status_code == 200, response.text


async def published_app(
    client: AsyncClient, *, enable: bool = True, **draft: Any
) -> dict[str, Any]:
    agent = (await client.post("/api/console/agents", json={"name": "Web Helper"})).json()
    await save_draft(client, agent["id"], **draft)
    response = await client.post(f"/api/console/agents/{agent['id']}/publish", json={})
    assert response.status_code == 201, response.text
    if enable:
        enabled = await client.patch(
            f"/api/console/agents/{agent['id']}", json={"webapp_enabled": True}
        )
        assert enabled.status_code == 200, enabled.text
    return response.json()


async def new_session(client: AsyncClient, slug: str) -> dict[str, Any]:
    response = await client.post(f"/api/web/apps/{slug}/sessions", json={})
    assert response.status_code == 201, response.text
    return response.json()


async def test_only_published_and_enabled_agents_open_as_webapps(
    logged_in: AsyncClient,
) -> None:
    agent = (await logged_in.post("/api/console/agents", json={"name": "Hidden"})).json()
    slug = agent["slug"]
    not_published = await logged_in.get(f"/api/web/apps/{slug}")
    await logged_in.post(f"/api/console/agents/{agent['id']}/publish", json={})
    not_enabled = await logged_in.get(f"/api/web/apps/{slug}")
    await logged_in.patch(f"/api/console/agents/{agent['id']}", json={"webapp_enabled": True})

    opened = await logged_in.get(f"/api/web/apps/{slug}")

    for response in (not_published, not_enabled):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "app_not_found"
    assert opened.status_code == 200
    info = opened.json()
    assert (info["agent_id"], info["slug"], info["version_number"]) == (agent["id"], slug, 1)
    assert info["approval_mode"] == "all_shell"
    assert (await logged_in.get("/api/web/apps/no-such-app")).status_code == 404
    refused = await logged_in.post(f"/api/web/apps/{agent['slug']}x/sessions", json={})
    assert refused.status_code == 404


async def test_webapp_session_talks_to_the_published_version_not_the_draft(
    logged_in: AsyncClient, script: Any, sse: Events
) -> None:
    published = await published_app(logged_in, instructions="발행본 지시문", approval_mode="off")
    agent_id = published["agent"]["id"]
    await save_draft(logged_in, agent_id, instructions="초안 지시문", approval_mode="off")
    preview = await logged_in.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    seen: list[ChatRequest] = []

    def answer(text: str) -> Callable[[ChatRequest], list[ModelEvent]]:
        def turn(request: ChatRequest) -> list[ModelEvent]:
            seen.append(request)
            return fake_text(text)

        return turn

    session = await new_session(logged_in, published["agent"]["slug"])
    script.add(answer("첫 답"), answer("이어서 답"))

    first = sse(
        (
            await logged_in.post(
                f"/api/web/sessions/{session['id']}/messages", json={"text": "안녕"}
            )
        ).text
    )
    second = sse(
        (
            await logged_in.post(
                f"/api/web/sessions/{session['id']}/messages", json={"text": "이어서"}
            )
        ).text
    )

    assert session["source"] == "webapp"
    assert session["agent_version_id"] == published["version"]["id"]
    assert first[-1]["data"]["status"] == "succeeded"
    assert second[-1]["data"]["status"] == "succeeded"
    assert all("발행본 지시문" in request.system for request in seen)
    assert not any("초안 지시문" in request.system for request in seen)
    # 이어서 대화하면 앞 대화가 히스토리로 간다.
    assert len(seen[1].messages) == 3
    listed = (await logged_in.get(f"/api/web/apps/{published['agent']['slug']}/sessions")).json()
    assert [item["id"] for item in listed["items"]] == [session["id"]]
    assert preview.json()["id"] not in [item["id"] for item in listed["items"]]
    messages = (await logged_in.get(f"/api/web/sessions/{session['id']}/messages")).json()
    assert [item["role"] for item in messages["items"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    detail = (await logged_in.get(f"/api/web/sessions/{session['id']}")).json()
    assert detail["last_run"]["status"] == "succeeded" and detail["active_run"] is None
    # 콘솔 로그에서는 소스로 웹앱 세션을 가린다(A10).
    logs = (await logged_in.get("/api/console/sessions", params={"source": "webapp"})).json()
    assert [item["id"] for item in logs["items"]] == [session["id"]]


async def test_webapp_api_only_reaches_webapp_sessions_of_enabled_apps(
    logged_in: AsyncClient, make_client: Callable[[], AsyncClient], make_session: Any
) -> None:
    published = await published_app(logged_in)
    agent_id = published["agent"]["id"]
    session = await new_session(logged_in, published["agent"]["slug"])
    _, preview_id = await make_session(logged_in)

    preview = await logged_in.get(f"/api/web/sessions/{preview_id}")
    preview_messages = await logged_in.get(f"/api/web/sessions/{preview_id}/messages")
    anonymous = await make_client().get(f"/api/web/sessions/{session['id']}")
    not_json = await logged_in.post(
        f"/api/web/sessions/{session['id']}/messages",
        content=b"text=hi",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    await logged_in.patch(f"/api/console/agents/{agent_id}", json={"webapp_enabled": False})
    disabled = await logged_in.get(f"/api/web/sessions/{session['id']}")

    assert preview.status_code == 404 and preview_messages.status_code == 404
    assert anonymous.status_code == 401
    assert not_json.status_code == 415
    assert disabled.status_code == 404


async def test_webapp_approval_stop_rename_and_delete(
    logged_in: AsyncClient, script: Any, sse: Events, settings: Settings
) -> None:
    published = await published_app(logged_in)  # 승인 모드 all_shell(기본)
    session = await new_session(logged_in, published["agent"]["slug"])
    script.add(
        [*fake_tool_call("shell", {"command": "echo web"}, call_id="call_1"), fake_end("tool_use")],
        fake_text("실행했다"),
        [
            *fake_tool_call("shell", {"command": "echo stop"}, call_id="call_2"),
            fake_end("tool_use"),
        ],
    )
    waiting = sse(
        (
            await logged_in.post(
                f"/api/web/sessions/{session['id']}/messages", json={"text": "실행해"}
            )
        ).text
    )
    assert waiting[-2]["type"] == "run.waiting_input"
    run_id = waiting[-1]["run_id"]

    resumed = await logged_in.post(
        f"/api/web/runs/{run_id}/input", json={"decision": "approve", "response_mode": "streaming"}
    )

    events = sse(resumed.text)
    assert events[0]["type"] == "interaction.resolved"
    assert events[-1]["data"]["status"] == "succeeded"
    tool = next(event for event in events if event["type"] == "tool_call.completed")["data"]
    assert "web" in tool["output_excerpt"]

    second = sse(
        (
            await logged_in.post(f"/api/web/sessions/{session['id']}/messages", json={"text": "또"})
        ).text
    )
    stopped = await logged_in.post(f"/api/web/runs/{second[-1]['run_id']}/stop", json={})
    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"

    renamed = await logged_in.patch(f"/api/web/sessions/{session['id']}", json={"title": "웹 대화"})
    assert renamed.status_code == 200 and renamed.json()["title"] == "웹 대화"
    deleted = await logged_in.delete(f"/api/web/sessions/{session['id']}", headers=FETCH)
    assert deleted.status_code == 204
    assert (await logged_in.get(f"/api/web/sessions/{session['id']}")).status_code == 404
    assert not (settings.data_dir / "workspaces" / session["id"]).exists()


async def test_webapp_upload_is_attached_and_output_downloads(
    logged_in: AsyncClient, script: Any, sse: Events
) -> None:
    published = await published_app(logged_in, approval_mode="off")
    session = await new_session(logged_in, published["agent"]["slug"])
    upload = await logged_in.post(
        f"/api/web/sessions/{session['id']}/uploads",
        files={"file": ("메모.txt", "첨부 내용".encode(), "text/plain")},
        headers=FETCH,
    )
    assert upload.status_code == 201, upload.text
    script.add(
        [
            *fake_tool_call("write_file", {"path": "outputs/result.txt", "content": "결과"}),
            fake_end("tool_use"),
        ],
        fake_text("만들었다"),
    )

    events = sse(
        (
            await logged_in.post(
                f"/api/web/sessions/{session['id']}/messages",
                json={"text": "첨부를 보고 결과를 만들어", "file_ids": [upload.json()["id"]]},
            )
        ).text
    )

    changed = next(event for event in events if event["type"] == "file.changed")["data"]
    download = await logged_in.get(
        f"/api/web/sessions/{session['id']}/files/{changed['session_file_id']}/download"
    )
    assert download.status_code == 200
    assert download.content.decode("utf-8") == "결과"
