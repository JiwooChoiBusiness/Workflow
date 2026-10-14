"""웹앱의 워크플로우 API(2단계 설계 6.2, 7.4, 10장 M16).

발행되어 webapp_enabled인 워크플로우만 /api/web/flows로 열리고, 웹앱 런은 발행 버전으로
trigger=webapp이다. 미리보기 런과 섞이지 않고, 런 조회·이벤트·중지·응답·파일 내려받기는 웹앱 런에만
닿는다.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.db.models import WfFile
from app.db.models.enums import WfFileKind
from app.db.repositories.workflows import WfFileRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.storage.base import wf_file_key

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
SHELL = [*fake_tool_call("shell", {"command": "echo web"}), fake_end("tool_use")]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


def node(node_id: str, node_type: str, config: dict[str, Any], x: float = 0) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "title": node_id,
        "position": {"x": x, "y": 0},
        "config": config,
    }


def edges(*pairs: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs, start=1):
        source, target = pair.split(">")
        result.append({"id": f"e{index}", "source": source, "target": target})
    return result


def greeting(prefix: str) -> dict[str, Any]:
    return {
        "nodes": [
            node(
                "start",
                "start",
                {
                    "fields": [
                        {"name": "name", "type": "string", "label": "이름", "required": True},
                        {"name": "doc", "type": "file", "label": "첨부"},
                    ]
                },
            ),
            node(
                "template_1",
                "template",
                {
                    "template": prefix + ", {{ name }}님",
                    "vars": [{"name": "name", "var": "start.name"}],
                },
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
        "edges": edges("start>template_1", "template_1>end_1"),
    }


def agent_flow(agent_id: str) -> dict[str, Any]:
    return {
        "nodes": [
            node("start", "start", {"fields": []}),
            node(
                "agent_1",
                "agent",
                {"agent_id": agent_id, "instruction": "웹앱에서 확인한다"},
                300,
            ),
            node("end_1", "end", {"outputs": [{"name": "answer", "var": "agent_1.text"}]}, 600),
        ],
        "edges": edges("start>agent_1", "agent_1>end_1"),
    }


async def save_draft(
    api: AsyncClient, workflow_id: str, graph: dict[str, Any], **settings: Any
) -> None:
    current = (await api.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    body = {
        "graph": graph,
        "env_vars": [],
        "conversation_vars": [],
        "settings": {**current["settings"], **settings},
        "updated_at": current["updated_at"],
    }
    response = await api.put(f"/api/console/workflows/{workflow_id}/draft", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["issues"] == [], response.json()["issues"]


async def published_flow(
    api: AsyncClient,
    graph: dict[str, Any],
    *,
    name: str = "인사 웹앱",
    enable: bool = True,
    **settings: Any,
) -> dict[str, Any]:
    created = await api.post("/api/console/workflows", json={"name": name})
    assert created.status_code == 201, created.text
    workflow = created.json()
    await save_draft(api, workflow["id"], graph, **settings)
    published = await api.post(f"/api/console/workflows/{workflow['id']}/publish", json={})
    assert published.status_code == 201, published.text
    if enable:
        patched = await api.patch(
            f"/api/console/workflows/{workflow['id']}", json={"webapp_enabled": True}
        )
        assert patched.status_code == 200, patched.text
    return workflow


async def published_agent(api: AsyncClient) -> str:
    agent = (await api.post("/api/console/agents", json={"name": "웹 확인 담당"})).json()
    current = (await api.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {key: current[key] for key in AGENT_DRAFT_FIELDS}
    body.update({"env_vars": [], "approval_mode": "all_shell"})
    saved = await api.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    assert saved.status_code == 200, saved.text
    published = await api.post(f"/api/console/agents/{agent['id']}/publish", json={})
    assert published.status_code == 201, published.text
    return agent["id"]


def types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events if event["type"] != "ping"]


async def test_only_published_and_enabled_workflows_open_as_webapps(
    logged_in: AsyncClient, make_client: Callable[[], AsyncClient]
) -> None:
    created = (await logged_in.post("/api/console/workflows", json={"name": "숨김"})).json()
    slug = created["slug"]
    not_published = await logged_in.get(f"/api/web/flows/{slug}")
    await save_draft(logged_in, created["id"], greeting("안녕"))
    await logged_in.post(f"/api/console/workflows/{created['id']}/publish", json={})
    not_enabled = await logged_in.get(f"/api/web/flows/{slug}")
    refused_run = await logged_in.post(f"/api/web/flows/{slug}/runs", json={"inputs": {}})
    await logged_in.patch(f"/api/console/workflows/{created['id']}", json={"webapp_enabled": True})

    opened = await logged_in.get(f"/api/web/flows/{slug}")

    for response in (not_published, not_enabled, refused_run):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "app_not_found"
    assert opened.status_code == 200, opened.text
    info = opened.json()
    assert (info["workflow_id"], info["slug"], info["kind"]) == (created["id"], slug, "workflow")
    assert info["version_number"] == 1
    assert [(field["name"], field["type"], field["required"]) for field in info["fields"]] == [
        ("name", "string", True),
        ("doc", "file", False),
    ]
    assert info["webapp"]["suggested_questions"] == []
    assert (await logged_in.get("/api/web/flows/no-such-flow")).status_code == 404
    # 웹앱 경로도 콘솔과 같은 로그인이 있어야 한다.
    assert (await make_client().get(f"/api/web/flows/{slug}")).status_code == 401


async def test_webapp_run_uses_the_published_version_and_lists_only_webapp_runs(
    logged_in: AsyncClient, sse: Events
) -> None:
    workflow = await published_flow(logged_in, greeting("발행본"))
    await save_draft(logged_in, workflow["id"], greeting("초안"))
    preview = await logged_in.post(
        f"/api/console/workflows/{workflow['id']}/draft/runs", json={"inputs": {"name": "미리"}}
    )
    preview_id = preview.headers["x-run-id"]

    response = await logged_in.post(
        f"/api/web/flows/{workflow['slug']}/runs", json={"inputs": {"name": "지우"}}
    )

    assert response.status_code == 200, response.text
    run_id = response.headers["x-run-id"]
    events = sse(response.text)
    assert types(events)[0] == "workflow.queued"
    assert events[-1]["type"] == "workflow.completed"
    assert events[-1]["data"]["outputs"] == {"greeting": "발행본, 지우님", "doc": None}
    detail = (await logged_in.get(f"/api/web/wf-runs/{run_id}")).json()
    assert detail["trigger"] == "webapp" and detail["status"] == "succeeded"
    assert [item["node_id"] for item in detail["node_runs"]] == ["start", "template_1", "end_1"]
    versions = (await logged_in.get(f"/api/console/workflows/{workflow['id']}/versions")).json()
    assert detail["wf_version_id"] == versions["items"][0]["id"]

    blocking = await logged_in.post(
        f"/api/web/flows/{workflow['slug']}/runs",
        json={"inputs": {"name": "민수"}, "response_mode": "blocking"},
    )
    assert blocking.status_code == 200, blocking.text
    result = blocking.json()
    assert result["status"] == "succeeded"
    assert result["outputs"] == {"greeting": "발행본, 민수님", "doc": None}
    assert set(result) == {"run_id", "status", "outputs", "usage", "error", "pending_interaction"}

    listed = (await logged_in.get(f"/api/web/flows/{workflow['slug']}/runs")).json()
    assert [item["id"] for item in listed["items"]] == [result["run_id"], run_id]
    assert listed["next_offset"] is None
    paged = (
        await logged_in.get(f"/api/web/flows/{workflow['slug']}/runs", params={"limit": 1})
    ).json()
    assert [item["id"] for item in paged["items"]] == [result["run_id"]]
    assert paged["next_offset"] == 1
    # 미리보기 런은 웹앱 경로로 보이지 않는다.
    assert (await logged_in.get(f"/api/web/wf-runs/{preview_id}")).status_code == 404
    assert (await logged_in.get(f"/api/web/wf-runs/{preview_id}/events")).status_code == 404
    stop_preview = await logged_in.post(f"/api/web/wf-runs/{preview_id}/stop", headers=FETCH)
    assert stop_preview.status_code == 404
    # 콘솔 런 목록에서는 trigger로 웹앱 런을 고른다.
    console = (
        await logged_in.get(
            f"/api/console/workflows/{workflow['id']}/runs", params={"trigger": "webapp"}
        )
    ).json()
    assert {item["id"] for item in console["items"]} == {run_id, result["run_id"]}

    replay = sse((await logged_in.get(f"/api/web/wf-runs/{run_id}/events?after=0")).text)
    assert types(replay) == types(events)

    # 공개를 끄면 이미 만든 웹앱 런에도 닿지 않는다.
    await logged_in.patch(
        f"/api/console/workflows/{workflow['id']}", json={"webapp_enabled": False}
    )
    assert (await logged_in.get(f"/api/web/wf-runs/{run_id}")).status_code == 404
    assert (await logged_in.get(f"/api/web/flows/{workflow['slug']}/runs")).status_code == 404


async def test_webapp_inputs_are_checked_and_chatflows_are_not_run_here(
    logged_in: AsyncClient,
) -> None:
    workflow = await published_flow(logged_in, greeting("안녕"))
    missing = await logged_in.post(f"/api/web/flows/{workflow['slug']}/runs", json={"inputs": {}})
    unknown_file = await logged_in.post(
        f"/api/web/flows/{workflow['slug']}/runs",
        json={"inputs": {"name": "a", "doc": str(uuid.uuid4())}},
    )
    chat = (
        await logged_in.post("/api/console/workflows", json={"name": "대화", "kind": "chatflow"})
    ).json()
    published = await logged_in.post(f"/api/console/workflows/{chat['id']}/publish", json={})
    assert published.status_code == 201, published.text
    await logged_in.patch(f"/api/console/workflows/{chat['id']}", json={"webapp_enabled": True})
    chat_info = await logged_in.get(f"/api/web/flows/{chat['slug']}")
    chat_run = await logged_in.post(f"/api/web/flows/{chat['slug']}/runs", json={"inputs": {}})

    assert missing.status_code == 422 and missing.json()["error"]["code"] == "invalid_inputs"
    assert "name" in missing.json()["error"]["details"]
    assert unknown_file.status_code == 422
    assert unknown_file.json()["error"]["code"] == "file_not_found"
    assert chat_info.status_code == 200 and chat_info.json()["kind"] == "chatflow"
    assert chat_run.status_code == 409 and chat_run.json()["error"]["code"] == "wrong_kind"


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def test_uploaded_and_produced_files_download_only_through_their_run(
    app: FastAPI, logged_in: AsyncClient, sse: Events
) -> None:
    workflow = await published_flow(logged_in, greeting("파일"))
    other = await published_flow(logged_in, greeting("다른"), name="다른 웹앱")
    upload = await logged_in.post(
        f"/api/web/flows/{workflow['slug']}/files",
        files={"file": ("메모.txt", "첨부 내용".encode(), "text/plain")},
        headers=FETCH,
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["id"]
    assert upload.json()["kind"] == "upload" and upload.json()["workflow_id"] == workflow["id"]
    # 다른 워크플로우에 올린 파일은 입력으로 쓸 수 없다.
    foreign = await logged_in.post(
        f"/api/web/flows/{other['slug']}/runs",
        json={"inputs": {"name": "a", "doc": file_id}, "response_mode": "blocking"},
    )
    assert foreign.status_code == 422 and foreign.json()["error"]["code"] == "file_not_found"

    response = await logged_in.post(
        f"/api/web/flows/{workflow['slug']}/runs",
        json={"inputs": {"name": "지우", "doc": file_id}},
    )
    run_id = response.headers["x-run-id"]
    outputs = sse(response.text)[-1]["data"]["outputs"]
    assert outputs["doc"]["file_id"] == file_id and outputs["doc"]["filename"] == "메모.txt"

    download = await logged_in.get(f"/api/web/wf-runs/{run_id}/files/{file_id}/download")
    assert download.status_code == 200, download.text
    assert download.content.decode("utf-8") == "첨부 내용"
    assert "attachment" in download.headers["content-disposition"]

    # 노드가 이 런에서 만든 파일(http_request 응답 등)은 출력에 없어도 내려받는다.
    state = app.state
    async with state.db.read() as session:
        workspace = await WorkspaceRepository(session).get_default()
    assert workspace is not None
    output_id = new_id()
    key = wf_file_key(workspace.id, output_id)
    stored = await state.storage.write_stream(key, chunks(b"node output"), max_bytes=1024)
    async with state.db.write() as session:
        WfFileRepository(session, workspace.id).add(
            WfFile(
                id=output_id,
                kind=WfFileKind.OUTPUT,
                workflow_id=uuid.UUID(workflow["id"]),
                wf_run_id=uuid.UUID(run_id),
                filename="response.bin",
                mime="application/octet-stream",
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                storage_key=key,
                created_at=utcnow(),
            )
        )
    produced = await logged_in.get(f"/api/web/wf-runs/{run_id}/files/{output_id}/download")
    assert produced.status_code == 200 and produced.content == b"node output"

    second = await logged_in.post(
        f"/api/web/flows/{workflow['slug']}/runs",
        json={"inputs": {"name": "민수"}, "response_mode": "blocking"},
    )
    second_id = second.json()["run_id"]
    # 이 런의 입력·출력에 없고 이 런이 만들지도 않은 파일은 내려받지 못한다.
    for path in (
        f"/api/web/wf-runs/{second_id}/files/{file_id}/download",
        f"/api/web/wf-runs/{second_id}/files/{output_id}/download",
        f"/api/web/wf-runs/{run_id}/files/{uuid.uuid4()}/download",
    ):
        assert (await logged_in.get(path)).status_code == 404, path


async def test_agent_node_approval_and_stop_go_through_the_webapp(
    logged_in: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await published_agent(logged_in)
    workflow = await published_flow(logged_in, agent_flow(agent_id), name="승인 웹앱")
    script.add(SHELL, fake_text("거절되어 셸 없이 끝냈다"), SHELL)

    first = await logged_in.post(f"/api/web/flows/{workflow['slug']}/runs", json={"inputs": {}})
    run_id = first.headers["x-run-id"]
    events = sse(first.text)
    assert types(events)[-2:] == ["workflow.waiting_input", "workflow.completed"]
    assert events[-1]["data"]["status"] == "waiting_input"
    waiting = (await logged_in.get(f"/api/web/wf-runs/{run_id}")).json()
    assert waiting["pending_interaction"]["interaction"]["kind"] == "approval"

    rejected = await logged_in.post(
        f"/api/web/wf-runs/{run_id}/input",
        json={"decision": "reject", "note": "셸은 쓰지 않는다", "response_mode": "blocking"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "succeeded"
    assert rejected.json()["outputs"] == {"answer": "거절되어 셸 없이 끝냈다"}

    second = await logged_in.post(
        f"/api/web/flows/{workflow['slug']}/runs",
        json={"inputs": {}, "response_mode": "blocking"},
    )
    assert second.json()["status"] == "waiting_input"
    second_id = second.json()["run_id"]
    stopped = await logged_in.post(f"/api/web/wf-runs/{second_id}/stop", headers=FETCH)
    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
    tail = sse((await logged_in.get(f"/api/web/wf-runs/{second_id}/events?after=0")).text)
    assert tail[-1]["type"] == "workflow.completed"
    assert tail[-1]["data"]["status"] == "cancelled"
    again = await logged_in.post(f"/api/web/wf-runs/{second_id}/input", json={"answer": "늦음"})
    assert again.status_code == 409
