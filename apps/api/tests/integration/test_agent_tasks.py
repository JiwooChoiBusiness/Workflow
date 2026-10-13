"""과업 모드(설계 4.10, 13.1, 수용 조건 A14).

POST /api/v1/agent-tasks로 입력·지시·출력 스키마를 보내면 스키마에 맞는 JSON 결과가 돌아오고,
ephemeral 과업은 끝나면 작업공간을 지운다(결과 파일은 남는다). 2단계 엔진이 쓰는 AgentTaskService도
같은 구현을 직접 부른다.
"""

import re
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.agent.interactions import InteractionReply
from app.agent.task_service import AgentTaskService, AgentTaskSpec
from app.config import Settings
from app.db.repositories.runs import RunRepository
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ForcedTool, ModelEvent, TextBlock, ToolResultBlock
from app.services.runs import RunService
from app.services.tasks import TaskService

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
SUM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"sum": {"type": "integer"}, "note": {"type": "string"}},
    "required": ["sum"],
    "additionalProperties": False,
}
Events = Callable[[str], list[dict[str, Any]]]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


@pytest.fixture
def api(make_client: Callable[[], AsyncClient]) -> AsyncClient:
    return make_client()


async def published_agent(
    client: AsyncClient, name: str = "Task Agent", **draft: Any
) -> dict[str, Any]:
    agent = (await client.post("/api/console/agents", json={"name": name})).json()
    current = (await client.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {key: current[key] for key in DRAFT_FIELDS}
    body.update(draft)
    saved = await client.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    assert saved.status_code == 200, saved.text
    published = await client.post(f"/api/console/agents/{agent['id']}/publish", json={})
    assert published.status_code == 201, published.text
    return agent


async def api_key(client: AsyncClient, agent_id: str, **fields: Any) -> dict[str, str]:
    response = await client.post(
        f"/api/console/agents/{agent_id}/api-keys", json={"name": "워크플로우", **fields}
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['key']}"}


def submit(data: dict[str, Any], call_id: str = "call_submit") -> list[ModelEvent]:
    # message_end를 빼면 가짜 어댑터가 휴리스틱 usage를 채운다.
    return fake_tool_call("submit_result", data, call_id=call_id)


def first_user_text(request: ChatRequest) -> str:
    message = next(message for message in request.messages if message.role == "user")
    return "".join(block.text for block in message.blocks if isinstance(block, TextBlock))


def tool_results(request: ChatRequest) -> list[ToolResultBlock]:
    return [
        block
        for message in request.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    ]


def recorder(
    seen: list[ChatRequest], events: Callable[[ChatRequest], list[ModelEvent]]
) -> Callable[[ChatRequest], list[ModelEvent]]:
    def turn(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return events(request)

    return turn


async def test_blocking_task_returns_a_schema_valid_result_and_drops_the_workspace(
    logged_in: AsyncClient, api: AsyncClient, script: Any, settings: Settings
) -> None:
    agent = await published_agent(logged_in, approval_mode="off")
    key = await api_key(logged_in, agent["id"])
    seen: list[ChatRequest] = []
    script.add(
        recorder(
            seen,
            lambda _: [
                *fake_tool_call(
                    "read_file", {"path": "task/inputs.json"}, index=0, call_id="call_read"
                ),
                *fake_tool_call(
                    "write_file",
                    {"path": "outputs/report.txt", "content": "2+3=5"},
                    index=1,
                    call_id="call_write",
                ),
                fake_end("tool_use"),
            ],
        ),
        recorder(seen, lambda _: [*fake_text("계산했다."), *submit({"sum": 5, "note": "ok"})]),
    )

    response = await api.post(
        "/api/v1/agent-tasks",
        headers=key,
        json={
            "inputs": {"a": 2, "b": 3},
            "instruction": "{{a}}와 {{b}}를 더해 sum에 담아 제출한다.",
            "output_schema": SUM_SCHEMA,
            "user": "workflow-1",
        },
    )

    assert response.status_code == 200, response.text
    task = response.json()
    assert (task["status"], task["result"], task["text"]) == (
        "succeeded",
        {"sum": 5, "note": "ok"},
        "계산했다.",
    )
    assert task["usage"]["input_tokens"] > 0 and task["error"] is None
    [report] = task["files"]
    assert (report["path"], report["downloadable"]) == ("outputs/report.txt", True)
    first = seen[0]
    assert "# 과업" in first.system and "<output_schema>" in first.system
    submit_spec = next(tool for tool in first.tools if tool.name == "submit_result")
    assert submit_spec.input_schema["properties"]["sum"] == {"type": "integer"}
    text = first_user_text(first)
    assert text.startswith("2와 3를 더해 sum에 담아 제출한다.")
    assert "입력 값(전체는 task/inputs.json):" in text
    read = next(block for block in tool_results(seen[1]) if block.tool_use_id == "call_read")
    assert '"a": 2' in read.content and '"b": 3' in read.content

    session_id = task["session_id"]
    assert not (settings.data_dir / "workspaces" / session_id).exists()
    detail = (await logged_in.get(f"/api/console/sessions/{session_id}")).json()
    assert detail["source"] == "task" and detail["workspace"] is None
    assert detail["external_user"] == "workflow-1"
    download = await api.get(f"/api/v1/files/{report['id']}/download", headers=key)
    assert download.status_code == 200 and download.content == b"2+3=5"
    again = (await api.get(f"/api/v1/agent-tasks/{task['run_id']}", headers=key)).json()
    assert again["result"] == {"sum": 5, "note": "ok"} and again["status"] == "succeeded"
    run = (await logged_in.get(f"/api/console/runs/{task['run_id']}")).json()
    assert run["trigger"] == "task"
    assert run["output"]["result"] == {"sum": 5, "note": "ok"}
    assert set(run["output"]) == {"status", "result", "text", "files", "usage"}


async def test_streaming_task_carries_the_output_in_run_completed(
    logged_in: AsyncClient, api: AsyncClient, script: Any, sse: Events
) -> None:
    agent = await published_agent(logged_in, approval_mode="off")
    key = await api_key(logged_in, agent["id"])
    script.add(submit({"sum": 7}))

    response = await api.post(
        "/api/v1/agent-tasks",
        headers=key,
        json={
            "inputs": {"x": 7},
            "instruction": "{{x}}를 그대로 낸다",
            "output_schema": SUM_SCHEMA,
            "response_mode": "streaming",
        },
    )

    events = sse(response.text)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert events[0]["type"] == "run.queued"
    completed = events[-1]
    assert completed["type"] == "run.completed"
    assert completed["data"]["status"] == "succeeded"
    assert completed["data"]["output"]["result"] == {"sum": 7}


async def test_text_answer_is_nudged_and_missing_result_fails(
    logged_in: AsyncClient, api: AsyncClient, script: Any
) -> None:
    agent = await published_agent(logged_in, approval_mode="off")
    key = await api_key(logged_in, agent["id"])
    seen: list[ChatRequest] = []
    script.add(
        recorder(seen, lambda _: fake_text("5입니다")),
        recorder(seen, lambda _: submit({"sum": 5})),
        fake_text("답은 1"),
        fake_text("정말 1"),
    )
    body = {"instruction": "더한다", "output_schema": SUM_SCHEMA}

    nudged = (await api.post("/api/v1/agent-tasks", headers=key, json=body)).json()
    missing = (await api.post("/api/v1/agent-tasks", headers=key, json=body)).json()

    assert nudged["status"] == "succeeded" and nudged["result"] == {"sum": 5}
    assert seen[0].params.tool_choice is None
    assert seen[1].params.tool_choice == ForcedTool(name="submit_result")
    assert missing["status"] == "failed"
    assert missing["error"]["code"] == "output_missing"
    assert missing["result"] is None and missing["text"] == "정말 1"


async def test_three_invalid_results_fail_the_task(
    logged_in: AsyncClient, api: AsyncClient, script: Any
) -> None:
    agent = await published_agent(logged_in, approval_mode="off")
    key = await api_key(logged_in, agent["id"])
    script.add(submit({"sum": "5"}, "c1"), submit({}, "c2"), submit({"sum": 5, "x": 1}, "c3"))

    task = (
        await api.post(
            "/api/v1/agent-tasks",
            headers=key,
            json={"instruction": "더한다", "output_schema": SUM_SCHEMA},
        )
    ).json()

    assert task["status"] == "failed"
    assert task["error"]["code"] == "output_invalid"
    assert task["result"] is None


async def test_reuse_keeps_the_workspace_and_refreshes_task_inputs(
    logged_in: AsyncClient, api: AsyncClient, script: Any, settings: Settings
) -> None:
    agent = await published_agent(logged_in, approval_mode="off")
    key = await api_key(logged_in, agent["id"])
    seen: list[ChatRequest] = []
    script.add(
        [
            *fake_tool_call("write_file", {"path": "memo.txt", "content": "첫 과업"}),
            fake_end("tool_use"),
        ],
        submit({"sum": 1}),
        recorder(
            seen,
            lambda _: [
                *fake_tool_call("read_file", {"path": "memo.txt"}, index=0, call_id="call_memo"),
                *fake_tool_call(
                    "read_file", {"path": "task/inputs.json"}, index=1, call_id="call_inputs"
                ),
                fake_end("tool_use"),
            ],
        ),
        recorder(seen, lambda _: submit({"sum": 2})),
    )

    first = (
        await api.post(
            "/api/v1/agent-tasks",
            headers=key,
            json={
                "instruction": "메모를 남긴다",
                "inputs": {"n": 1},
                "output_schema": SUM_SCHEMA,
                "session_mode": "reuse",
            },
        )
    ).json()
    session_id = first["session_id"]
    assert first["status"] == "succeeded"
    assert (settings.data_dir / "workspaces" / session_id / "memo.txt").is_file()

    second = (
        await api.post(
            "/api/v1/agent-tasks",
            headers=key,
            json={
                "instruction": "메모를 읽는다",
                "inputs": {"n": 2},
                "output_schema": SUM_SCHEMA,
                "session_mode": "reuse",
                "session_id": session_id,
            },
        )
    ).json()

    assert second["status"] == "succeeded" and second["session_id"] == session_id
    results = {block.tool_use_id: block.content for block in tool_results(seen[1])}
    assert "첫 과업" in results["call_memo"]
    assert '"n": 2' in results["call_inputs"]
    assert (settings.data_dir / "workspaces" / session_id).is_dir()


async def test_file_inputs_are_copied_into_task_files(
    logged_in: AsyncClient, api: AsyncClient, script: Any
) -> None:
    agent = await published_agent(logged_in, approval_mode="off")
    key = await api_key(logged_in, agent["id"])
    uploaded = (
        await api.post(
            "/api/v1/files/upload",
            headers=key,
            files={"file": ("data.csv", b"a\nb\n", "text/csv")},
        )
    ).json()
    seen: list[ChatRequest] = []

    def read_the_file(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        match = re.search(r"(task/files/\S+) 파일", first_user_text(request))
        assert match is not None
        return [
            *fake_tool_call("read_file", {"path": match.group(1)}, call_id="call_doc"),
            fake_end("tool_use"),
        ]

    script.add(read_the_file, recorder(seen, lambda _: submit({"sum": 2})))

    task = (
        await api.post(
            "/api/v1/agent-tasks",
            headers=key,
            json={
                "instruction": "{{doc}} 파일의 줄 수를 sum에 담는다",
                "inputs": {"doc": {"file_id": uploaded["file_id"]}},
                "output_schema": SUM_SCHEMA,
            },
        )
    ).json()

    assert task["status"] == "succeeded"
    assert "task/files/data.csv 파일" in first_user_text(seen[0])
    [content] = [block.content for block in tool_results(seen[1])]
    assert "a\nb\n" in content


async def test_task_requests_are_checked(
    logged_in: AsyncClient, api: AsyncClient, script: Any
) -> None:
    agent = await published_agent(logged_in, approval_mode="off")
    key = await api_key(logged_in, agent["id"])
    chat_only = await api_key(logged_in, agent["id"], scopes=["chat"])
    script.add(fake_text("대화"))
    conversation = (
        await api.post(
            "/api/v1/chat-messages",
            headers=key,
            json={"query": "안녕", "response_mode": "blocking"},
        )
    ).json()

    def post(body: dict[str, Any], headers: dict[str, str] = key) -> Any:
        return api.post("/api/v1/agent-tasks", headers=headers, json=body)

    open_schema = {"type": "object", "properties": {"sum": {"type": "integer"}}}
    cases = [
        (
            await post({"instruction": "x", "output_schema": open_schema}),
            422,
            "output_schema_invalid",
        ),
        (await post({"instruction": "{{ 없는_변수 }}"}), 422, "instruction_invalid"),
        (await post({"instruction": "x", "files": [str(uuid.uuid4())]}), 422, "file_not_found"),
        (
            await post({"instruction": "x", "inputs": {"f": {"file_id": "nope"}}}),
            422,
            "file_not_found",
        ),
        (
            await post(
                {
                    "instruction": "x",
                    "session_mode": "reuse",
                    "session_id": conversation["conversation_id"],
                }
            ),
            404,
            "not_found",
        ),
        (
            await post({"instruction": "x", "session_id": conversation["conversation_id"]}),
            422,
            "session_id_not_allowed",
        ),
        (await post({"instruction": "x"}, chat_only), 403, "scope_not_allowed"),
    ]
    chat_run = await api.get(f"/api/v1/agent-tasks/{conversation['run_id']}", headers=key)

    for response, status, code in cases:
        assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert chat_run.status_code == 404


async def test_agent_task_service_waits_through_approval_and_cancel(
    logged_in: AsyncClient, app: FastAPI, settings: Settings, script: Any
) -> None:
    agent = await published_agent(logged_in)  # 승인 모드 all_shell
    workspace_id = uuid.UUID(
        (await logged_in.get("/api/console/auth/me")).json()["workspace"]["id"]
    )
    state = app.state
    runs = RunService(
        state.db,
        settings,
        bus=state.bus,
        supervisor=state.supervisor,
        storage=state.storage,
        backend=state.sandbox,
    )
    service: AgentTaskService = TaskService(state.db, settings, runs=runs, bus=state.bus)
    spec = AgentTaskSpec(
        workspace_id=workspace_id,
        agent_id=uuid.UUID(agent["id"]),
        instruction="echo를 실행하고 결과를 낸다",
        output_schema=SUM_SCHEMA,
        correlation={"workflow_run_id": "wf-1", "node_id": "n-1"},
    )
    script.add(
        [
            *fake_tool_call("shell", {"command": "echo task"}, call_id="call_sh"),
            fake_end("tool_use"),
        ],
        submit({"sum": 1}),
        [*fake_tool_call("shell", {"command": "echo 2"}, call_id="call_sh2"), fake_end("tool_use")],
    )

    run_id = await service.start(spec)
    waiting = await service.wait(run_id, timeout=30)
    await service.provide_input(run_id, InteractionReply(decision="approve"))
    done = await service.wait(run_id, timeout=30)
    events = [event async for event in service.events(run_id)]

    assert waiting.status == "waiting_input"
    assert waiting.pending_interaction is not None
    assert waiting.pending_interaction.tool_name == "shell"
    assert done.status == "succeeded" and done.result == {"sum": 1}
    assert (await service.wait(run_id, timeout=5)).status == "succeeded"
    event_types = [event.type for event in events]
    assert "interaction.resolved" in event_types
    assert events[-1].type == "run.completed" and events[-1].data["output"]["result"] == {"sum": 1}
    async with state.db.read() as session:
        run = await RunRepository(session, workspace_id).get(run_id)
    assert run is not None and run.input is not None
    assert run.input["correlation"] == {"workflow_run_id": "wf-1", "node_id": "n-1"}

    second = await service.start(spec)
    paused = await service.wait(second, timeout=30)
    await service.cancel(second)
    await service.cancel(second)
    cancelled = await service.wait(second, timeout=5)

    assert paused.status == "waiting_input"
    assert cancelled.status == "cancelled" and cancelled.result is None
    assert not (settings.data_dir / "workspaces" / str(paused.session_id)).exists()
