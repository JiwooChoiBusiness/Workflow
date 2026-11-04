"""지식 노드와 인용의 백엔드 흐름(3단계 설계 8.1~8.3, 11장, 12장, K5·K10, M24).

start → knowledge_retrieval → llm(context) → answer 챗플로우가 가짜 임베더·가짜 어댑터로 돌고 인용이
assistant 메시지·런·이벤트·서비스 API 응답에 붙는다. knowledge_index는 지식 레인으로 문서를 만들고
wait면 끝날 때까지 기다린다. DSL은 kb_slug로 다시 잇고 지운 KB는 knowledge_base_not_found다.

문서는 지식 레인을 거치지 않고 app.db 행과 index.db 행을 직접 넣는다(test_knowledge_retrieve.py와
같은 방식).
"""

import base64
import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.config import Settings
from app.db.models import KbDocument, KbQueryLog, KbSegment, WfRun
from app.db.repositories.knowledge import KbCounterRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.knowledge.units import FtsRow, VecRow, doc_id_of, fts_text
from app.llm.embeddings import EmbeddingConfig
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ModelEvent
from app.services.knowledge_runtime import KnowledgeRuntime
from app.workflow.nodes import default_registry
from app.workflow.nodes.llm import CONTEXT_PREAMBLE

FETCH = {"X-Requested-With": "fetch"}
Events = Callable[[str], list[dict[str, Any]]]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    # 가짜 어댑터에는 분당 상한이 없다. 시험이 리미터의 20초 조각을 기다리지 않게 넉넉히 준다.
    # 비밀 환경변수를 쓰는 시험이 있어 암호화 키도 둔다.
    return short_settings.model_copy(
        update={
            "llm_fake": True,
            "kb_embed_rpm": 6000,
            "encryption_key": SecretStr(base64.b64encode(bytes(range(32))).decode()),
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


async def make_kb(client: AsyncClient, name: str, **body: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "data_policy": {"acknowledged": True}}
    payload.update(body)
    response = await client.post("/api/console/knowledge", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def seed(app: FastAPI, kb: dict[str, Any], name: str, chunks: Sequence[str]) -> uuid.UUID:
    """완료된 문서 하나와 청크(전문·벡터 색인 행 포함)를 넣는다."""
    runtime: KnowledgeRuntime = app.state.knowledge
    kb_id = uuid.UUID(kb["id"])
    units: list[tuple[int, str]] = []
    async with app.state.db.write() as session:
        workspace = await WorkspaceRepository(session).get_default()
        assert workspace is not None
        workspace_id = workspace.id
        document = KbDocument(
            workspace_id=workspace_id,
            kb_id=kb_id,
            position=1,
            name=name,
            data_source_type="paste_text",
            filename=f"{name}.txt",
            mime="text/plain",
            size_bytes=1,
            sha256=hashlib.sha256(name.encode()).hexdigest(),
            storage_key=f"seed/{uuid.uuid4().hex}",
            batch="seed",
            indexing_status="completed",
        )
        session.add(document)
        await session.flush()
        counter = KbCounterRepository(session)
        for position, content in enumerate(chunks, start=1):
            unit_no = next(iter(await counter.allocate(1)))
            session.add(
                KbSegment(
                    workspace_id=workspace_id,
                    kb_id=kb_id,
                    document_id=document.id,
                    position=position,
                    unit_no=unit_no,
                    content=content,
                    word_count=len(content),
                    tokens=len(content),
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    status="completed",
                    index_state={"fts": "ok", "vector": "ok"},
                )
            )
            units.append((unit_no, content))
        document_id = document.id
    await runtime.fts.ensure(kb_id, workspace_id=workspace_id)
    await runtime.fts.upsert(kb_id, [FtsRow(unit, fts_text(text, None)) for unit, text in units])
    config = EmbeddingConfig.model_validate(kb["embedding"])
    await runtime.vectors.ensure(kb_id, config, workspace_id=workspace_id)
    embedded = await runtime.embeddings.embed_documents(
        [text for _, text in units], config, priority="interactive"
    )
    await runtime.vectors.upsert(
        kb_id,
        [
            VecRow(unit, vector, doc_id_of(document_id), "segment")
            for (unit, _), vector in zip(units, embedded.vectors, strict=True)
            if vector is not None
        ],
    )
    return document_id


def rag_graph(kb_id: str) -> dict[str, Any]:
    return {
        "nodes": [
            node("start", "start", {"fields": []}),
            node("knowledge_1", "knowledge_retrieval", {"kb_ids": [kb_id]}, 200),
            node(
                "llm_1",
                "llm",
                {
                    "prompt": [
                        {"role": "system", "text": "자료로만 답한다."},
                        {"role": "user", "text": "{{ sys.query }}"},
                    ],
                    "context": {"var": "knowledge_1.result"},
                },
                400,
            ),
            node("answer_1", "answer", {"text": "{{ llm_1.text }}"}, 600),
        ],
        "edges": chain("start", "knowledge_1", "llm_1", "answer_1"),
    }


async def save_flow(
    client: AsyncClient,
    graph: dict[str, Any],
    *,
    kind: str = "chatflow",
    name: str = "규정 챗",
    env_vars: Sequence[dict[str, Any]] = (),
) -> str:
    created = await client.post("/api/console/workflows", json={"name": name, "kind": kind})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    current = (await client.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/console/workflows/{workflow_id}/draft",
        json={
            "graph": graph,
            "env_vars": list(env_vars),
            "conversation_vars": [],
            "settings": current["settings"],
            "updated_at": current["updated_at"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert [issue for issue in saved.json()["issues"] if issue["level"] == "error"] == []
    return workflow_id


def answering(seen: list[ChatRequest], text: str) -> Callable[[ChatRequest], list[ModelEvent]]:
    def turn(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return [*fake_text(text), fake_end()]

    return turn


async def test_chatflow_cites_the_segments_it_answered_from(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    kb = await make_kb(logged_in, "환불 규정")
    await seed(
        app, kb, "환불 규정", ["환불은 구매 후 7일 안에 신청한다.", "교환은 30일 안에 한다."]
    )
    workflow_id = await save_flow(logged_in, rag_graph(kb["id"]))
    seen: list[ChatRequest] = []
    script.add(answering(seen, "구매 후 7일 안에 환불을 신청한다 [1]."))
    conversation = (
        await logged_in.post(f"/api/console/workflows/{workflow_id}/preview/conversations", json={})
    ).json()

    sent = await logged_in.post(
        f"/api/console/wf-conversations/{conversation['id']}/messages",
        json={"text": "환불 기한이 언제야?", "response_mode": "blocking"},
    )

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["run"]["status"] == "succeeded", body["run"]
    cited = body["retriever_resources"]
    assert cited, body
    assert cited[0]["position"] == 1 and cited[0]["document_name"] == "환불 규정"
    assert cited[0]["dataset_id"] == kb["id"] and cited[0]["dataset_name"] == "환불 규정"
    system = seen[0].system
    assert CONTEXT_PREAMBLE in system
    assert '<document index="1" name="환불 규정" source="환불 규정">' in system
    assert body["run"]["limits"]["tier"] == "free"
    messages = (
        await logged_in.get(f"/api/console/wf-conversations/{conversation['id']}/messages")
    ).json()["items"]
    assistant = messages[-1]
    assert assistant["role"] == "assistant" and assistant["retriever_resources"] == cited
    assert messages[0]["retriever_resources"] == []
    detail = (await logged_in.get(f"/api/console/wf-runs/{body['run']['id']}")).json()
    assert detail["retriever_resources"] == cited
    events = (await logged_in.get(f"/api/console/wf-runs/{body['run']['id']}/event-log")).json()
    [completed] = [item for item in events["items"] if item["type"] == "workflow.completed"]
    assert [item["segment_id"] for item in completed["data"]["retriever_resources"]] == [
        item["segment_id"] for item in cited
    ]
    async with app.state.db.read() as session:
        logs = list((await session.execute(select(KbQueryLog))).scalars())
        hits = {
            row.content: row.hit_count
            for row in (await session.execute(select(KbSegment))).scalars()
        }
    assert [row.source for row in logs] == ["node"]
    # 노드 검색은 hit_count를 올린다(검색 시험·평가는 올리지 않는다, M23).
    assert hits["환불은 구매 후 7일 안에 신청한다."] == 1


async def test_automatic_filter_conditions_are_logged(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    kb = await make_kb(logged_in, "출장 규정")
    await seed(app, kb, "출장 규정", ["출장비 정산은 14일 안에 한다."])
    field = await logged_in.post(
        f"/api/console/knowledge/{kb['id']}/metadata", json={"name": "team", "type": "string"}
    )
    assert field.status_code == 201, field.text
    graph = rag_graph(kb["id"])
    graph["nodes"][1]["config"]["metadata_filter"] = {"mode": "automatic"}
    workflow_id = await save_flow(logged_in, graph)
    condition = {"name": "team", "comparison_operator": "is", "value": "dev"}
    script.add(
        fake_tool_call("build_metadata_filter", {"conditions": [condition]}),
        answering([], "찾은 자료가 없다."),
    )
    conversation = (
        await logged_in.post(f"/api/console/workflows/{workflow_id}/preview/conversations", json={})
    ).json()

    sent = await logged_in.post(
        f"/api/console/wf-conversations/{conversation['id']}/messages",
        json={"text": "개발팀 출장비 정산 기한은?", "response_mode": "blocking"},
    )

    body = sent.json()
    assert body["run"]["status"] == "succeeded", body["run"]
    # team 값을 가진 문서가 없어 조건에 걸러진다.
    assert body["retriever_resources"] == []
    async with app.state.db.read() as session:
        [row] = list((await session.execute(select(KbQueryLog))).scalars())
    # 모델이 만든 조건은 검색 기록에 남아 왜 그 결과가 나왔는지 나중에 볼 수 있다(M23 검토).
    assert row.settings["auto_filter"] == {
        "logical_operator": "and",
        "conditions": [condition],
        "applied": True,
    }
    assert row.settings["metadata_filter"]["mode"] == "automatic"


async def test_service_api_returns_citations_in_the_dify_places(
    app: FastAPI, logged_in: AsyncClient, make_client: Callable[[], AsyncClient], script: Any
) -> None:
    kb = await make_kb(logged_in, "배송")
    await seed(app, kb, "배송 안내", ["배송은 결제 후 2일 걸린다."])
    workflow_id = await save_flow(logged_in, rag_graph(kb["id"]), name="배송 챗")
    published = await logged_in.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text
    created = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/api-keys", json={"name": "자동화"}
    )
    headers = {"Authorization": f"Bearer {created.json()['key']}"}
    api = make_client()
    script.add(answering([], "2일 걸린다 [1]."))

    sent = await api.post(
        "/api/v1/chat-messages",
        json={"query": "배송은 얼마나 걸려?", "user": "u1", "response_mode": "blocking"},
        headers=headers,
    )

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["status"] == "succeeded"
    [cited] = body["metadata"]["retriever_resources"]
    assert cited["document_name"] == "배송 안내" and cited["position"] == 1
    listed = await api.get(
        "/api/v1/messages",
        params={"conversation_id": body["conversation_id"], "user": "u1"},
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][-1]["retriever_resources"] == [cited]
    run = await api.get(
        f"/api/v1/workflows/run/{body['run_id']}", params={"user": "u1"}, headers=headers
    )
    assert run.status_code == 200, run.text
    assert run.json()["retriever_resources"] == [cited]


async def test_workflow_service_api_returns_the_run_citations(
    app: FastAPI, logged_in: AsyncClient, make_client: Callable[[], AsyncClient], script: Any
) -> None:
    kb = await make_kb(logged_in, "출장")
    await seed(app, kb, "출장 규정", ["출장비는 14일 안에 정산한다."])
    graph = {
        "nodes": [
            node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            node(
                "knowledge_1", "knowledge_retrieval", {"kb_ids": [kb["id"]], "query_var": "start.q"}
            ),
            node(
                "llm_1",
                "llm",
                {
                    "prompt": [{"role": "user", "text": "{{ start.q }}"}],
                    "context": {"var": "knowledge_1.result"},
                },
            ),
            node("end_1", "end", {"outputs": [{"name": "answer", "var": "llm_1.text"}]}),
        ],
        "edges": chain("start", "knowledge_1", "llm_1", "end_1"),
    }
    workflow_id = await save_flow(logged_in, graph, kind="workflow", name="출장 답변")
    published = await logged_in.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text
    created = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/api-keys", json={"name": "자동화"}
    )
    headers = {"Authorization": f"Bearer {created.json()['key']}"}
    script.add(answering([], "14일 안이다 [1]."))

    sent = await make_client().post(
        "/api/v1/workflows/run",
        json={"inputs": {"q": "출장비 정산 기한"}, "user": "u1", "response_mode": "blocking"},
        headers=headers,
    )

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["status"] == "succeeded", body
    [cited] = body["retriever_resources"]
    assert cited["document_name"] == "출장 규정" and cited["position"] == 1


async def test_secret_env_values_are_masked_in_stored_citations(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    leaked = "tok-7f3a9c1e5b"
    kb = await make_kb(logged_in, "연동")
    await seed(app, kb, "연동 안내", [f"연동 키는 {leaked}이다. 외부에 알리지 않는다."])
    graph = {
        "nodes": [
            node("start", "start", {"fields": []}),
            node("knowledge_1", "knowledge_retrieval", {"kb_ids": [kb["id"]]}),
            node(
                "llm_1",
                "llm",
                {
                    "prompt": [{"role": "user", "text": "{{ sys.query }}"}],
                    "context": {"var": "knowledge_1.result"},
                },
            ),
            node(
                "human_input_1",
                "human_input",
                {"fields": [{"name": "ok", "type": "select", "options": ["예"], "required": True}]},
            ),
            node("answer_1", "answer", {"text": "{{ llm_1.text }}"}),
        ],
        "edges": chain("start", "knowledge_1", "llm_1", "human_input_1", "answer_1"),
    }
    workflow_id = await save_flow(
        logged_in,
        graph,
        name="연동 챗",
        env_vars=[{"name": "API_TOKEN", "value": leaked, "secret": True}],
    )
    script.add(answering([], "키는 알려 줄 수 없다 [1]."))
    conversation = (
        await logged_in.post(f"/api/console/workflows/{workflow_id}/preview/conversations", json={})
    ).json()

    waiting = await logged_in.post(
        f"/api/console/wf-conversations/{conversation['id']}/messages",
        json={"text": "연동 키가 뭐야?", "response_mode": "blocking"},
    )

    assert waiting.status_code == 200, waiting.text
    run_id = waiting.json()["run"]["id"]
    assert waiting.json()["run"]["status"] == "waiting_input"
    async with app.state.db.read() as session:
        state = (await session.get(WfRun, uuid.UUID(run_id))).state  # pyright: ignore[reportOptionalMemberAccess]
    checkpoint_text = state["retriever_resources"][0]["content"]
    assert leaked not in checkpoint_text and "***" in checkpoint_text
    finished = await logged_in.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"kind": "form", "values": {"ok": "예"}, "response_mode": "blocking"},
    )
    assert finished.status_code == 200, finished.text
    detail = (await logged_in.get(f"/api/console/wf-runs/{run_id}")).json()
    assert detail["status"] == "succeeded", detail["error"]
    messages = (
        await logged_in.get(f"/api/console/wf-conversations/{conversation['id']}/messages")
    ).json()["items"]
    for stored in (detail["retriever_resources"], messages[-1]["retriever_resources"]):
        [cited] = stored
        assert leaked not in cited["content"] and "***" in cited["content"]


async def test_knowledge_index_creates_a_document_and_waits_for_it(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = default_registry().get("knowledge_index")
    monkeypatch.setattr(runner, "poll_sec", 0.05)
    kb = await make_kb(logged_in, "회의록")
    graph = {
        "nodes": [
            node("start", "start", {"fields": [{"name": "body", "type": "paragraph"}]}),
            node(
                "index_1",
                "knowledge_index",
                {
                    "kb_id": kb["id"],
                    "source": {"text_var": "start.body"},
                    "name": "11월 회의",
                    "wait": True,
                    "timeout_sec": 60,
                },
                200,
            ),
            node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "ids", "var": "index_1.document_ids"},
                        {"name": "status", "var": "index_1.status"},
                    ]
                },
                400,
            ),
        ],
        "edges": chain("start", "index_1", "end_1"),
    }
    workflow_id = await save_flow(logged_in, graph, kind="workflow", name="회의록 저장")

    first = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"body": "결정: 배포는 다음 주 화요일이다."}, "response_mode": "blocking"},
    )

    assert first.status_code == 200, first.text
    run = first.json()
    assert run["status"] == "succeeded", run["error"]
    assert run["outputs"]["status"] == "completed"
    [document_id] = run["outputs"]["ids"]
    documents = (await logged_in.get(f"/api/console/knowledge/{kb['id']}/documents")).json()
    [document] = documents["items"]
    assert document["id"] == document_id and document["name"] == "11월 회의"
    assert document["data_source_type"] == "workflow"
    assert document["indexing_status"] == "completed"

    # 같은 이름이면 기본(replace)으로 옛 문서를 지우고 새로 넣는다.
    second = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {"body": "결정: 배포는 수요일로 옮겼다."}, "response_mode": "blocking"},
    )
    assert second.json()["status"] == "succeeded", second.json()["error"]
    after = (await logged_in.get(f"/api/console/knowledge/{kb['id']}/documents")).json()["items"]
    assert [item["id"] for item in after] == second.json()["outputs"]["ids"]


async def test_settings_keys_are_masked_in_workflows_even_with_output_masking_off(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    # 워크플로우도 에이전트 런과 같은 규칙으로 .env의 키를 가린다. '출력에서 비밀 값 가리기'를 꺼도
    # 설정의 키는 끝 출력과 노드 런, 이벤트에서 가린다(꺼서 드러나는 것은 비밀 env뿐이다).
    key = "AIza" + "0" * 31 + "k000"
    assert key in app.state.settings.secret_values()
    kb = await make_kb(logged_in, "키 메모")
    await seed(app, kb, "키 메모", [f"서버 키는 {key} 이다. 외부에 알리지 않는다."])
    graph = {
        "nodes": [
            node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            node(
                "knowledge_1", "knowledge_retrieval", {"kb_ids": [kb["id"]], "query_var": "start.q"}
            ),
            node("end_1", "end", {"outputs": [{"name": "text", "var": "knowledge_1.result_text"}]}),
        ],
        "edges": chain("start", "knowledge_1", "end_1"),
    }
    workflow_id = await save_flow(logged_in, graph, kind="workflow", name="키 찾기")
    base = f"/api/console/workflows/{workflow_id}"
    current = (await logged_in.get(f"{base}/draft")).json()
    saved = await logged_in.put(
        f"{base}/draft",
        json={
            "graph": graph,
            "env_vars": [],
            "conversation_vars": [],
            "settings": {**current["settings"], "mask_secrets_in_outputs": False},
            "updated_at": current["updated_at"],
        },
    )
    assert saved.status_code == 200, saved.text

    done = await logged_in.post(
        f"{base}/draft/runs", json={"inputs": {"q": "서버 키"}, "response_mode": "blocking"}
    )

    assert done.status_code == 200, done.text
    run = done.json()
    assert run["status"] == "succeeded", run["error"]
    assert "***" in run["outputs"]["text"] and key not in run["outputs"]["text"]
    detail = (await logged_in.get(f"/api/console/wf-runs/{run['id']}")).json()
    retrieval = next(item for item in detail["node_runs"] if item["node_id"] == "knowledge_1")
    node_run = (
        await logged_in.get(f"/api/console/wf-runs/{run['id']}/node-runs/{retrieval['id']}")
    ).json()
    log = (await logged_in.get(f"/api/console/wf-runs/{run['id']}/event-log")).json()
    for label, payload in (("node run", node_run), ("events", log), ("run", detail)):
        assert key not in json.dumps(payload, ensure_ascii=False), label


async def test_dsl_relinks_by_slug_and_flags_a_deleted_knowledge_base(
    app: FastAPI, logged_in: AsyncClient
) -> None:
    kb = await make_kb(logged_in, "규정", slug="rules")
    workflow_id = await save_flow(logged_in, rag_graph(kb["id"]))
    exported = await logged_in.post(f"/api/console/workflows/{workflow_id}/export", headers=FETCH)
    assert exported.status_code == 200, exported.text
    text = exported.text
    assert "kb_slugs:" in text and "- rules" in text
    # 다른 PC에서 내보낸 파일처럼 id를 바꿔 넣으면 slug로 이 워크스페이스의 KB를 찾는다.
    moved = text.replace(kb["id"], str(uuid.uuid4()))

    imported = await logged_in.post(
        "/api/console/workflows/import",
        files={"file": ("rag.yml", moved.encode("utf-8"), "application/yaml")},
        headers=FETCH,
    )

    assert imported.status_code == 201, imported.text
    body = imported.json()
    assert {item["code"] for item in body["converted"]["warnings"]} >= {"knowledge_relinked"}
    assert body["issues"] == []
    draft = (await logged_in.get(f"/api/console/workflows/{body['workflow_id']}/draft")).json()
    retrieval = next(item for item in draft["graph"]["nodes"] if item["id"] == "knowledge_1")
    assert retrieval["config"]["kb_ids"] == [kb["id"]]
    assert "kb_slugs" not in retrieval["config"]

    deleted = await logged_in.delete(f"/api/console/knowledge/{kb['id']}", headers=FETCH)
    assert deleted.status_code == 204, deleted.text
    again = (await logged_in.get(f"/api/console/workflows/{body['workflow_id']}/draft")).json()
    assert [(item["code"], item["node_id"]) for item in again["issues"]] == [
        ("knowledge_base_not_found", "knowledge_1")
    ]
    refused = await logged_in.post(f"/api/console/workflows/{body['workflow_id']}/publish", json={})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "draft_invalid"


async def test_node_types_list_the_knowledge_nodes_and_options(logged_in: AsyncClient) -> None:
    response = await logged_in.get("/api/console/node-types")

    assert response.status_code == 200, response.text
    body = response.json()
    types = {item["type"]: item for item in body["items"]}
    assert [o["name"] for o in types["knowledge_retrieval"]["outputs"]] == [
        "result",
        "result_text",
        "count",
    ]
    assert [o["name"] for o in types["knowledge_index"]["outputs"]] == [
        "document_ids",
        "batch",
        "status",
    ]
    options = body["kb_options"]
    assert options["max_kbs"] == 5 and options["top_k_max"] == 20
    assert options["operators"]["time"] == ["is", "empty", "not empty", "before", "after"]
    assert options["duplicate_policies"] == ["skip", "replace", "add"]
    assert options["paid_key_configured"] is False
    assert options["default_retrieval"]["search_method"] == "hybrid_search"
