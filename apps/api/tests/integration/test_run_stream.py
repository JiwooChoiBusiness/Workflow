"""API → 큐 → Supervisor → 루프 → SSE 끝까지(설계 10장 M6).

모델은 가짜 어댑터의 각본을 따르고 도구는 실제 로컬 백엔드(PowerShell·Job Object)에서 돈다.
"""

import asyncio
import shutil
import sys
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ModelEvent, ToolResultBlock

WINDOWS = sys.platform == "win32"
Events = Callable[[str], list[dict[str, Any]]]


def sh(windows: str, posix: str) -> str:
    return windows if WINDOWS else posix


SLEEP = sh("Start-Sleep 30", "sleep 30")
WRITE_CSV = sh(
    "Write-Output '한글 출력'; 'a,b' | Out-File outputs/r.csv -Encoding utf8",
    "echo '한글 출력'; printf 'a,b\\n' > outputs/r.csv",
)
BIG_OUTPUT = sh(
    "1..4000 | ForEach-Object { 'line ' + $_ + ' ' + ('x' * 80) }",
    "for i in $(seq 1 4000); do printf 'line %s %080d\\n' $i 0; done",
)


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        process = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not process:
            return False
        try:
            return kernel32.WaitForSingleObject(process, 0) == 0x102
        finally:
            kernel32.CloseHandle(process)
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def last_results(request: ChatRequest) -> list[ToolResultBlock]:
    return [block for block in request.messages[-1].blocks if isinstance(block, ToolResultBlock)]


def types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events]


def in_order(events: list[dict[str, Any]], expected: list[str]) -> bool:
    found = types(events)
    positions = [found.index(name) for name in expected]
    return positions == sorted(positions)


async def wait_running(client: AsyncClient, session_id: str, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = (await client.get(f"/api/console/sessions/{session_id}/workspace")).json()
        if status.get("running"):
            return status["running"][0]
        await asyncio.sleep(0.1)
    raise AssertionError("도구 프로세스가 뜨지 않았다")


async def test_text_answer_streams_in_order(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(fake_text("안녕하세요. 무엇을 할까요?"))

    response = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "안녕"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    run_id = response.headers["x-run-id"]
    events = sse(response.text)
    assert events[0]["type"] == "run.queued" and events[0]["seq"] == 1
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["run_id"] == run_id and event["session_id"] == session_id for event in events)
    assert in_order(
        events,
        [
            "run.queued",
            "run.started",
            "step.started",
            "message.delta",
            "usage",
            "message.completed",
        ],
    )
    assert events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["status"] == "succeeded"
    assert events[-1]["data"]["usage_total"]["input_tokens"] > 0
    started = next(event for event in events if event["type"] == "run.started")["data"]
    assert started["approval_mode"] == "all_shell" and started["resumed"] is False

    run = (await logged_in.get(f"/api/console/runs/{run_id}")).json()
    assert (run["status"], run["steps"]) == ("succeeded", 1)
    messages = (await logged_in.get(f"/api/console/sessions/{session_id}/messages")).json()
    assert [item["role"] for item in messages["items"]] == ["user", "assistant"]
    assert messages["items"][1]["text"] == "안녕하세요. 무엇을 할까요?"
    detail = (await logged_in.get(f"/api/console/sessions/{session_id}")).json()
    assert detail["active_run"] is None
    assert detail["last_run"]["id"] == run_id
    assert detail["title"] == "안녕"


async def test_approval_tool_output_files_and_download(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(
        [
            *fake_text("파일을 만들게요."),
            *fake_tool_call("shell", {"command": WRITE_CSV}, call_id="call_csv"),
            fake_end("tool_use"),
        ],
        lambda request: [*fake_text("완료: outputs/r.csv")],
    )

    first = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "CSV를 만들어 줘"}
    )
    run_id = first.headers["x-run-id"]
    events = sse(first.text)

    assert types(events)[-2:] == ["run.waiting_input", "run.completed"]
    assert events[-1]["data"]["status"] == "waiting_input"
    waiting = events[-2]["data"]
    assert (waiting["kind"], waiting["tool_name"], waiting["cwd"]) == ("approval", "shell", ".")
    assert waiting["input"] == {"command": WRITE_CSV}
    detail = (await logged_in.get(f"/api/console/sessions/{session_id}")).json()
    pending = detail["active_run"]["pending_interaction"]
    assert detail["active_run"]["status"] == "waiting_input"
    assert pending["interaction_id"] == waiting["interaction_id"]

    resumed = await logged_in.post(
        f"/api/console/runs/{run_id}/input",
        json={"decision": "approve", "response_mode": "streaming"},
    )

    assert resumed.status_code == 200
    more = sse(resumed.text)
    assert more[0]["type"] == "interaction.resolved"
    assert more[0]["seq"] == events[-1]["seq"] + 1
    assert in_order(
        more,
        [
            "interaction.resolved",
            "run.queued",
            "run.resumed",
            "run.started",
            "tool_call.output_delta",
            "tool_call.completed",
            "step.started",
            "message.delta",
            "usage",
            "file.changed",
            "message.completed",
            "run.completed",
        ],
    )
    tool = next(event for event in more if event["type"] == "tool_call.completed")["data"]
    assert (tool["status"], tool["exit_code"]) == ("ok", 0)
    assert "한글 출력" in tool["output_excerpt"]
    changed = next(event for event in more if event["type"] == "file.changed")["data"]
    assert (changed["path"], changed["downloadable"]) == ("outputs/r.csv", True)
    final = more[-2]["data"]
    assert final["files"][0]["session_file_id"] == changed["session_file_id"]
    assert more[-1]["data"]["status"] == "succeeded"

    download = await logged_in.get(
        f"/api/console/sessions/{session_id}/files/{changed['session_file_id']}/download"
    )
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert "a,b" in download.content.decode("utf-8-sig")

    run = (await logged_in.get(f"/api/console/runs/{run_id}")).json()
    assert (run["status"], run["steps"]) == ("succeeded", 2)
    assert run["pending_interaction"] is None
    messages = (await logged_in.get(f"/api/console/sessions/{session_id}/messages")).json()
    roles = [item["role"] for item in messages["items"]]
    assert roles == ["user", "assistant", "assistant"]
    step = messages["items"][1]
    assert [block["type"] for block in step["blocks"]] == ["text", "tool_use", "tool_result"]
    # 승인 뒤 도구 결과를 붙여 다시 저장한 행도 stop_reason·usage를 잃지 않는다.
    assert step["stop_reason"] == "tool_use"
    assert step["usage"] is not None and step["usage"]["model"] == "fake-model"
    assert messages["items"][2]["files"][0]["path"] == "outputs/r.csv"


async def test_files_from_the_previous_run_are_not_reported_again(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    # 앞 런이 만든 파일을 건드리지 않은 다음 런은 그 파일을 다시 알리지 않는다(3.13).
    _, session_id = await make_session(logged_in, approval_mode="off")

    def write(content: str) -> list[ModelEvent]:
        return [
            *fake_tool_call("write_file", {"path": "outputs/result.csv", "content": content}),
            fake_end("tool_use"),
        ]

    async def send(text: str) -> list[dict[str, Any]]:
        response = await logged_in.post(
            f"/api/console/sessions/{session_id}/messages", json={"text": text}
        )
        return sse(response.text)

    def changed(events: list[dict[str, Any]]) -> list[str]:
        return [event["data"]["path"] for event in events if event["type"] == "file.changed"]

    script.add(write("a,b\n"), fake_text("만들었다"))
    first = await send("CSV를 만들어")
    script.add(fake_text("그대로 둔다"))
    second = await send("아무것도 바꾸지 마")
    script.add(write("a,b,c\n"), fake_text("고쳤다"))
    third = await send("CSV를 고쳐")

    assert changed(first) == ["outputs/result.csv"]
    assert changed(second) == []
    assert (
        next(event for event in second if event["type"] == "message.completed")["data"]["files"]
        == []
    )
    assert changed(third) == ["outputs/result.csv"]
    files = (await logged_in.get(f"/api/console/sessions/{session_id}/files")).json()["items"]
    assert len([item for item in files if item["kind"] == "output"]) == 2
    messages = (await logged_in.get(f"/api/console/sessions/{session_id}/messages")).json()
    untouched = next(item for item in messages["items"] if item["text"] == "그대로 둔다")
    assert untouched["files"] == []


async def test_input_then_reconnect_continues_the_same_run(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(
        [*fake_tool_call("shell", {"command": "echo hi"}, call_id="call_1"), fake_end("tool_use")],
        fake_text("끝"),
    )
    first = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "실행해"}
    )
    run_id = first.headers["x-run-id"]
    last_seq = sse(first.text)[-1]["seq"]

    accepted = await logged_in.post(
        f"/api/console/runs/{run_id}/input", json={"decision": "approve"}
    )

    assert accepted.status_code == 202
    assert accepted.json()["id"] == run_id
    assert accepted.json()["status"] in ("queued", "running", "succeeded")
    again = await logged_in.get(
        f"/api/console/sessions/{session_id}/events", params={"after": last_seq}
    )
    events = sse(again.text)
    assert events[0]["type"] == "interaction.resolved"
    assert events[0]["seq"] == last_seq + 1
    assert all(event["run_id"] == run_id for event in events)
    assert events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["status"] == "succeeded"


async def test_reconnect_with_after_or_last_event_id(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(fake_text("짧은 답"))
    events = sse(
        (
            await logged_in.post(
                f"/api/console/sessions/{session_id}/messages", json={"text": "질문"}
            )
        ).text
    )

    by_query = sse(
        (
            await logged_in.get(f"/api/console/sessions/{session_id}/events", params={"after": 2})
        ).text
    )
    by_header = sse(
        (
            await logged_in.get(
                f"/api/console/sessions/{session_id}/events", headers={"Last-Event-ID": "2"}
            )
        ).text
    )

    assert [event["seq"] for event in by_query] == [event["seq"] for event in events[2:]]
    assert by_header == by_query
    persisted = (
        await logged_in.get(f"/api/console/runs/{events[0]['run_id']}/events", params={"after": 0})
    ).json()["items"]
    assert "message.delta" not in types(persisted)
    assert types(persisted)[-1] == "run.completed"


async def test_stop_kills_the_tool_and_finishes_cancelled(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in, approval_mode="off")
    script.add(
        [
            *fake_tool_call("shell", {"command": SLEEP, "timeout_sec": 60}, call_id="call_sleep"),
            fake_end("tool_use"),
        ]
    )

    stream = asyncio.create_task(
        logged_in.post(f"/api/console/sessions/{session_id}/messages", json={"text": "기다려"})
    )
    running = await wait_running(logged_in, session_id)
    assert running["exec_id"] == "call_sleep"
    run_id = (await logged_in.get(f"/api/console/sessions/{session_id}")).json()["active_run"]["id"]

    started = time.monotonic()
    stopped = await logged_in.post(f"/api/console/runs/{run_id}/stop", json={})
    response = await asyncio.wait_for(stream, 10)
    elapsed = time.monotonic() - started

    assert stopped.status_code == 200
    events = sse(response.text)
    assert events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["status"] == "cancelled"
    assert elapsed < 3
    tool = next(event for event in events if event["type"] == "tool_call.completed")["data"]
    assert tool["status"] == "cancelled"
    deadline = time.monotonic() + 1
    while pid_alive(running["pid"]) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not pid_alive(running["pid"])
    again = await logged_in.post(f"/api/console/runs/{run_id}/stop", json={})
    assert again.status_code == 409


async def test_question_is_answered_by_the_next_message(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in)

    def greet(request: ChatRequest) -> list[ModelEvent]:
        return fake_text(f"안녕하세요, {last_results(request)[0].content}님!")

    script.add(
        [
            *fake_tool_call("ask_user", {"question": "이름이 뭐예요?"}, call_id="call_q"),
            fake_end("tool_use"),
        ],
        greet,
    )
    first = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "인사해 줘"}
    )
    waiting = sse(first.text)[-2]
    assert waiting["type"] == "run.waiting_input"
    assert (waiting["data"]["kind"], waiting["data"]["question"]) == ("question", "이름이 뭐예요?")

    answer = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "지우"}
    )

    assert answer.headers["x-run-id"] == first.headers["x-run-id"]
    events = sse(answer.text)
    assert events[0]["type"] == "interaction.resolved"
    assert events[0]["data"]["answer"] == "지우"
    assert events[-2]["data"]["text"] == "안녕하세요, 지우님!"
    messages = (await logged_in.get(f"/api/console/sessions/{session_id}/messages")).json()
    assert [item["role"] for item in messages["items"]].count("user") == 1


async def test_blocking_mode_returns_the_result(
    logged_in: AsyncClient, make_session: Any, script: Any
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(fake_text("블로킹 답"))

    response = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages",
        json={"text": "질문", "response_mode": "blocking"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["status"] == "succeeded"
    assert body["answer"] == "블로킹 답"
    assert body["message_id"] == body["run"]["assistant_message_id"]


async def test_second_message_while_waiting_for_approval_is_rejected(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in)
    script.add([*fake_tool_call("shell", {"command": "echo x"}), fake_end("tool_use")])
    first = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "실행"}
    )

    second = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "다른 일"}
    )

    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "run_in_progress"
    assert error["details"]["run_id"] == first.headers["x-run-id"]
    assert error["details"]["interaction"] == "approval"


async def test_deleted_workspace_folder_is_recreated(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events, settings: Settings
) -> None:
    _, session_id = await make_session(logged_in)
    script.add(
        [
            *fake_tool_call("write_file", {"path": "outputs/a.txt", "content": "A"}),
            fake_end("tool_use"),
        ],
        fake_text("썼다"),
        [*fake_tool_call("list_files", {}), fake_end("tool_use")],
        fake_text("다시 만들어졌다"),
    )
    await logged_in.post(f"/api/console/sessions/{session_id}/messages", json={"text": "써"})
    folder = settings.data_dir / "workspaces" / session_id
    assert (folder / "outputs" / "a.txt").is_file()
    shutil.rmtree(folder)

    events = sse(
        (
            await logged_in.post(
                f"/api/console/sessions/{session_id}/messages", json={"text": "다시"}
            )
        ).text
    )

    assert "workspace.recreated" in types(events)
    assert events[-1]["data"]["status"] == "succeeded"
    assert folder.is_dir()


async def test_large_tool_output_is_downloadable_in_full(
    logged_in: AsyncClient, make_session: Any, script: Any, sse: Events
) -> None:
    _, session_id = await make_session(logged_in, approval_mode="off")
    script.add(
        [
            *fake_tool_call("shell", {"command": BIG_OUTPUT}, call_id="call_big"),
            fake_end("tool_use"),
        ],
        fake_text("많이 출력했다"),
    )
    response = await logged_in.post(
        f"/api/console/sessions/{session_id}/messages", json={"text": "출력해"}
    )
    events = sse(response.text)
    tool = next(event for event in events if event["type"] == "tool_call.completed")["data"]

    assert tool["output_bytes"] > 256 * 1024
    assert tool["output_storage_key"] is not None
    assert "bytes omitted" in tool["output_excerpt"]
    download = await logged_in.get(
        f"/api/console/runs/{response.headers['x-run-id']}/tool-calls/call_big/output"
    )
    assert download.status_code == 200
    lines = download.text.splitlines()
    assert len(lines) == 4000
    assert lines[0].startswith("line 1 ") and lines[-1].startswith("line 4000 ")
