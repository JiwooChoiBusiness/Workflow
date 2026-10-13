"""세션·메시지·세션 파일·작업공간·런 조회 API(설계 6.2)."""

import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.db.engine import Database
from app.db.repositories.workspace_instances import WorkspaceInstanceRepository
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ImageBlock, ModelEvent, TextBlock
from app.runner.supervisor import Supervisor

Events = Callable[[str], list[dict[str, Any]]]
FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


def tiny_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


async def send(client: AsyncClient, session_id: str, text: str, **body: Any) -> Any:
    response = await client.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": text, **body}
    )
    assert response.status_code == 200, response.text
    return response


async def test_session_api_requires_login_and_json(
    client: AsyncClient, logged_in: AsyncClient, make_session: Any
) -> None:
    _, session_id = await make_session(logged_in)

    raw = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages",
        content=b'{"text": "x"}',
        headers={"Content-Type": "text/plain"},
    )
    assert raw.status_code == 415
    await logged_in.post("/api/console/auth/logout", json={})
    assert (await logged_in.get("/api/console/sessions")).status_code == 401
    assert (await logged_in.get(f"/api/console/sessions/{session_id}")).status_code == 401


async def test_preview_session_list_detail_and_patch(
    logged_in: AsyncClient, make_session: Any
) -> None:
    agent_id, session_id = await make_session(logged_in)
    other_agent, _ = await make_session(logged_in)
    missing = await logged_in.post(
        "/api/console/agents/00000000-0000-0000-0000-000000000000/preview/sessions", json={}
    )
    assert missing.status_code == 404

    listed = (await logged_in.get("/api/console/sessions", params={"agent_id": agent_id})).json()
    assert [item["id"] for item in listed["items"]] == [session_id]
    assert listed["items"][0]["source"] == "preview"
    previews = (await logged_in.get("/api/console/sessions", params={"source": "preview"})).json()
    assert len(previews["items"]) == 2
    assert other_agent != agent_id

    detail = (await logged_in.get(f"/api/console/sessions/{session_id}")).json()
    assert detail["active_run"] is None and detail["workspace"] is None
    assert detail["approval_override"] is None

    patched = await logged_in.patch(
        f"/api/console/sessions/{session_id}",
        json={"title": "이름 붙인 세션", "approval_override": "off"},
    )
    assert patched.status_code == 200
    assert (patched.json()["title"], patched.json()["approval_override"]) == (
        "이름 붙인 세션",
        "off",
    )
    cleared = await logged_in.patch(
        f"/api/console/sessions/{session_id}", json={"approval_override": None}
    )
    assert cleared.json()["approval_override"] is None
    assert cleared.json()["title"] == "이름 붙인 세션"


async def test_session_auto_approve_skips_approval(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in)
    await logged_in.patch(f"/api/console/sessions/{session_id}", json={"approval_override": "off"})
    script.add(
        [*fake_tool_call("shell", {"command": "echo auto"}), fake_end("tool_use")],
        fake_text("승인 없이 실행했다"),
    )

    events = sse((await send(logged_in, session_id, "실행")).text)

    assert "run.waiting_input" not in [event["type"] for event in events]
    started = next(event for event in events if event["type"] == "run.started")
    assert started["data"]["approval_mode"] == "off"
    assert events[-1]["data"]["status"] == "succeeded"


async def test_messages_are_paged_by_seq(
    logged_in: AsyncClient, make_session: Any, script: Any
) -> None:
    _, session_id = await make_session(logged_in)
    for index in range(3):
        script.add(fake_text(f"답 {index}"))
        await send(logged_in, session_id, f"질문 {index}")

    last = (
        await logged_in.get(f"/api/console/sessions/{session_id}/messages", params={"limit": 2})
    ).json()
    assert [item["text"] for item in last["items"]] == ["질문 2", "답 2"]
    assert last["has_more"] is True
    earlier = (
        await logged_in.get(
            f"/api/console/sessions/{session_id}/messages",
            params={"before_seq": last["items"][0]["seq"], "limit": 10},
        )
    ).json()
    assert [item["text"] for item in earlier["items"]] == ["질문 0", "답 0", "질문 1", "답 1"]
    assert earlier["has_more"] is False
    user_blocks = earlier["items"][0]["blocks"]
    assert user_blocks[0] == {"type": "text", "text": "질문 0"}
    assert user_blocks[-1]["kind"] == "context"
    assert user_blocks[-1]["text"].startswith("<context>")


async def test_upload_is_attached_copied_and_described(
    logged_in: AsyncClient, make_session: Any, script: Any, settings: Settings
) -> None:
    _, session_id = await make_session(logged_in)
    seen: list[ChatRequest] = []

    def remember(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return fake_text("첨부를 받았다")

    script.add(remember)
    uploaded = await logged_in.post(
        f"/api/console/sessions/{session_id}/uploads",
        files={"file": ("자료.csv", b"a,b\n1,2\n", "text/csv")},
        headers=FETCH,
    )
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]
    assert uploaded.json()["kind"] == "upload"

    await send(logged_in, session_id, "첨부를 봐 줘", file_ids=[file_id])

    messages = (await logged_in.get(f"/api/console/sessions/{session_id}/messages")).json()
    user = messages["items"][0]
    [ref] = user["files"]
    assert ref["session_file_id"] == file_id
    assert ref["path"] == f"uploads/{user['id']}/자료.csv"
    copied = settings.data_dir / "workspaces" / session_id / "uploads" / user["id"] / "자료.csv"
    assert copied.read_bytes() == b"a,b\n1,2\n"
    context = seen[0].messages[-1].blocks[-1]
    assert isinstance(context, TextBlock) and ref["path"] in context.text
    reused = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages",
        json={"text": "다시", "file_ids": [file_id]},
    )
    assert reused.status_code == 422
    assert reused.json()["error"]["code"] == "file_not_found"


async def test_image_upload_reaches_the_model_as_image(
    logged_in: AsyncClient, make_session: Any, script: Any
) -> None:
    _, session_id = await make_session(logged_in)
    seen: list[ChatRequest] = []

    def remember(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return fake_text("그림을 봤다")

    script.add(remember)
    uploaded = await logged_in.post(
        f"/api/console/sessions/{session_id}/uploads",
        files={"file": ("dot.png", tiny_png(), "image/png")},
        headers=FETCH,
    )

    await send(logged_in, session_id, "이 그림은?", file_ids=[uploaded.json()["id"]])

    images = [block for block in seen[0].messages[-1].blocks if isinstance(block, ImageBlock)]
    assert len(images) == 1
    assert images[0].media_type == "image/png" and images[0].data_b64
    stored = (await logged_in.get(f"/api/console/sessions/{session_id}/messages")).json()
    image_block = stored["items"][0]["blocks"][1]
    assert image_block["type"] == "image" and "data_b64" not in image_block


async def test_session_files_and_safe_download_headers(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(
        [
            *fake_tool_call(
                "write_file", {"path": "outputs/page.html", "content": "<script>x</script>"}
            ),
            fake_end("tool_use"),
        ],
        fake_text("만들었다"),
    )
    events = sse((await send(logged_in, session_id, "html을 만들어")).text)
    run_id = events[0]["run_id"]

    files = (await logged_in.get(f"/api/console/sessions/{session_id}/files")).json()["items"]
    [output] = [item for item in files if item["kind"] == "output"]
    assert (output["path"], output["downloadable"], output["run_id"]) == (
        "outputs/page.html",
        True,
        run_id,
    )
    download = await logged_in.get(
        f"/api/console/sessions/{session_id}/files/{output['id']}/download"
    )
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert 'filename="page.html"' in download.headers["content-disposition"]
    assert download.content == b"<script>x</script>"
    only_uploads = (
        await logged_in.get(f"/api/console/sessions/{session_id}/files", params={"kind": "upload"})
    ).json()["items"]
    assert only_uploads == []


async def test_runs_list_detail_events_and_small_tool_output(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    agent_id, session_id = await make_session(logged_in, approval_mode="off")
    script.add(
        [
            *fake_tool_call("shell", {"command": "echo 작은출력"}, call_id="call_s"),
            fake_end("tool_use"),
        ],
        fake_text("끝"),
    )
    events = sse((await send(logged_in, session_id, "실행")).text)
    run_id = events[0]["run_id"]

    runs = (await logged_in.get("/api/console/runs", params={"session_id": session_id})).json()
    assert [item["id"] for item in runs["items"]] == [run_id]
    by_agent = (await logged_in.get("/api/console/runs", params={"agent_id": agent_id})).json()
    assert [item["id"] for item in by_agent["items"]] == [run_id]
    detail = (await logged_in.get(f"/api/console/runs/{run_id}")).json()
    assert detail["status"] == "succeeded"
    assert detail["model"]["used"] == "fake-model"
    page = (
        await logged_in.get(f"/api/console/runs/{run_id}/events", params={"after": 0, "limit": 3})
    ).json()
    assert [item["seq"] for item in page["items"]] == [1, 2, 3]
    assert page["next_after"] == 3
    output = await logged_in.get(f"/api/console/runs/{run_id}/tool-calls/call_s/output")
    assert output.status_code == 200
    assert "작은출력" in output.text
    missing = await logged_in.get(f"/api/console/runs/{run_id}/tool-calls/nope/output")
    assert missing.status_code == 404


async def test_input_errors(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in)
    script.add([*fake_tool_call("shell", {"command": "echo x"}), fake_end("tool_use")])
    events = sse((await send(logged_in, session_id, "실행")).text)
    run_id = events[0]["run_id"]

    wrong_kind = await logged_in.post(f"/api/console/runs/{run_id}/input", json={"answer": "네"})
    assert wrong_kind.status_code == 422
    assert wrong_kind.json()["error"]["code"] == "decision_required"
    stopped = await logged_in.post(f"/api/console/runs/{run_id}/stop", json={})
    assert stopped.json()["status"] == "cancelled"
    late = await logged_in.post(f"/api/console/runs/{run_id}/input", json={"decision": "approve"})
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "run_not_waiting"
    # 중지한 런 다음에는 새 메시지를 보낼 수 있고, 끝나지 않은 도구 호출에는 결과가 채워져 있다.
    seen: list[ChatRequest] = []

    def remember(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return fake_text("다음")

    script.add(remember)
    await send(logged_in, session_id, "다음 질문")
    assert any("실행되지 않음" in str(message.model_dump()) for message in seen[0].messages)


async def test_workspace_status_clean_reset_and_list(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events, settings: Settings
) -> None:
    _, session_id = await make_session(logged_in)
    none_yet = (await logged_in.get(f"/api/console/sessions/{session_id}/workspace")).json()
    assert none_yet["exists"] is False and none_yet["instance"] is None
    script.add(
        [
            *fake_tool_call("write_file", {"path": "outputs/a.txt", "content": "A" * 100}),
            fake_end("tool_use"),
        ],
        fake_text("썼다"),
    )
    await send(logged_in, session_id, "써")
    folder = settings.data_dir / "workspaces" / session_id

    status = (await logged_in.get(f"/api/console/sessions/{session_id}/workspace")).json()
    assert status["exists"] is True
    assert status["instance"]["status"] == "ready"
    assert Path(status["instance"]["root_path"]) == folder.resolve()
    assert status["disk_bytes"] >= 100
    assert "outputs" in [item["path"] for item in status["largest_dirs"]]
    assert status["running"] == []
    killed = await logged_in.post(f"/api/console/sessions/{session_id}/workspace/kill", json={})
    assert killed.json() == {"killed": []}

    (folder / ".packages" / "pkg").mkdir(parents=True)
    (folder / ".packages" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    cleaned = await logged_in.post(
        f"/api/console/sessions/{session_id}/workspace/clean-packages", json={}
    )
    assert cleaned.status_code == 200
    assert not (folder / ".packages" / "pkg").exists()

    reset = await logged_in.post(f"/api/console/sessions/{session_id}/workspace/reset", json={})
    assert reset.status_code == 200
    assert not (folder / "outputs" / "a.txt").exists()
    assert (folder / ".platform" / "workspace.json").is_file()

    listed = (await logged_in.get("/api/console/workspaces")).json()["items"]
    assert [item["session_id"] for item in listed] == [session_id]
    assert listed[0]["session_source"] == "preview"

    # 초기화한 폴더를 다음 런이 같은 작업공간으로 알아본다(recreated가 아니다).
    script.add(fake_text("그대로"))
    events = sse((await send(logged_in, session_id, "다시")).text)
    assert "workspace.recreated" not in [event["type"] for event in events]


async def test_disk_limit_rejects_new_runs(
    logged_in: AsyncClient, make_session: Any, script: Any, db: Database
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(fake_text("첫 런"))
    await send(logged_in, session_id, "처음")
    detail = (await logged_in.get(f"/api/console/sessions/{session_id}")).json()
    workspace_id = (await logged_in.get("/api/console/auth/me")).json()["workspace"]["id"]
    async with db.write() as session:
        import uuid

        row = await WorkspaceInstanceRepository(session, uuid.UUID(workspace_id)).get_for_session(
            uuid.UUID(session_id)
        )
        assert row is not None and str(row.id) == detail["workspace"]["id"]
        row.disk_bytes = 3 * 1024**3

    refused = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "더"}
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "workspace_disk_limit"


async def test_delete_session_removes_workspace_but_keeps_outputs(
    logged_in: AsyncClient, make_session: Any, script: Any, settings: Settings
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(
        [
            *fake_tool_call("write_file", {"path": "outputs/keep.txt", "content": "K"}),
            fake_end("tool_use"),
        ],
        fake_text("남긴다"),
    )
    upload = await logged_in.post(
        f"/api/console/sessions/{session_id}/uploads",
        files={"file": ("note.txt", b"n", "text/plain")},
        headers=FETCH,
    )
    await send(logged_in, session_id, "써", file_ids=[upload.json()["id"]])
    files = (await logged_in.get(f"/api/console/sessions/{session_id}/files")).json()["items"]
    output = next(item for item in files if item["kind"] == "output")
    folder = settings.data_dir / "workspaces" / session_id
    assert folder.is_dir()

    deleted = await logged_in.delete(f"/api/console/sessions/{session_id}", headers=FETCH)

    assert deleted.status_code == 204
    assert (await logged_in.get(f"/api/console/sessions/{session_id}")).status_code == 404
    assert not folder.exists()
    kept = await logged_in.get(f"/api/console/sessions/{session_id}/files/{output['id']}/download")
    assert kept.status_code == 200 and kept.content == b"K"
    assert not list((settings.data_dir / "storage" / "uploads").rglob("*.*"))
    assert (await logged_in.get("/api/console/workspaces")).json()["items"] == []


async def test_new_runs_are_refused_while_shutting_down(
    logged_in: AsyncClient, make_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, session_id = await make_session(logged_in)
    monkeypatch.setattr(Supervisor, "shutting_down", property(lambda self: True))

    refused = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "늦은 요청"}
    )

    assert refused.status_code == 503
    assert refused.json()["error"]["code"] == "shutting_down"
