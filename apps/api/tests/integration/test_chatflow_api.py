"""챗플로우 대화 API(2단계 설계 6.1, 6.2, A6, 10장 M17).

미리보기 대화 3턴에서 메모리 창과 대화 변수가 다음 턴에 보이고, 실패한 런은 대화 변수를 바꾸지
않는다. llm → answer는 델타가 llm과 함께 흐르고 llm → if_else → answer는 분기 뒤 한 번에 나온다.
입력 대기 중인 대화에 보낸 메시지는 409이고, 승인 뒤 재개해 끝나면 답 메시지가 채워진다. 웹앱 대화는
발행 버전으로 돌고 웹앱 범위 밖 대화는 404다.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.db.models import WfFile
from app.db.repositories.workspaces import WorkspaceRepository
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ErrorEvent, ModelEvent, TextBlock
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
SHELL = [*fake_tool_call("shell", {"command": "echo chat"}), fake_end("tool_use")]
CONVERSATION_VARS = [
    {"name": "turns", "type": "number", "default": 0},
    {"name": "last", "type": "string", "default": ""},
]


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
    found: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs, start=1):
        source, target = pair.split(">")
        source, _, handle = source.partition(":")
        found.append(
            {
                "id": f"e{index}",
                "source": source,
                "target": target,
                "source_handle": handle or "out",
            }
        )
    return found


def llm(text: str, **config: Any) -> dict[str, Any]:
    return {"prompt": [{"role": "user", "text": text}], **config}


def memory_flow() -> dict[str, Any]:
    return {
        "nodes": [
            node("start", "start", {"fields": [{"name": "lang", "type": "string"}]}),
            node(
                "count",
                "variable_assigner",
                {
                    "items": [
                        {"target": "conv.turns", "op": "+=", "value": 1},
                        {"target": "conv.last", "op": "overwrite", "var": "sys.query"},
                    ]
                },
                200,
            ),
            node(
                "llm_1",
                "llm",
                llm(
                    "{{ sys.query }} (턴 {{ sys.turn }}, 누적 {{ conv.turns }}, {{ start.lang }})",
                    memory={"enabled": True, "window_turns": 1},
                ),
                400,
            ),
            node("answer_1", "answer", {"text": "{{ llm_1.text }}"}, 600),
        ],
        "edges": edges("start>count", "count>llm_1", "llm_1>answer_1"),
    }


async def chatflow(
    api: AsyncClient,
    graph: dict[str, Any],
    *,
    name: str = "상담",
    conversation_vars: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    created = await api.post("/api/console/workflows", json={"name": name, "kind": "chatflow"})
    assert created.status_code == 201, created.text
    workflow = created.json()
    current = (await api.get(f"/api/console/workflows/{workflow['id']}/draft")).json()
    body = {
        "graph": graph,
        "env_vars": [],
        "conversation_vars": conversation_vars or [],
        "settings": current["settings"],
        "updated_at": current["updated_at"],
    }
    saved = await api.put(f"/api/console/workflows/{workflow['id']}/draft", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["issues"] == [], saved.json()["issues"]
    return workflow


async def preview(api: AsyncClient, workflow_id: str) -> str:
    response = await api.post(
        f"/api/console/workflows/{workflow_id}/preview/conversations", json={}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def published_agent(api: AsyncClient) -> str:
    agent = (await api.post("/api/console/agents", json={"name": "대화 확인 담당"})).json()
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


def position(events: list[dict[str, Any]], event_type: str, node_id: str | None = None) -> int:
    for index, event in enumerate(events):
        if event["type"] == event_type and (
            node_id is None or event["data"].get("node_id") == node_id
        ):
            return index
    raise AssertionError(f"{event_type}({node_id})가 없다")


def conversation(request: ChatRequest) -> list[tuple[str, str]]:
    return [
        (message.role, "".join(b.text for b in message.blocks if isinstance(b, TextBlock)))
        for message in request.messages
    ]


def answering(seen: list[ChatRequest], text: str) -> Callable[[ChatRequest], list[ModelEvent]]:
    def turn(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return [*fake_text(text, chunk_size=4), fake_end()]

    return turn


async def test_three_preview_turns_carry_memory_and_conversation_variables(
    logged_in: AsyncClient, script: Any, sse: Events
) -> None:
    workflow = await chatflow(logged_in, memory_flow(), conversation_vars=CONVERSATION_VARS)
    cid = await preview(logged_in, workflow["id"])
    seen: list[ChatRequest] = []
    script.add(
        answering(seen, "첫 답이다"),
        answering(seen, "둘째 답이다"),
        answering(seen, "셋째 답이다"),
    )
    path = f"/api/console/wf-conversations/{cid}/messages"

    first = await logged_in.post(path, json={"text": "첫 질문", "inputs": {"lang": "ko"}})

    assert first.status_code == 200, first.text
    events = sse(first.text)
    assert types(events)[0] == "workflow.queued" and events[-1]["type"] == "workflow.completed"
    assert events[-1]["data"]["status"] == "succeeded"
    assert {event["conversation_id"] for event in events} == {cid}
    assert first.headers["x-conversation-id"] == cid
    completed = events[position(events, "answer.completed")]["data"]
    assert completed == {"text": "첫 답이다", "message_id": first.headers["x-message-id"]}
    assert "".join(e["data"]["text"] for e in events if e["type"] == "answer.delta") == "첫 답이다"
    variables = (await logged_in.get(f"/api/console/wf-conversations/{cid}/variables")).json()
    assert {item["name"]: item["value"] for item in variables["items"]} == {
        "turns": 1,
        "last": "첫 질문",
    }

    # inputs를 비우면 앞 메시지의 start 값을 다시 쓴다.
    second = await logged_in.post(path, json={"text": "둘째 질문", "response_mode": "blocking"})
    third = await logged_in.post(path, json={"text": "셋째 질문", "response_mode": "blocking"})

    assert second.status_code == 200 and third.status_code == 200, third.text
    result = third.json()
    assert result["answer"] == "셋째 답이다" and result["conversation_id"] == cid
    assert result["run"]["status"] == "succeeded" and result["run"]["kind"] == "chatflow"
    assert result["message_id"] == result["run"]["message_id"]
    # window_turns=1이라 셋째 턴은 바로 앞 턴만 기억한다.
    assert conversation(seen[1]) == [
        ("user", "첫 질문"),
        ("assistant", "첫 답이다"),
        ("user", "둘째 질문 (턴 2, 누적 2, ko)"),
    ]
    assert conversation(seen[2]) == [
        ("user", "둘째 질문"),
        ("assistant", "둘째 답이다"),
        ("user", "셋째 질문 (턴 3, 누적 3, ko)"),
    ]
    variables = (await logged_in.get(f"/api/console/wf-conversations/{cid}/variables")).json()
    assert {item["name"]: item["value"] for item in variables["items"]} == {
        "turns": 3,
        "last": "셋째 질문",
    }
    messages = (await logged_in.get(f"/api/console/wf-conversations/{cid}/messages")).json()
    assert [(m["seq"], m["role"], m["text"], m["status"]) for m in messages["items"]] == [
        (1, "user", "첫 질문", None),
        (2, "assistant", "첫 답이다", "succeeded"),
        (3, "user", "둘째 질문", None),
        (4, "assistant", "둘째 답이다", "succeeded"),
        (5, "user", "셋째 질문", None),
        (6, "assistant", "셋째 답이다", "succeeded"),
    ]
    assert messages["items"][0]["inputs"] == {"lang": "ko"}
    assert messages["items"][1]["run_id"] == messages["items"][0]["run_id"]
    assert messages["items"][1]["usage"]["output_tokens"] >= 0
    detail = (await logged_in.get(f"/api/console/wf-conversations/{cid}")).json()
    assert detail["message_count"] == 6 and detail["title"] == "첫 질문"
    assert detail["active_run"] is None and detail["last_run"]["id"] == result["run"]["id"]
    runs = (
        await logged_in.get(
            f"/api/console/workflows/{workflow['id']}/runs", params={"conversation_id": cid}
        )
    ).json()
    assert len(runs["items"]) == 3 and {item["trigger"] for item in runs["items"]} == {"preview"}
    # 노드 런 기록에 그 턴의 sys 값과 메모리가 남는다.
    run_id = result["run"]["id"]
    nodes = (await logged_in.get(f"/api/console/wf-runs/{run_id}/node-runs")).json()["items"]
    start_id = next(item["id"] for item in nodes if item["node_id"] == "start")
    llm_id = next(item["id"] for item in nodes if item["node_id"] == "llm_1")
    start = (await logged_in.get(f"/api/console/wf-runs/{run_id}/node-runs/{start_id}")).json()
    assert start["inputs"]["sys.query"] == "셋째 질문" and start["inputs"]["sys.turn"] == 3
    assert start["inputs"]["sys.conversation_id"] == cid
    assert start["inputs"]["sys.message_id"] == result["message_id"]
    llm_run = (await logged_in.get(f"/api/console/wf-runs/{run_id}/node-runs/{llm_id}")).json()
    assert llm_run["inputs"]["memory"] == [
        {"role": "user", "text": "둘째 질문"},
        {"role": "assistant", "text": "둘째 답이다"},
    ]


async def test_a_failed_turn_keeps_variables_and_is_left_out_of_memory(
    logged_in: AsyncClient, script: Any
) -> None:
    workflow = await chatflow(logged_in, memory_flow(), conversation_vars=CONVERSATION_VARS)
    cid = await preview(logged_in, workflow["id"])
    seen: list[ChatRequest] = []
    broken = [ErrorEvent(code="model_error", message="모델이 거절했다", retryable=False)]
    script.add(answering(seen, "답 하나"), broken, answering(seen, "답 셋"))
    path = f"/api/console/wf-conversations/{cid}/messages"

    ok = await logged_in.post(path, json={"text": "하나", "response_mode": "blocking"})
    failed = await logged_in.post(path, json={"text": "둘", "response_mode": "blocking"})

    assert ok.json()["run"]["status"] == "succeeded"
    assert failed.status_code == 200
    assert failed.json()["run"]["status"] == "failed" and failed.json()["answer"] == ""
    variables = (await logged_in.get(f"/api/console/wf-conversations/{cid}/variables")).json()
    assert {item["name"]: item["value"] for item in variables["items"]}["turns"] == 1
    third = await logged_in.post(path, json={"text": "셋", "response_mode": "blocking"})
    assert third.json()["run"]["status"] == "succeeded"
    # 실패한 턴은 기억하지 않는다. 창(1턴)에는 성공한 앞 턴이 들어간다.
    assert conversation(seen[1])[:2] == [("user", "하나"), ("assistant", "답 하나")]
    messages = (await logged_in.get(f"/api/console/wf-conversations/{cid}/messages")).json()
    assert [m["status"] for m in messages["items"] if m["role"] == "assistant"] == [
        "succeeded",
        "failed",
        "succeeded",
    ]


async def test_answers_stream_with_the_llm_unless_a_branch_comes_first(
    logged_in: AsyncClient, script: Any, sse: Events
) -> None:
    direct = await chatflow(
        logged_in,
        {
            "nodes": [
                node("start", "start", {"fields": []}),
                node("llm_1", "llm", llm("{{ sys.query }}"), 300),
                node("answer_1", "answer", {"text": "답: {{ llm_1.text }}"}, 600),
            ],
            "edges": edges("start>llm_1", "llm_1>answer_1"),
        },
        name="바로 흐름",
    )
    branched = await chatflow(
        logged_in,
        {
            "nodes": [
                node("start", "start", {"fields": []}),
                node("llm_1", "llm", llm("{{ sys.query }}"), 300),
                node(
                    "if_1",
                    "if_else",
                    {
                        "cases": [
                            {
                                "id": "yes",
                                "conditions": [{"var": "llm_1.text", "op": "is_not_empty"}],
                            }
                        ]
                    },
                    500,
                ),
                node("answer_1", "answer", {"text": "답: {{ llm_1.text }}"}, 700),
                node("answer_2", "answer", {"text": "빈 답"}, 700),
            ],
            "edges": edges(
                "start>llm_1", "llm_1>if_1", "if_1:case_yes>answer_1", "if_1:else>answer_2"
            ),
        },
        name="분기 흐름",
    )
    for workflow, expected in ((direct, ["llm_1"]), (branched, [])):
        graph = (await logged_in.get(f"/api/console/workflows/{workflow['id']}/draft")).json()
        answer = next(n for n in graph["graph"]["nodes"] if n["id"] == "answer_1")
        assert answer["ui"]["prestream_sources"] == expected
    text = "조각으로 나뉘어 도착하는 긴 답"
    script.add(
        [*fake_text(text, chunk_size=3), fake_end()], [*fake_text(text, chunk_size=3), fake_end()]
    )

    direct_chat = await preview(logged_in, direct["id"])
    branched_chat = await preview(logged_in, branched["id"])
    flowing = sse(
        (
            await logged_in.post(
                f"/api/console/wf-conversations/{direct_chat}/messages", json={"text": "질문"}
            )
        ).text
    )
    held = sse(
        (
            await logged_in.post(
                f"/api/console/wf-conversations/{branched_chat}/messages", json={"text": "질문"}
            )
        ).text
    )

    deltas = [event["data"]["text"] for event in flowing if event["type"] == "answer.delta"]
    assert "".join(deltas) == f"답: {text}" and len(deltas) > 2
    assert position(flowing, "answer.delta") < position(flowing, "node.completed", "llm_1")
    assert [event["data"]["text"] for event in held if event["type"] == "answer.delta"] == [
        f"답: {text}"
    ]
    assert position(held, "answer.delta") > position(held, "node.completed", "if_1")


async def test_waiting_turn_blocks_new_messages_then_resumes_into_the_answer(
    logged_in: AsyncClient, script: Any, sse: Events
) -> None:
    agent_id = await published_agent(logged_in)
    workflow = await chatflow(
        logged_in,
        {
            "nodes": [
                node("start", "start", {"fields": []}),
                node("agent_1", "agent", {"agent_id": agent_id, "instruction": "{{ sys.query }}"}),
                node("answer_1", "answer", {"text": "결과: {{ agent_1.text }}"}, 600),
            ],
            "edges": edges("start>agent_1", "agent_1>answer_1"),
        },
        name="승인 대화",
    )
    cid = await preview(logged_in, workflow["id"])
    script.add(SHELL, fake_text("셸로 확인했다"), SHELL)
    path = f"/api/console/wf-conversations/{cid}/messages"

    waiting = await logged_in.post(path, json={"text": "확인해 줘"})

    events = sse(waiting.text)
    assert types(events)[-2:] == ["workflow.waiting_input", "workflow.completed"]
    run_id = waiting.headers["x-run-id"]
    blocked = await logged_in.post(path, json={"text": "또 보낸다"})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "run_in_progress"
    assert blocked.json()["error"]["details"]["status"] == "waiting_input"
    assert blocked.json()["error"]["details"]["interaction"] == "approval"
    detail = (await logged_in.get(f"/api/console/wf-conversations/{cid}")).json()
    assert detail["active_run"]["id"] == run_id
    assert detail["active_run"]["pending_interaction"]["interaction"]["kind"] == "approval"

    approved = await logged_in.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"decision": "approve", "response_mode": "blocking"},
    )

    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "succeeded"
    messages = (await logged_in.get(f"/api/console/wf-conversations/{cid}/messages")).json()
    assert [(m["role"], m["text"], m["status"]) for m in messages["items"]] == [
        ("user", "확인해 줘", None),
        ("assistant", "결과: 셸로 확인했다", "succeeded"),
    ]

    second = await logged_in.post(path, json={"text": "한 번 더", "response_mode": "blocking"})
    assert second.json()["run"]["status"] == "waiting_input"
    stopped = await logged_in.post(
        f"/api/console/wf-runs/{second.json()['run']['id']}/stop", headers=FETCH
    )
    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
    messages = (await logged_in.get(f"/api/console/wf-conversations/{cid}/messages")).json()
    assert messages["items"][-1]["status"] == "cancelled"


async def test_preview_conversations_list_rename_delete_and_files(
    logged_in: AsyncClient, app: FastAPI, script: Any
) -> None:
    workflow = await chatflow(
        logged_in,
        {
            "nodes": [
                node("start", "start", {"fields": []}),
                node(
                    "answer_1",
                    "answer",
                    {"text": "받은 파일: {{ sys.files[0].filename }} / {{ sys.query }}"},
                    300,
                ),
            ],
            "edges": edges("start>answer_1"),
        },
        name="파일 대화",
    )
    plain = (await logged_in.post("/api/console/workflows", json={"name": "그냥 흐름"})).json()
    refused = await logged_in.post(
        f"/api/console/workflows/{plain['id']}/preview/conversations", json={}
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "wrong_kind"
    first, second = (
        await preview(logged_in, workflow["id"]),
        await preview(logged_in, workflow["id"]),
    )
    upload = await logged_in.post(
        f"/api/console/workflows/{workflow['id']}/files",
        files={"file": ("메모.txt", "첨부".encode(), "text/plain")},
        headers=FETCH,
    )
    file_id = upload.json()["id"]

    sent = await logged_in.post(
        f"/api/console/wf-conversations/{first}/messages",
        json={"text": "읽어 줘", "files": [file_id], "response_mode": "blocking"},
    )

    assert sent.json()["answer"] == "받은 파일: 메모.txt / 읽어 줘"
    messages = (await logged_in.get(f"/api/console/wf-conversations/{first}/messages")).json()
    assert [item["filename"] for item in messages["items"][0]["files"]] == ["메모.txt"]
    # 한 대화에 붙은 업로드는 다른 대화에 쓸 수 없다.
    other = await logged_in.post(
        f"/api/console/wf-conversations/{second}/messages",
        json={"text": "나도", "files": [file_id], "response_mode": "blocking"},
    )
    assert other.status_code == 422 and other.json()["error"]["code"] == "file_not_found"
    listed = (
        await logged_in.get(f"/api/console/workflows/{workflow['id']}/preview/conversations")
    ).json()
    assert [item["id"] for item in listed["items"]] == [first, second]
    renamed = await logged_in.patch(
        f"/api/console/wf-conversations/{second}", json={"title": "두 번째"}
    )
    assert renamed.json()["title"] == "두 번째"
    by_source = (
        await logged_in.get("/api/console/wf-conversations", params={"source": "preview"})
    ).json()
    assert {item["id"] for item in by_source["items"]} == {first, second}

    deleted = await logged_in.delete(f"/api/console/wf-conversations/{first}", headers=FETCH)

    assert deleted.status_code == 204
    assert (await logged_in.get(f"/api/console/wf-conversations/{first}")).status_code == 404
    # 메시지에 붙인 업로드는 대화와 함께 지운다. 런 기록은 남는다.
    async with app.state.db.read() as session:
        workspace = await WorkspaceRepository(session).get_default()
        assert workspace is not None
        assert await session.get(WfFile, uuid.UUID(file_id)) is None
    assert await app.state.storage.size(wf_file_key(workspace.id, uuid.UUID(file_id))) is None
    runs = (await logged_in.get(f"/api/console/workflows/{workflow['id']}/runs")).json()
    assert len(runs["items"]) == 1


async def test_attachments_download_through_their_conversation_or_run(
    logged_in: AsyncClient, script: Any
) -> None:
    workflow = await chatflow(
        logged_in,
        {
            "nodes": [
                node("start", "start", {"fields": []}),
                node("answer_1", "answer", {"text": "{{ sys.files[0].filename }}"}, 300),
            ],
            "edges": edges("start>answer_1"),
        },
        name="첨부 받기",
    )
    await logged_in.post(f"/api/console/workflows/{workflow['id']}/publish", json={})
    await logged_in.patch(f"/api/console/workflows/{workflow['id']}", json={"webapp_enabled": True})
    first, second = (
        await preview(logged_in, workflow["id"]),
        await preview(logged_in, workflow["id"]),
    )

    async def upload(path: str, name: str) -> str:
        response = await logged_in.post(
            path, files={"file": (name, f"{name} 내용".encode(), "text/plain")}, headers=FETCH
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    attached = await upload(f"/api/console/workflows/{workflow['id']}/files", "메모.txt")
    loose = await upload(f"/api/console/workflows/{workflow['id']}/files", "따로.txt")
    sent = await logged_in.post(
        f"/api/console/wf-conversations/{first}/messages",
        json={"text": "읽어 줘", "files": [attached], "response_mode": "blocking"},
    )
    run_id = sent.json()["run"]["id"]

    for path in (
        f"/api/console/wf-conversations/{first}/files/{attached}/download",
        f"/api/console/wf-runs/{run_id}/files/{attached}/download",
    ):
        response = await logged_in.get(path)
        assert response.status_code == 200, path
        assert response.content.decode("utf-8") == "메모.txt 내용"
    # 다른 대화, 대화에 붙지 않은 업로드, 웹앱 경로의 미리보기 대화는 받지 못한다.
    for path in (
        f"/api/console/wf-conversations/{second}/files/{attached}/download",
        f"/api/console/wf-conversations/{first}/files/{loose}/download",
        f"/api/console/wf-runs/{run_id}/files/{loose}/download",
        f"/api/web/wf-conversations/{first}/files/{attached}/download",
    ):
        assert (await logged_in.get(path)).status_code == 404, path

    created = await logged_in.post(f"/api/web/flows/{workflow['slug']}/conversations", json={})
    web_cid = created.json()["id"]
    web_file = await upload(f"/api/web/flows/{workflow['slug']}/files", "웹.txt")
    await logged_in.post(
        f"/api/web/wf-conversations/{web_cid}/messages",
        json={"text": "웹", "files": [web_file], "response_mode": "blocking"},
    )
    web = await logged_in.get(f"/api/web/wf-conversations/{web_cid}/files/{web_file}/download")
    assert web.status_code == 200 and web.content.decode("utf-8") == "웹.txt 내용"
    foreign = await logged_in.get(f"/api/web/wf-conversations/{web_cid}/files/{attached}/download")
    assert foreign.status_code == 404
    # 공개를 끄면 웹앱 대화의 첨부에도 닿지 않는다.
    await logged_in.patch(
        f"/api/console/workflows/{workflow['id']}", json={"webapp_enabled": False}
    )
    hidden = await logged_in.get(f"/api/web/wf-conversations/{web_cid}/files/{web_file}/download")
    assert hidden.status_code == 404


async def test_webapp_chat_runs_the_published_version_inside_its_scope(
    logged_in: AsyncClient, script: Any, sse: Events
) -> None:
    graph = {
        "nodes": [
            node("start", "start", {"fields": []}),
            node("answer_1", "answer", {"text": "발행본: {{ sys.query }}"}, 300),
        ],
        "edges": edges("start>answer_1"),
    }
    workflow = await chatflow(logged_in, graph, name="웹 상담")
    published = await logged_in.post(f"/api/console/workflows/{workflow['id']}/publish", json={})
    assert published.status_code == 201, published.text
    hidden = await logged_in.post(f"/api/web/flows/{workflow['slug']}/conversations", json={})
    assert hidden.status_code == 404
    await logged_in.patch(f"/api/console/workflows/{workflow['id']}", json={"webapp_enabled": True})
    graph["nodes"][1]["config"]["text"] = "초안: {{ sys.query }}"
    current = (await logged_in.get(f"/api/console/workflows/{workflow['id']}/draft")).json()
    await logged_in.put(
        f"/api/console/workflows/{workflow['id']}/draft",
        json={
            "graph": graph,
            "env_vars": [],
            "conversation_vars": [],
            "settings": current["settings"],
            "updated_at": current["updated_at"],
        },
    )

    created = await logged_in.post(f"/api/web/flows/{workflow['slug']}/conversations", json={})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    assert created.json()["source"] == "webapp"
    response = await logged_in.post(
        f"/api/web/wf-conversations/{cid}/messages", json={"text": "안녕"}
    )

    events = sse(response.text)
    assert events[-1]["data"]["status"] == "succeeded"
    assert events[position(events, "answer.completed")]["data"]["text"] == "발행본: 안녕"
    run_id = response.headers["x-run-id"]
    assert (await logged_in.get(f"/api/web/wf-runs/{run_id}")).json()["trigger"] == "webapp"
    messages = (await logged_in.get(f"/api/web/wf-conversations/{cid}/messages")).json()
    assert [m["text"] for m in messages["items"]] == ["안녕", "발행본: 안녕"]
    listed = (await logged_in.get(f"/api/web/flows/{workflow['slug']}/conversations")).json()
    assert [item["id"] for item in listed["items"]] == [cid]
    renamed = await logged_in.patch(f"/api/web/wf-conversations/{cid}", json={"title": "인사"})
    assert renamed.json()["title"] == "인사"
    # 미리보기 대화는 웹앱 경로로 보이지 않는다.
    private = await preview(logged_in, workflow["id"])
    for path in (
        f"/api/web/wf-conversations/{private}",
        f"/api/web/wf-conversations/{private}/messages",
    ):
        assert (await logged_in.get(path)).status_code == 404
    refused = await logged_in.post(
        f"/api/web/wf-conversations/{private}/messages", json={"text": "x"}
    )
    assert refused.status_code == 404
    # 공개를 끄면 이미 만든 웹앱 대화에도 닿지 않는다.
    await logged_in.patch(
        f"/api/console/workflows/{workflow['id']}", json={"webapp_enabled": False}
    )
    assert (await logged_in.get(f"/api/web/wf-conversations/{cid}")).status_code == 404
    await logged_in.patch(f"/api/console/workflows/{workflow['id']}", json={"webapp_enabled": True})
    deleted = await logged_in.delete(f"/api/web/wf-conversations/{cid}", headers=FETCH)
    assert deleted.status_code == 204
    assert (await logged_in.get(f"/api/web/wf-conversations/{cid}")).status_code == 404
