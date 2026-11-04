"""런의 키 등급 상속과 위쪽 전파(3단계 설계 14.4, M24).

런의 등급은 그 런이 닿을 수 있는 지식 베이스 전부로 정한다. 유료 KB를 읽는 워크플로우, agent 노드가
부르는 에이전트(와 그 하위 에이전트)가 유료 KB를 쓰는 워크플로우, 하위 에이전트가 유료 KB를 쓰는
지휘자 에이전트는 런 전체가 paid라 모든 모델 호출이 ChatRequest.tier=paid로 어댑터에 닿는다(가짜
어댑터가 tier를 기록한다). 유료 키를 나중에 지우면 런 시작·발행이 paid_key_missing으로 막히고, 멈춘
런을 재개할 때는 그사이 유료가 된 KB를 따라 등급을 올린다.
"""

import hashlib
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.config import Settings
from app.db.models import KbDocument, KbSegment, KnowledgeBase, Run
from app.db.repositories.knowledge import KbCounterRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.knowledge.units import FtsRow, fts_text
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ModelEvent
from app.services.knowledge_runtime import KnowledgeRuntime

DRAFT_FIELDS = (
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


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings.model_copy(
        update={
            "llm_fake": True,
            "kb_embed_rpm": 6000,
            "gemini_paid_api_key": SecretStr("paid-" + "7" * 30),
        }
    )


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


def chain(*ids: str) -> list[dict[str, Any]]:
    return [
        {"id": f"e{index}", "source": source, "target": target}
        for index, (source, target) in enumerate(zip(ids, ids[1:], strict=False), start=1)
    ]


async def make_kb(
    app: FastAPI, client: AsyncClient, *, tier: str = "paid", slug: str = "payroll"
) -> dict[str, Any]:
    created = await client.post(
        "/api/console/knowledge",
        json={
            "name": "급여 규정",
            "slug": slug,
            "description": "급여 지급 규정",
            # 전문 검색만 하는 KB라 임베딩 없이 찾는다. 등급은 key_tier만 본다.
            "indexing_technique": "economy",
            "data_policy": {"acknowledged": True, "key_tier": tier},
        },
    )
    assert created.status_code == 201, created.text
    kb = created.json()
    content = "급여는 매월 25일에 지급한다."
    runtime: KnowledgeRuntime = app.state.knowledge
    kb_id = uuid.UUID(kb["id"])
    async with app.state.db.write() as session:
        workspace = await WorkspaceRepository(session).get_default()
        assert workspace is not None
        workspace_id = workspace.id
        document = KbDocument(
            workspace_id=workspace_id,
            kb_id=kb_id,
            position=1,
            name="급여 지급",
            data_source_type="paste_text",
            filename="급여 지급.txt",
            mime="text/plain",
            size_bytes=len(content.encode()),
            sha256=hashlib.sha256(content.encode()).hexdigest(),
            storage_key=f"seed/{uuid.uuid4().hex}",
            batch="seed",
            indexing_status="completed",
        )
        session.add(document)
        await session.flush()
        unit_no = next(iter(await KbCounterRepository(session).allocate(1)))
        session.add(
            KbSegment(
                workspace_id=workspace_id,
                kb_id=kb_id,
                document_id=document.id,
                position=1,
                unit_no=unit_no,
                content=content,
                word_count=len(content),
                tokens=len(content),
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                status="completed",
                index_state={"fts": "ok", "vector": "skip"},
            )
        )
    await runtime.fts.ensure(kb_id, workspace_id=workspace_id)
    await runtime.fts.upsert(kb_id, [FtsRow(unit_no, fts_text(content, None))])
    return kb


async def become_paid(app: FastAPI, kb: dict[str, Any]) -> None:
    async with app.state.db.write() as session:
        row = await session.get(KnowledgeBase, uuid.UUID(kb["id"]))
        assert row is not None
        row.data_policy = {**(row.data_policy or {}), "key_tier": "paid"}


async def make_agent(
    client: AsyncClient,
    name: str,
    slug: str,
    *,
    description: str = "",
    publish: bool = True,
    **draft: Any,
) -> dict[str, Any]:
    created = await client.post(
        "/api/console/agents", json={"name": name, "slug": slug, "description": description}
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    saved = await save_agent(client, agent["id"], **draft)
    agent["warnings"] = saved["warnings"]
    if publish:
        published = await client.post(f"/api/console/agents/{agent['id']}/publish", json={})
        assert published.status_code == 201, published.text
    return agent


async def save_agent(client: AsyncClient, agent_id: str, **draft: Any) -> dict[str, Any]:
    current = (await client.get(f"/api/console/agents/{agent_id}/draft")).json()
    body = {key: current[key] for key in DRAFT_FIELDS}
    body.update({"env_vars": [], **draft})
    saved = await client.put(f"/api/console/agents/{agent_id}/draft", json=body)
    assert saved.status_code == 200, saved.text
    return saved.json()


async def save_flow(client: AsyncClient, graph: dict[str, Any]) -> str:
    created = await client.post(
        "/api/console/workflows", json={"name": "급여 안내", "kind": "workflow"}
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    current = (await client.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/console/workflows/{workflow_id}/draft",
        json={
            "graph": graph,
            "env_vars": [],
            "conversation_vars": [],
            "settings": current["settings"],
            "updated_at": current["updated_at"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert [issue for issue in saved.json()["issues"] if issue["level"] == "error"] == []
    return workflow_id


def flow(conductor_id: str, kb_id: str | None) -> dict[str, Any]:
    nodes = [node("start", "start", {"fields": [{"name": "question", "type": "string"}]})]
    llm: dict[str, Any] = {"prompt": [{"role": "user", "text": "{{ start.question }}"}]}
    if kb_id is not None:
        nodes.append(
            node(
                "knowledge_1",
                "knowledge_retrieval",
                {"kb_ids": [kb_id], "query_var": "start.question"},
                200,
            )
        )
        llm["context"] = {"var": "knowledge_1.result"}
    nodes += [
        node("llm_1", "llm", llm, 400),
        node(
            "agent_1", "agent", {"agent_id": conductor_id, "instruction": "{{ llm_1.text }}"}, 600
        ),
        node("llm_2", "llm", {"prompt": [{"role": "user", "text": "{{ agent_1.text }}"}]}, 700),
        node("end_1", "end", {"outputs": [{"name": "answer", "var": "llm_2.text"}]}, 800),
    ]
    return {"nodes": nodes, "edges": chain(*(item["id"] for item in nodes))}


def said(text: str) -> list[ModelEvent]:
    return [*fake_text(text), fake_end()]


def calling(slug: str) -> list[ModelEvent]:
    return [
        *fake_tool_call("call_agent", {"agent": slug, "task": "근거를 찾는다"}),
        fake_end("tool_use"),
    ]


def asking() -> list[ModelEvent]:
    return [*fake_tool_call("ask_user", {"question": "어느 달?"}), fake_end("tool_use")]


async def run_flow(client: AsyncClient, workflow_id: str) -> dict[str, Any]:
    response = await client.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"question": "급여일이 언제야?"}, "response_mode": "blocking"},
    )
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["status"] == "succeeded", run["error"]
    return run


async def run_tiers(app: FastAPI) -> list[str]:
    async with app.state.db.read() as session:
        runs = (await session.execute(select(Run).order_by(Run.queued_at))).scalars()
        return [str((run.limits or {}).get("tier")) for run in runs]


async def preview(client: AsyncClient, agent_id: str, text: str) -> dict[str, Any]:
    session = (
        await client.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    ).json()
    response = await client.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": text, "response_mode": "blocking"},
    )
    assert response.status_code == 200, response.text
    return {"session_id": session["id"], **response.json()}


async def test_paid_knowledge_makes_the_whole_run_tree_paid(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    kb = await make_kb(app, logged_in)
    researcher = await make_agent(logged_in, "조사", "researcher", description="근거를 찾는다")
    conductor = await make_agent(
        logged_in, "지휘자", "conductor", sub_agents=[{"agent_id": researcher["id"]}]
    )
    workflow_id = await save_flow(logged_in, flow(conductor["id"], kb["id"]))
    # llm 노드 → 지휘자(call_agent) → 조사(손자) → 지휘자 마무리 → 뒤 llm 순서로 부른다.
    script.add(
        said("25일"), calling("researcher"), said("근거: 규정"), said("25일에 준다"), said("끝")
    )

    run = await run_flow(logged_in, workflow_id)

    assert run["outputs"] == {"answer": "끝"}
    assert run["limits"]["tier"] == "paid"
    assert app.state.llm.tiers == ["paid"] * 5
    # agent 노드의 자식 런과 call_agent 손자 런 모두 limits에 paid가 남는다.
    assert await run_tiers(app) == ["paid", "paid"]


async def test_an_agent_node_with_paid_knowledge_makes_the_workflow_paid(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    kb = await make_kb(app, logged_in)
    helper = await make_agent(
        logged_in, "급여 도우미", "payroll-bot", knowledge={"kb_ids": [kb["id"]]}
    )
    workflow_id = await save_flow(logged_in, flow(helper["id"], None))
    script.add(said("요약"), said("25일"), said("끝"))

    run = await run_flow(logged_in, workflow_id)

    # 워크플로우는 KB를 직접 읽지 않지만 agent 노드의 답을 받는 뒤 llm까지 유료 키로 간다.
    assert run["limits"]["tier"] == "paid"
    assert app.state.llm.tiers == ["paid", "paid", "paid"]
    assert await run_tiers(app) == ["paid"]


async def test_a_sub_agent_with_paid_knowledge_makes_its_caller_paid(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    kb = await make_kb(app, logged_in)
    researcher = await make_agent(
        logged_in,
        "조사",
        "researcher",
        description="근거를 찾는다",
        knowledge={"kb_ids": [kb["id"]]},
    )
    conductor = await make_agent(
        logged_in,
        "지휘자",
        "conductor",
        publish=False,
        sub_agents=[{"agent_id": researcher["id"]}],
    )
    assert ("paid_key_required", researcher["id"]) in {
        (item["code"], item["agent_id"]) for item in conductor["warnings"]
    }
    script.add(calling("researcher"), said("근거: 규정"), said("25일이다"))

    body = await preview(logged_in, conductor["id"], "급여일?")

    assert body["run"]["status"] == "succeeded", body["run"]
    assert body["run"]["limits"]["tier"] == "paid"
    # 지휘자 자신은 KB가 없어도 하위 에이전트의 자료를 받으므로 첫 호출부터 유료 키다.
    assert app.state.llm.tiers == ["paid", "paid", "paid"]
    assert await run_tiers(app) == ["paid", "paid"]


async def test_publishing_paid_agents_needs_the_paid_key(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    kb = await make_kb(app, logged_in)
    researcher = await make_agent(
        logged_in, "조사", "researcher", description="근거", knowledge={"kb_ids": [kb["id"]]}
    )
    conductor = await make_agent(
        logged_in,
        "지휘자",
        "conductor",
        publish=False,
        sub_agents=[{"agent_id": researcher["id"]}],
    )
    workflow_id = await save_flow(logged_in, flow(researcher["id"], None))
    settings: Settings = app.state.settings
    app.state.settings = settings.model_copy(update={"gemini_paid_api_key": None})

    flow_refused = await logged_in.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    agent_refused = await logged_in.post(f"/api/console/agents/{conductor['id']}/publish", json={})

    assert flow_refused.status_code == 422, flow_refused.text
    assert flow_refused.json()["error"]["code"] == "paid_key_missing"
    assert agent_refused.status_code == 422, agent_refused.text
    details = agent_refused.json()["error"]["details"]
    assert [(item["type"], item["loc"][-3:]) for item in details] == [
        ("paid_key_missing", ["sub_agents", 0, "agent_id"])
    ]


async def test_runs_on_paid_knowledge_refuse_to_start_without_the_paid_key(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    kb = await make_kb(app, logged_in)
    leaf = await make_agent(
        logged_in, "급여 도우미", "payroll-bot", knowledge={"kb_ids": [kb["id"]]}
    )
    conductor = await make_agent(logged_in, "지휘자", "conductor")
    workflow_id = await save_flow(logged_in, flow(conductor["id"], kb["id"]))
    session = (
        await logged_in.post(f"/api/console/agents/{leaf['id']}/preview/sessions", json={})
    ).json()
    settings: Settings = app.state.settings
    app.state.settings = settings.model_copy(update={"gemini_paid_api_key": None})

    started = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"question": "급여일?"}, "response_mode": "blocking"},
    )
    sent = await logged_in.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": "급여일?", "response_mode": "blocking"},
    )

    for response in (started, sent):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "paid_key_missing"
    assert app.state.llm.tiers == []


async def test_a_resumed_workflow_follows_a_knowledge_base_that_became_paid(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    kb = await make_kb(app, logged_in, tier="free")
    graph = {
        "nodes": [
            node("start", "start", {"fields": [{"name": "question", "type": "string"}]}),
            node(
                "knowledge_1",
                "knowledge_retrieval",
                {"kb_ids": [kb["id"]], "query_var": "start.question"},
            ),
            node(
                "human_input_1",
                "human_input",
                {"fields": [{"name": "ok", "type": "select", "options": ["예"], "required": True}]},
            ),
            node(
                "llm_1",
                "llm",
                {
                    "prompt": [{"role": "user", "text": "{{ start.question }}"}],
                    "context": {"var": "knowledge_1.result"},
                },
            ),
            node("end_1", "end", {"outputs": [{"name": "answer", "var": "llm_1.text"}]}),
        ],
        "edges": chain("start", "knowledge_1", "human_input_1", "llm_1", "end_1"),
    }
    workflow_id = await save_flow(logged_in, graph)
    waiting = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"question": "급여일?"}, "response_mode": "blocking"},
    )
    run = waiting.json()
    assert run["status"] == "waiting_input" and run["limits"]["tier"] == "free"
    await become_paid(app, kb)
    script.add(said("25일"))

    resumed = await logged_in.post(
        f"/api/console/wf-runs/{run['id']}/input",
        json={"kind": "form", "values": {"ok": "예"}, "response_mode": "blocking"},
    )

    assert resumed.status_code == 200, resumed.text
    detail = (await logged_in.get(f"/api/console/wf-runs/{run['id']}")).json()
    assert detail["status"] == "succeeded", detail["error"]
    # 재개할 때 지금 등급으로 올린다(내리지는 않는다).
    assert detail["limits"]["tier"] == "paid"
    assert app.state.llm.tiers == ["paid"]


async def test_a_resumed_agent_run_follows_a_knowledge_base_that_became_paid(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    kb = await make_kb(app, logged_in, tier="free")
    agent = await make_agent(
        logged_in,
        "급여 도우미",
        "payroll-bot",
        publish=False,
        tools=["ask_user"],
        knowledge={"kb_ids": [kb["id"]]},
    )
    script.add(asking(), said("11월 25일이다"))
    first = await preview(logged_in, agent["id"], "급여일?")
    assert first["run"]["status"] == "waiting_input"
    assert first["run"]["limits"]["tier"] == "free"
    await become_paid(app, kb)

    answered = await logged_in.post(
        f"/api/console/sessions/{first['session_id']}/messages",
        json={"text": "11월", "response_mode": "blocking"},
    )

    assert answered.status_code == 200, answered.text
    assert answered.json()["run"]["status"] == "succeeded", answered.json()["run"]
    assert answered.json()["run"]["limits"]["tier"] == "paid"
    assert app.state.llm.tiers == ["free", "paid"]
