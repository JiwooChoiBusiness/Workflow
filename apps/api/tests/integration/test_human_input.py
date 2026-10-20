"""human_input 노드의 폼 대기와 재개(2단계 설계 2.4, 3.7, 6장의 /input, M19).

콘솔(/api/console/wf-runs/{id}/input), 웹앱(/api/web/wf-runs/{id}/input), 서비스 API
(/api/v1/workflows/run/{id}/input, /api/v1/chat-messages/{id}/input) 세 경로에서 폼으로 멈추고
kind=form, values로 재개하는지, 값 검사(422 invalid_inputs)와 파일 칸(올린 wf_file id), 다른 종류의
응답(409 wrong_input_kind), 대기 중 중지, 대기 상한(input_timeout), 재기동 뒤 대기 유지를 본다.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import sysinfo
from app.config import Settings
from app.db.models import WfFile, WfRun
from app.db.types import utcnow
from app.housekeeping import WorkflowRetention
from app.logsetup import configure_logging
from app.main import create_app
from app.runner import recovery
from app.services.workflow_runs import WorkflowParents

Events = Callable[[str], list[dict[str, Any]]]
FETCH = {"X-Requested-With": "fetch"}
BASE_URL = "http://127.0.0.1:8500"
EMAIL = "owner@example.com"
PASSWORD = "correct-horse-battery"  # noqa: S105  테스트 계정
FIELDS = [
    {"name": "decision", "type": "select", "options": ["승인", "반려"], "required": True},
    {"name": "qty", "type": "number"},
    {"name": "doc", "type": "file"},
]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


def node(node_id: str, node_type: str, config: dict[str, Any], x: float = 0) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "title": node_id,
        "position": {"x": x, "y": 0},
        "config": config,
    }


def edges(*pairs: str) -> list[dict[str, Any]]:
    return [
        {"id": f"e{index}", "source": source, "target": target}
        for index, (source, target) in enumerate((pair.split(">") for pair in pairs), start=1)
    ]


def form_flow() -> dict[str, Any]:
    return {
        "nodes": [
            node("start", "start", {"fields": [{"name": "topic", "type": "string"}]}),
            node(
                "human_input_1",
                "human_input",
                {"description": "{{ start.topic }}을(를) 검토한다", "fields": FIELDS},
                300,
            ),
            node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": name, "var": f"human_input_1.{name}"}
                        for name in ("decision", "qty", "doc")
                    ]
                },
                600,
            ),
        ],
        "edges": edges("start>human_input_1", "human_input_1>end_1"),
    }


def parallel_form_flow() -> dict[str, Any]:
    return {
        "nodes": [
            node("start", "start", {"fields": []}),
            node("human_input_a", "human_input", {"fields": FIELDS[:1]}, 300),
            node("human_input_b", "human_input", {"fields": FIELDS[:1]}, 300),
            node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "a", "var": "human_input_a.decision"},
                        {"name": "b", "var": "human_input_b.decision"},
                    ]
                },
                600,
            ),
        ],
        "edges": edges(
            "start>human_input_a",
            "start>human_input_b",
            "human_input_a>end_1",
            "human_input_b>end_1",
        ),
    }


def chat_form_flow() -> dict[str, Any]:
    return {
        "nodes": [
            node("start", "start", {"fields": []}),
            node(
                "human_input_1",
                "human_input",
                {"description": "{{ sys.query }}", "fields": FIELDS[:1]},
                300,
            ),
            node("answer_1", "answer", {"text": "결정: {{ human_input_1.decision }}"}, 600),
        ],
        "edges": edges("start>human_input_1", "human_input_1>answer_1"),
    }


async def make_workflow(
    api: AsyncClient,
    graph: dict[str, Any],
    *,
    kind: str = "workflow",
    publish: bool = False,
    webapp: bool = False,
) -> dict[str, Any]:
    created = await api.post("/api/console/workflows", json={"name": "검토 폼", "kind": kind})
    assert created.status_code == 201, created.text
    workflow = created.json()
    current = (await api.get(f"/api/console/workflows/{workflow['id']}/draft")).json()
    saved = await api.put(
        f"/api/console/workflows/{workflow['id']}/draft",
        json={
            "graph": graph,
            "env_vars": [],
            "conversation_vars": [],
            "settings": current["settings"],
            "updated_at": current["updated_at"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["issues"] == [], saved.json()["issues"]
    if publish:
        published = await api.post(f"/api/console/workflows/{workflow['id']}/publish", json={})
        assert published.status_code == 201, published.text
    if webapp:
        patched = await api.patch(
            f"/api/console/workflows/{workflow['id']}", json={"webapp_enabled": True}
        )
        assert patched.status_code == 200, patched.text
    return workflow


def types(events: list[dict[str, Any]]) -> list[str]:
    return [event["type"] for event in events if event["type"] != "ping"]


def error(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


async def start_waiting(api: AsyncClient, sse: Events, workflow_id: str) -> tuple[str, Any]:
    response = await api.post(
        f"/api/console/workflows/{workflow_id}/draft/runs", json={"inputs": {"topic": "보고서"}}
    )
    assert response.status_code == 200, response.text
    events = sse(response.text)
    assert types(events)[-2:] == ["workflow.waiting_input", "workflow.completed"]
    run_id = response.headers["x-run-id"]
    run = (await api.get(f"/api/console/wf-runs/{run_id}")).json()
    assert run["status"] == "waiting_input", run
    return run_id, events


async def wait_status(api: AsyncClient, path: str, *statuses: str, timeout: float = 20) -> Any:
    deadline = time.monotonic() + timeout
    body: Any = None
    while time.monotonic() < deadline:
        body = (await api.get(path)).json()
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path}가 {statuses}가 되지 않았다: {body}")


async def test_console_run_waits_for_the_form_and_resumes(
    app: FastAPI, logged_in: AsyncClient, sse: Events
) -> None:
    workflow = await make_workflow(logged_in, form_flow())

    run_id, events = await start_waiting(logged_in, sse, workflow["id"])

    waiting = next(event["data"] for event in events if event["type"] == "workflow.waiting_input")
    assert (waiting["kind"], waiting["interaction"], waiting["node_id"]) == (
        "form",
        None,
        "human_input_1",
    )
    assert waiting["form"]["description"] == "보고서을(를) 검토한다"
    assert [field["name"] for field in waiting["form"]["fields"]] == ["decision", "qty", "doc"]
    run = (await logged_in.get(f"/api/console/wf-runs/{run_id}")).json()
    pending = run["pending_interaction"]
    assert pending["kind"] == "form" and pending["interaction"] is None
    assert pending["form"]["form_id"] == waiting["form"]["form_id"]
    node_run = next(item for item in run["node_runs"] if item["node_id"] == "human_input_1")
    assert node_run["status"] == "waiting_input"

    wrong = await logged_in.post(f"/api/console/wf-runs/{run_id}/input", json={"answer": "네"})
    mixed = await logged_in.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"kind": "form", "values": {}, "decision": "approve"},
    )
    invalid = await logged_in.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"kind": "form", "values": {"decision": "보류", "qty": "많이"}},
    )
    unknown_file = await logged_in.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"values": {"decision": "승인", "doc": str(uuid.uuid4())}},
    )

    assert error(wrong) == (409, "wrong_input_kind")
    assert error(mixed) == (422, "invalid_input")
    assert error(invalid) == (422, "invalid_inputs")
    assert set(invalid.json()["error"]["details"]) == {"decision", "qty"}
    assert error(unknown_file) == (422, "file_not_found")
    assert (await logged_in.get(f"/api/console/wf-runs/{run_id}")).json()["status"] == (
        "waiting_input"
    )

    upload = await logged_in.post(
        f"/api/console/workflows/{workflow['id']}/files",
        files={"file": ("근거.txt", "자료".encode(), "text/plain")},
        headers=FETCH,
    )
    assert upload.status_code == 201, upload.text
    file_id = upload.json()["id"]
    finished = await logged_in.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={
            "kind": "form",
            "values": {"decision": "승인", "qty": "2", "doc": file_id},
            "response_mode": "blocking",
        },
    )

    assert finished.status_code == 200, finished.text
    body = finished.json()
    assert body["status"] == "succeeded", body
    assert body["outputs"]["decision"] == "승인" and body["outputs"]["qty"] == 2
    assert body["outputs"]["doc"]["file_id"] == file_id
    assert body["pending_interaction"] is None
    # 폼 파일 칸으로 받은 업로드도 이 런에 묶여 보존 정리가 따로 지우지 않는다.
    async with app.state.db.read() as session:
        bound = await session.get(WfFile, uuid.UUID(file_id))
    assert bound is not None and str(bound.wf_run_id) == run_id
    again = await logged_in.post(
        f"/api/console/wf-runs/{run_id}/input", json={"kind": "form", "values": {}}
    )
    assert error(again) == (409, "run_not_waiting")
    log = (await logged_in.get(f"/api/console/wf-runs/{run_id}/event-log")).json()["items"]
    assert "workflow.resumed" in [item["type"] for item in log]


async def test_webapp_run_waits_and_the_form_answer_streams_the_rest(
    logged_in: AsyncClient, sse: Events
) -> None:
    workflow = await make_workflow(logged_in, form_flow(), publish=True, webapp=True)
    slug = workflow["slug"]

    blocking = await logged_in.post(
        f"/api/web/flows/{slug}/runs",
        json={"inputs": {"topic": "웹앱"}, "response_mode": "blocking"},
    )

    assert blocking.status_code == 200, blocking.text
    result = blocking.json()
    assert result["status"] == "waiting_input"
    assert result["pending_interaction"]["kind"] == "form"
    run_id = result["run_id"]
    upload = await logged_in.post(
        f"/api/web/flows/{slug}/files",
        files={"file": ("사진.txt", b"x", "text/plain")},
        headers=FETCH,
    )
    assert upload.status_code == 201, upload.text

    resumed = await logged_in.post(
        f"/api/web/wf-runs/{run_id}/input",
        json={
            "kind": "form",
            "values": {"decision": "반려", "doc": upload.json()["id"]},
            "response_mode": "streaming",
        },
    )

    assert resumed.status_code == 200, resumed.text
    events = sse(resumed.text)
    assert types(events)[0] == "workflow.queued"
    assert "workflow.resumed" in types(events)
    completed = events[-1]
    assert completed["type"] == "workflow.completed"
    assert completed["data"]["status"] == "succeeded"
    assert completed["data"]["outputs"]["decision"] == "반려"
    detail = (await logged_in.get(f"/api/web/wf-runs/{run_id}")).json()
    assert detail["status"] == "succeeded" and detail["trigger"] == "webapp"


async def test_service_api_workflow_key_answers_the_form(
    logged_in: AsyncClient, make_client: Callable[[], AsyncClient]
) -> None:
    workflow = await make_workflow(logged_in, form_flow(), publish=True)
    created = await logged_in.post(
        f"/api/console/workflows/{workflow['id']}/api-keys", json={"name": "자동화"}
    )
    key = {"Authorization": f"Bearer {created.json()['key']}"}
    api = make_client()

    started = await api.post(
        "/api/v1/workflows/run",
        headers=key,
        json={"inputs": {"topic": "API"}, "user": "u-1", "response_mode": "blocking"},
    )

    assert started.status_code == 200, started.text
    body = started.json()
    assert body["status"] == "waiting_input" and body["pending_interaction"]["kind"] == "form"
    run_id = body["workflow_run_id"]
    upload = await api.post(
        "/api/v1/files/upload",
        headers=key,
        files={"file": ("근거.txt", b"x", "text/plain")},
        data={"user": "u-1"},
    )
    assert upload.status_code == 201, upload.text
    stranger = await api.post(
        f"/api/v1/workflows/run/{run_id}/input",
        headers=key,
        json={"kind": "form", "values": {"decision": "승인"}, "user": "u-2"},
    )
    assert stranger.status_code == 404

    finished = await api.post(
        f"/api/v1/workflows/run/{run_id}/input",
        headers=key,
        json={
            "kind": "form",
            "values": {"decision": "승인", "doc": upload.json()["file_id"]},
            "user": "u-1",
            "response_mode": "blocking",
        },
    )

    assert finished.status_code == 200, finished.text
    result = finished.json()
    assert result["status"] == "succeeded", result
    assert result["outputs"]["decision"] == "승인"
    assert result["outputs"]["doc"]["file_id"] == upload.json()["file_id"]


async def test_service_api_chatflow_key_answers_the_form(
    logged_in: AsyncClient, make_client: Callable[[], AsyncClient]
) -> None:
    workflow = await make_workflow(logged_in, chat_form_flow(), kind="chatflow", publish=True)
    created = await logged_in.post(
        f"/api/console/workflows/{workflow['id']}/api-keys", json={"name": "챗"}
    )
    key = {"Authorization": f"Bearer {created.json()['key']}"}
    api = make_client()

    sent = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={"query": "환불 승인할까요?", "user": "u-1", "response_mode": "blocking"},
    )

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["status"] == "waiting_input"
    pending = body["pending_interaction"]
    assert pending["kind"] == "form" and pending["form"]["description"] == "환불 승인할까요?"
    busy = await api.post(
        "/api/v1/chat-messages",
        headers=key,
        json={"query": "또", "user": "u-1", "conversation_id": body["conversation_id"]},
    )
    assert error(busy) == (409, "run_in_progress")
    assert busy.json()["error"]["details"]["interaction"] == "form"

    answered = await api.post(
        f"/api/v1/chat-messages/{body['run_id']}/input",
        headers=key,
        json={
            "kind": "form",
            "values": {"decision": "승인"},
            "user": "u-1",
            "response_mode": "blocking",
        },
    )

    assert answered.status_code == 200, answered.text
    result = answered.json()
    assert result["status"] == "succeeded", result
    assert result["answer"] == "결정: 승인"


async def test_agent_keys_cannot_send_a_form(
    logged_in: AsyncClient, make_client: Callable[[], AsyncClient]
) -> None:
    agent = (await logged_in.post("/api/console/agents", json={"name": "에이전트"})).json()
    await logged_in.post(f"/api/console/agents/{agent['id']}/publish", json={})
    created = await logged_in.post(
        f"/api/console/agents/{agent['id']}/api-keys", json={"name": "k"}
    )
    key = {"Authorization": f"Bearer {created.json()['key']}"}

    response = await make_client().post(
        f"/api/v1/chat-messages/{uuid.uuid4()}/input",
        headers=key,
        json={"kind": "form", "values": {"a": 1}},
    )

    assert error(response) == (422, "invalid_input")


async def test_stopping_a_waiting_form_run(logged_in: AsyncClient, sse: Events) -> None:
    workflow = await make_workflow(logged_in, form_flow())
    run_id, _ = await start_waiting(logged_in, sse, workflow["id"])

    stopped = await logged_in.post(f"/api/console/wf-runs/{run_id}/stop", headers=FETCH)

    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "cancelled"
    detail = (await logged_in.get(f"/api/console/wf-runs/{run_id}")).json()
    node_run = next(item for item in detail["node_runs"] if item["node_id"] == "human_input_1")
    assert node_run["status"] == "cancelled" and node_run["ended_at"] is not None
    late = await logged_in.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"kind": "form", "values": {"decision": "승인"}},
    )
    assert error(late) == (409, "run_not_waiting")


async def test_forms_expire_after_the_waiting_limit(
    app: FastAPI, logged_in: AsyncClient, sse: Events
) -> None:
    workflow = await make_workflow(logged_in, form_flow())
    run_id, _ = await start_waiting(logged_in, sse, workflow["id"])
    state = app.state
    ttl = state.settings.waiting_input_ttl_hours
    now = WorkflowParents(state.db, state.bus, state.workflow_supervisor, waiting_ttl_hours=ttl)
    # 하우스키핑이 매시 부르는 경로. 상한이 지나면 timeout 응답으로 다시 넣는다.
    later = WorkflowParents(
        state.db,
        state.bus,
        state.workflow_supervisor,
        waiting_ttl_hours=ttl,
        clock=lambda: utcnow() + timedelta(hours=ttl + 1),
    )

    retention = WorkflowRetention(
        state.db,
        storage=state.storage,
        backend=state.wf_sandbox,
        retention_days=90,
        forms=later,
    )
    job = dict(retention.jobs())["wf_form_timeouts"]

    assert await now.expire_forms() == 0
    assert await job() == 1

    expired = await wait_status(logged_in, f"/api/console/wf-runs/{run_id}", "failed")
    assert expired["error"]["code"] == "input_timeout"
    assert expired["error"]["node_id"] == "human_input_1"
    assert await job() == 0


async def test_a_form_waiting_beside_an_expired_one_fails_with_the_run(
    app: FastAPI, logged_in: AsyncClient, sse: Events
) -> None:
    workflow = await make_workflow(logged_in, parallel_form_flow())
    run_id, _ = await start_waiting(logged_in, sse, workflow["id"])
    waiting = (await logged_in.get(f"/api/console/wf-runs/{run_id}")).json()
    assert waiting["pending_interaction"]["node_id"] == "human_input_a"
    forms = [item for item in waiting["node_runs"] if item["node_type"] == "human_input"]
    assert [item["status"] for item in forms] == ["waiting_input"] * 2
    state = app.state
    ttl = state.settings.waiting_input_ttl_hours
    later = WorkflowParents(
        state.db,
        state.bus,
        state.workflow_supervisor,
        waiting_ttl_hours=ttl,
        clock=lambda: utcnow() + timedelta(hours=ttl + 1),
    )

    assert await later.expire_forms() == 1

    ended = await wait_status(logged_in, f"/api/console/wf-runs/{run_id}", "failed")
    assert ended["error"]["code"] == "input_timeout"
    closed = {
        item["node_id"]: (item["status"], item["error"]["code"], item["ended_at"] is not None)
        for item in ended["node_runs"]
        if item["node_type"] == "human_input"
    }
    # 만료된 a는 엔진이, 다시 멈춘 b는 런이 끝날 때 같은 오류로 닫는다.
    assert closed == {
        "human_input_a": ("failed", "input_timeout", True),
        "human_input_b": ("failed", "input_timeout", True),
    }


async def test_answering_an_expired_form_is_refused(
    app: FastAPI, logged_in: AsyncClient, sse: Events
) -> None:
    workflow = await make_workflow(logged_in, form_flow())
    run_id, _ = await start_waiting(logged_in, sse, workflow["id"])
    ttl = app.state.settings.waiting_input_ttl_hours
    async with app.state.db.write() as session:
        row = await session.get(WfRun, uuid.UUID(run_id))
        assert row is not None
        pending = dict(row.pending_interaction)
        form = dict(pending["form"])
        form["created_at"] = (utcnow() - timedelta(hours=ttl + 1)).isoformat()
        row.pending_interaction = {**pending, "form": form}

    late = await logged_in.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"kind": "form", "values": {"decision": "승인"}},
    )

    assert error(late) == (409, "input_timeout")
    ended = await wait_status(logged_in, f"/api/console/wf-runs/{run_id}", "failed")
    assert ended["error"]["code"] == "input_timeout"


async def test_recovery_leaves_form_waits_alone(
    app: FastAPI, logged_in: AsyncClient, sse: Events
) -> None:
    workflow = await make_workflow(logged_in, form_flow())
    run_id, _ = await start_waiting(logged_in, sse, workflow["id"])
    state = app.state

    result = await recovery.recover_workflows(state.db, state.workflow_supervisor, state.bus)

    assert result["resumed"] == 0
    assert (await logged_in.get(f"/api/console/wf-runs/{run_id}")).json()["status"] == (
        "waiting_input"
    )


@asynccontextmanager
async def serve(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, *, first: bool = False
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    application = create_app(settings)
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
                yield client
    finally:
        configure_logging("INFO")


async def test_a_waiting_form_survives_a_restart(
    short_settings: Settings, monkeypatch: pytest.MonkeyPatch, sse: Events
) -> None:
    async with serve(short_settings, monkeypatch, first=True) as client:
        workflow = await make_workflow(client, form_flow())
        run_id, _ = await start_waiting(client, sse, workflow["id"])

    async with serve(short_settings, monkeypatch) as client:
        kept = (await client.get(f"/api/console/wf-runs/{run_id}")).json()
        assert kept["status"] == "waiting_input"
        assert kept["pending_interaction"]["kind"] == "form"

        finished = await client.post(
            f"/api/console/wf-runs/{run_id}/input",
            json={"kind": "form", "values": {"decision": "승인"}, "response_mode": "blocking"},
        )

        assert finished.status_code == 200, finished.text
        assert finished.json()["status"] == "succeeded"
        assert finished.json()["outputs"]["decision"] == "승인"
