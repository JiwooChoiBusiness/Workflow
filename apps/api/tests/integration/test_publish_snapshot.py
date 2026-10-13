"""발행 스냅샷(설계 3.12, 수용 조건 A7).

미리보기 작업공간을 zip으로 떠서 발행 버전에 붙이고, 새 웹앱 세션의 첫 런이 그 zip에서 시작하는지
본다. 모델은 가짜 어댑터의 각본을 따르고 도구는 실제 로컬 백엔드에서 돈다.
"""

import asyncio
import sys
import time
import uuid
import zipfile
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.db.engine import Database
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.sandbox.base import WorkspaceHandle
from app.sandbox.provisioning import WorkspaceProvisioner

WINDOWS = sys.platform == "win32"
FETCH = {"X-Requested-With": "fetch"}
LIST_FILES = "Get-ChildItem -Recurse -Name" if WINDOWS else "find . -type f"
SLEEP = "Start-Sleep 30" if WINDOWS else "sleep 30"
Events = Callable[[str], list[dict[str, Any]]]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


def types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


async def workspace_id(client: AsyncClient) -> str:
    return (await client.get("/api/console/auth/me")).json()["workspace"]["id"]


async def write_in_preview(client: AsyncClient, script: Any, session_id: str, path: str) -> None:
    script.add(
        [
            *fake_tool_call("write_file", {"path": path, "content": "발행 전 메모"}),
            fake_end("tool_use"),
        ],
        fake_text("메모를 남겼다"),
    )
    response = await client.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "메모를 남겨"}
    )
    assert response.status_code == 200, response.text


async def publish(client: AsyncClient, agent_id: str) -> httpx.Response:
    return await client.post(f"/api/console/agents/{agent_id}/publish", json={})


async def test_publish_snapshots_the_preview_and_a_new_webapp_session_starts_from_it(
    logged_in: AsyncClient,
    make_session: Any,
    script: Any,
    sse: Events,
    settings: Settings,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id, preview_id = await make_session(logged_in, approval_mode="off")
    await write_in_preview(logged_in, script, preview_id, "data/notes.txt")
    preview = settings.data_dir / "workspaces" / preview_id
    # install_packages가 남기는 설치 이력과 패키지 폴더를 흉내 낸다. 패키지는 재설치로 되살린다.
    (preview / ".platform" / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")
    (preview / ".packages" / "requests").mkdir(parents=True)
    (preview / ".packages" / "requests" / "__init__.py").write_text("x = 1\n", encoding="utf-8")

    published = await logged_in.post(
        f"/api/console/agents/{agent_id}/publish", json={"change_note": "메모 포함"}
    )

    assert published.status_code == 201, published.text
    body = published.json()
    snapshot = body["snapshot"]
    assert (snapshot["inherited"], snapshot["session_id"]) == (False, preview_id)
    assert body["version"]["snapshot_id"] == snapshot["id"]
    ws = await workspace_id(logged_in)
    archive = settings.data_dir / "storage" / "snapshots" / ws / f"{snapshot['id']}.zip"
    with zipfile.ZipFile(archive) as opened:
        names = set(opened.namelist())
    assert {"data/notes.txt", ".platform/requirements.txt"} <= names
    excluded = (".packages/", ".run/", ".tmp/", "uploads/", "agent-files/", "skills/")
    assert not any(name.startswith(excluded) for name in names)
    assert snapshot["file_count"] == len(names)
    assert snapshot["size_bytes"] == archive.stat().st_size

    reinstalled: list[list[str]] = []

    async def fake_reinstall(
        self: WorkspaceProvisioner, handle: WorkspaceHandle, requirements: list[str]
    ) -> tuple[bool, str]:
        # 실제 pip은 네트워크가 필요하다. 플랫폼이 설치 이력을 넘기는지만 본다.
        reinstalled.append(list(requirements))
        return True, ""

    monkeypatch.setattr(WorkspaceProvisioner, "reinstall_packages", fake_reinstall)
    patched = await logged_in.patch(
        f"/api/console/agents/{agent_id}", json={"webapp_enabled": True}
    )
    assert patched.status_code == 200, patched.text
    slug = body["agent"]["slug"]
    session = (await logged_in.post(f"/api/web/apps/{slug}/sessions", json={})).json()
    script.add(
        [
            *fake_tool_call("shell", {"command": LIST_FILES}, call_id="call_ls"),
            fake_end("tool_use"),
        ],
        fake_text("메모가 보인다"),
    )

    response = await logged_in.post(
        f"/api/web/sessions/{session['id']}/messages", json={"text": "작업공간 파일을 보여 줘"}
    )

    events = sse(response.text)
    order = types(events)
    assert order.index("workspace.restoring") < order.index("workspace.restored")
    assert order.index("workspace.restored") < order.index("step.started")
    restored = next(event for event in events if event["type"] == "workspace.restored")["data"]
    assert restored == {"snapshot_id": snapshot["id"], "packages": 1}
    assert reinstalled == [["requests==2.32.3"]]
    listing = next(event for event in events if event["type"] == "tool_call.completed")["data"]
    assert listing["status"] == "ok"
    assert "notes.txt" in listing["output_excerpt"]
    assert events[-1]["data"]["status"] == "succeeded"
    folder = settings.data_dir / "workspaces" / session["id"]
    assert (folder / "data" / "notes.txt").read_text(encoding="utf-8") == "발행 전 메모"
    assert not (folder / ".packages" / "requests").exists()
    async with db.read() as read:
        instance = await WorkspaceInstanceRepository(read, uuid.UUID(ws)).get_for_session(
            uuid.UUID(session["id"])
        )
    assert instance is not None and str(instance.restored_from_snapshot_id) == snapshot["id"]

    # 같은 세션의 다음 런은 작업공간을 그대로 쓴다(다시 풀지 않는다).
    script.add(fake_text("그대로다"))
    again = sse(
        (
            await logged_in.post(
                f"/api/web/sessions/{session['id']}/messages", json={"text": "다시"}
            )
        ).text
    )
    assert "workspace.restoring" not in types(again)
    assert reinstalled == [["requests==2.32.3"]]


async def test_publish_without_a_preview_workspace_inherits_the_current_snapshot(
    logged_in: AsyncClient, make_session: Any, script: Any
) -> None:
    agent_id, first_preview = await make_session(logged_in, approval_mode="off")
    await write_in_preview(logged_in, script, first_preview, "a.txt")
    v1 = (await publish(logged_in, agent_id)).json()
    deleted = await logged_in.delete(f"/api/console/sessions/{first_preview}", headers=FETCH)
    assert deleted.status_code == 204

    v2 = await publish(logged_in, agent_id)

    assert v2.status_code == 201, v2.text
    assert v2.json()["snapshot"]["id"] == v1["snapshot"]["id"]
    assert v2.json()["snapshot"]["inherited"] is True
    assert v2.json()["version"]["snapshot_id"] == v1["snapshot"]["id"]

    # 새 미리보기 작업공간으로 v3를 뜬 뒤 v1로 롤백하면, 그 다음 발행은 v1의 스냅샷을 잇는다.
    response = await logged_in.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    second_preview = response.json()["id"]
    await write_in_preview(logged_in, script, second_preview, "b.txt")
    v3 = (await publish(logged_in, agent_id)).json()
    assert v3["snapshot"]["inherited"] is False
    assert v3["snapshot"]["id"] != v1["snapshot"]["id"]
    await logged_in.delete(f"/api/console/sessions/{second_preview}", headers=FETCH)
    rollback = await logged_in.post(
        f"/api/console/agents/{agent_id}/versions/{v1['version']['id']}/restore", json={}
    )
    assert rollback.status_code == 200, rollback.text

    v4 = (await publish(logged_in, agent_id)).json()

    assert v4["snapshot"]["id"] == v1["snapshot"]["id"]


async def test_agent_without_any_workspace_publishes_without_a_snapshot(
    logged_in: AsyncClient,
) -> None:
    agent = (await logged_in.post("/api/console/agents", json={"name": "빈 에이전트"})).json()

    response = await publish(logged_in, agent["id"])

    assert response.status_code == 201, response.text
    assert response.json()["snapshot"] is None
    assert response.json()["version"]["snapshot_id"] is None


async def test_snapshot_over_the_limit_is_refused_and_leaves_nothing(
    logged_in: AsyncClient,
    make_session: Any,
    script: Any,
    app: FastAPI,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id, preview_id = await make_session(logged_in, approval_mode="off")
    await write_in_preview(logged_in, script, preview_id, "big.txt")
    monkeypatch.setattr(app.state.sandbox, "_snapshot_max_bytes", 4)

    response = await publish(logged_in, agent_id)

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "snapshot_too_large"
    assert "큰 폴더" in error["message"]
    versions = (await logged_in.get(f"/api/console/agents/{agent_id}/versions")).json()
    assert versions["items"] == []
    leftovers = list((settings.data_dir / "storage").rglob("*.zip*"))
    assert leftovers == []


async def test_publish_rechecks_the_draft(
    logged_in: AsyncClient, skill_zip: Callable[..., bytes]
) -> None:
    agent = (
        await logged_in.post("/api/console/agents", json={"name": "스킬 쓰는 에이전트"})
    ).json()
    upload = await logged_in.post(
        "/api/console/skills",
        files={"file": ("skill.zip", skill_zip(), "application/zip")},
        headers=FETCH,
    )
    assert upload.status_code == 201, upload.text
    skill = upload.json()
    draft = (await logged_in.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {
        key: draft[key]
        for key in (
            "instructions",
            "model",
            "tools",
            "limits",
            "sandbox",
            "approval_mode",
            "file_ids",
            "updated_at",
        )
    }
    body["skill_version_ids"] = [skill["current_version_id"]]
    saved = await logged_in.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    assert saved.status_code == 200, saved.text
    removed = await logged_in.delete(f"/api/console/skills/{skill['id']}", headers=FETCH)
    assert removed.status_code == 204

    response = await publish(logged_in, agent["id"])

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "draft_invalid"
    assert error["details"][0]["loc"] == ["draft", "skill_version_ids", 0]
    assert error["details"][0]["type"] == "skill_deleted"
    versions = (await logged_in.get(f"/api/console/agents/{agent['id']}/versions")).json()
    assert versions["items"] == []


async def test_publish_waits_for_a_running_preview_run(
    logged_in: AsyncClient, make_session: Any, script: Any
) -> None:
    agent_id, preview_id = await make_session(logged_in, approval_mode="off")
    script.add(
        [
            *fake_tool_call("shell", {"command": SLEEP, "timeout_sec": 60}, call_id="call_sleep"),
            fake_end("tool_use"),
        ]
    )
    stream = asyncio.create_task(
        logged_in.post(f"/api/console/sessions/{preview_id}/messages", json={"text": "기다려"})
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = (await logged_in.get(f"/api/console/sessions/{preview_id}/workspace")).json()
        if status.get("running"):
            break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError("도구 프로세스가 뜨지 않았다")

    response = await publish(logged_in, agent_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "run_in_progress"
    run_id = (await logged_in.get(f"/api/console/sessions/{preview_id}")).json()["active_run"]["id"]
    await logged_in.post(f"/api/console/runs/{run_id}/stop", json={})
    await asyncio.wait_for(stream, 15)
