"""지식 베이스를 붙인 에이전트(3단계 설계 8.4, K6, M24).

버전의 knowledge.kb_ids가 있으면 search_knowledge와 시스템 프롬프트의 지식 베이스 절이 붙고, 도구가
찾은 인용이 tool_call.completed와 런의 마지막 assistant 메시지에 실린다. 빌더는 지운 KB·검색 설정
없음을 경고하고 발행은 막는다.
"""

import base64
import hashlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import text, update

from app.config import Settings
from app.db.models import KbDocument, KbSegment
from app.db.repositories.knowledge import KbCounterRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.knowledge.units import FtsRow, fts_text
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ModelEvent
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
    # 비밀 환경변수를 저장하려면 ENCRYPTION_KEY가 있어야 한다(KS9 시험).
    key = SecretStr(base64.b64encode(bytes(range(32))).decode())
    return short_settings.model_copy(
        update={"llm_fake": True, "kb_embed_rpm": 6000, "encryption_key": key}
    )


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


async def make_kb(client: AsyncClient, name: str, slug: str) -> dict[str, Any]:
    response = await client.post(
        "/api/console/knowledge",
        json={
            "name": name,
            "slug": slug,
            "description": f"{name} 문서",
            # 전문 검색만 하는 KB라 임베딩 없이 바로 찾는다.
            "indexing_technique": "economy",
            "data_policy": {"acknowledged": True},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def seed(app: FastAPI, kb: dict[str, Any], name: str, content: str) -> None:
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
            name=name,
            data_source_type="paste_text",
            filename=f"{name}.txt",
            mime="text/plain",
            size_bytes=1,
            sha256=hashlib.sha256(name.encode()).hexdigest(),
            storage_key=f"seed/{uuid.uuid4().hex}",
            batch="seed",
            indexing_status="indexing",
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
                index_state={"fts": "pending", "vector": "skip"},
            )
        )
        document_id = document.id
    await runtime.fts.ensure(kb_id, workspace_id=workspace_id)
    await runtime.fts.upsert(kb_id, [FtsRow(unit_no, fts_text(content, None))])
    # 레인 작업처럼 색인 행을 다 쓴 뒤에 ok와 completed를 적는다(이유는 test_knowledge_retrieve.py의
    # Seeder.document).
    async with app.state.db.write() as session:
        await session.execute(
            update(KbSegment)
            .where(KbSegment.document_id == document_id)
            .values(index_state={"fts": "ok", "vector": "skip"})
        )
        await session.execute(
            update(KbDocument)
            .where(KbDocument.id == document_id)
            .values(indexing_status="completed")
        )


async def agent_with(client: AsyncClient, **draft: Any) -> tuple[str, dict[str, Any]]:
    agent = (await client.post("/api/console/agents", json={"name": "규정 도우미"})).json()
    current = (await client.get(f"/api/console/agents/{agent['id']}/draft")).json()
    body = {key: current[key] for key in DRAFT_FIELDS}
    body.update({"env_vars": [], "tools": [], **draft})
    saved = await client.put(f"/api/console/agents/{agent['id']}/draft", json=body)
    assert saved.status_code == 200, saved.text
    return agent["id"], saved.json()


def searching(seen: list[ChatRequest], query: str) -> Callable[[ChatRequest], list[ModelEvent]]:
    def turn(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return [*fake_tool_call("search_knowledge", {"query": query}), fake_end("tool_use")]

    return turn


async def test_agent_searches_and_cites_on_its_final_message(
    app: FastAPI,
    logged_in: AsyncClient,
    make_client: Callable[[], AsyncClient],
    script: Any,
) -> None:
    kb = await make_kb(logged_in, "휴가 규정", "leave")
    await seed(app, kb, "연차 규정", "연차 휴가는 1년에 15일이다.")
    agent_id, draft = await agent_with(logged_in, knowledge={"kb_ids": [kb["id"]]})
    assert draft["knowledge"] == {"kb_ids": [kb["id"]], "retrieval_model": None}
    assert draft["warnings"] == []
    seen: list[ChatRequest] = []
    script.add(searching(seen, "연차 휴가"), [*fake_text("연차는 15일이다 [1]."), fake_end()])
    session = (
        await logged_in.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    ).json()

    sent = await logged_in.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": "연차가 며칠이야?", "response_mode": "blocking"},
    )

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["run"]["status"] == "succeeded", body["run"]
    [cited] = body["retriever_resources"]
    assert cited["position"] == 1 and cited["document_name"] == "연차 규정"
    assert cited["content"] == "연차 휴가는 1년에 15일이다."
    request = seen[0]
    assert [tool.name for tool in request.tools] == ["search_knowledge"]
    assert (
        "# 지식 베이스" in request.system
        and "- leave: 휴가 규정 — 휴가 규정 문서" in request.system
    )
    assert request.tier == "free"
    messages = (await logged_in.get(f"/api/console/sessions/{session['id']}/messages")).json()
    steps = [item for item in messages["items"] if item["role"] == "assistant"]
    # 스텝마다 붙었던 인용은 런이 끝나면 마지막 메시지로 모인다.
    assert [len(item["retriever_resources"]) for item in steps] == [0, 1]
    events = (await logged_in.get(f"/api/console/runs/{body['run']['id']}/events")).json()
    [completed] = [item for item in events["items"] if item["type"] == "tool_call.completed"]
    assert completed["data"]["retriever_resources"][0]["segment_id"] == cited["segment_id"]

    published = await logged_in.post(f"/api/console/agents/{agent_id}/publish", json={})
    assert published.status_code == 201, published.text
    key = await logged_in.post(f"/api/console/agents/{agent_id}/api-keys", json={"name": "봇"})
    headers = {"Authorization": f"Bearer {key.json()['key']}"}
    script.add(searching([], "연차"), [*fake_text("15일 [1]"), fake_end()])
    api = make_client()
    reply = await api.post(
        "/api/v1/chat-messages",
        json={"query": "연차?", "response_mode": "blocking"},
        headers=headers,
    )
    assert reply.status_code == 200, reply.text
    assert reply.json()["metadata"]["retriever_resources"][0]["document_name"] == "연차 규정"


async def test_secret_env_values_in_search_results_are_masked(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    # KS9: all_shell에서도 search_knowledge는 승인 없이 돌고, 결과(문서)와 모델 답에 비밀 환경변수
    # 값이 섞여도 이벤트·메시지 행·응답에 원문이 남지 않는다. 모델은 원문 결과를 본다.
    secret = "tok-KS9-" + "7f3a9c" * 4
    kb = await make_kb(logged_in, "운영 메모", "ops")
    await seed(app, kb, "배포 메모", f"배포 토큰은 {secret} 이다. 연차 규정과 무관하다.")
    agent_id, draft = await agent_with(
        logged_in,
        knowledge={"kb_ids": [kb["id"]]},
        env_vars=[{"name": "DEPLOY_TOKEN", "value": secret, "secret": True}],
    )
    assert draft["approval_mode"] == "all_shell"
    seen: list[ChatRequest] = []

    def answering(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        # 답 글은 조각 경계에 비밀값이 걸치게 둘로 나눠 흘린다.
        return [
            *fake_text(f"토큰은 {secret[:5]}"),
            *fake_text(f"{secret[5:]} 입니다 [1]."),
            fake_end(),
        ]

    script.add(searching(seen, "배포 토큰"), answering)
    session = (
        await logged_in.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    ).json()

    sent = await logged_in.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": "배포 토큰이 뭐야?", "response_mode": "blocking"},
    )

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["run"]["status"] == "succeeded", body["run"]
    run_id = body["run"]["id"]
    events = (await logged_in.get(f"/api/console/runs/{run_id}/events")).json()["items"]
    [completed] = [item for item in events if item["type"] == "tool_call.completed"]
    # 승인 카드 없이 끝났다.
    assert not [item for item in events if item["type"] == "run.waiting_input"]
    assert "***" in completed["data"]["output_excerpt"]
    assert "***" in completed["data"]["retriever_resources"][0]["content"]
    # 저장 이벤트에는 델타가 없다. 델타 조각 경계는 unit/test_run_masker.py가 본다.
    answers = [item["data"]["text"] for item in events if item["type"] == "message.completed"]
    assert "토큰은 *** 입니다 [1]." in answers
    messages = (await logged_in.get(f"/api/console/sessions/{session['id']}/messages")).json()
    for label, payload in (("events", events), ("messages", messages), ("response", body)):
        assert secret not in json.dumps(payload, ensure_ascii=False), label
    async with app.state.db.read() as session_db:
        rows = (
            await session_db.execute(
                text(
                    "SELECT blocks, text, retriever_resources FROM message WHERE run_id = :id"
                    " UNION ALL SELECT payload, '', '' FROM run_event WHERE run_id = :id"
                ),
                {"id": run_id},
            )
        ).all()
    assert rows and not [row for row in rows if secret in " ".join(str(value) for value in row)]
    # 모델은 도구 결과 원문을 받았다(가리는 것은 기록과 화면이다).
    sent_to_model = json.dumps(
        [message.model_dump(mode="json") for message in seen[-1].messages], ensure_ascii=False
    )
    assert secret in sent_to_model


SECRET_9 = "tok-KS9-" + "5e1d" * 6


async def secret_agent(app: FastAPI, client: AsyncClient) -> str:
    kb = await make_kb(client, "운영 메모", "ops")
    await seed(app, kb, "배포 메모", f"배포 토큰은 {SECRET_9} 이다.")
    agent_id, draft = await agent_with(
        client,
        tools=["shell"],
        knowledge={"kb_ids": [kb["id"]]},
        env_vars=[{"name": "DEPLOY_TOKEN", "value": SECRET_9, "secret": True}],
    )
    assert draft["approval_mode"] == "all_shell"
    return agent_id


async def pause_on(
    client: AsyncClient, agent_id: str, script: Any, seen: list[ChatRequest], code: str
) -> str:
    """검색 → run_python(승인 대기)까지 돌리고 런 id를 돌려준다. 재개 뒤 답은 한 턴이다."""

    def running(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return [*fake_tool_call("run_python", {"code": code}), fake_end("tool_use")]

    def answering(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return [*fake_text("길이를 쟀다."), fake_end()]

    script.add(searching(seen, "배포 토큰"), running, answering)
    session = (
        await client.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    ).json()
    first = await client.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": "배포 토큰 길이를 재 줘", "response_mode": "blocking"},
    )
    assert first.json()["run"]["status"] == "waiting_input", first.text
    return first.json()["run"]["id"]


async def approve(client: AsyncClient, run_id: str) -> dict[str, Any]:
    resumed = await client.post(
        f"/api/console/runs/{run_id}/input",
        json={"decision": "approve", "response_mode": "blocking"},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["run"]["status"] == "succeeded", resumed.text
    events = (await client.get(f"/api/console/runs/{run_id}/events")).json()["items"]
    [ran] = [
        item["data"]
        for item in events
        if item["type"] == "tool_call.completed" and item["data"].get("exit_code") is not None
    ]
    return ran


async def test_after_an_approval_the_resumed_model_sees_the_masked_history(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    # KS9의 절충: 메시지 행을 가리므로 모델이 원문을 보는 것은 한 실행 구간 안에서뿐이다. 승인 뒤
    # 재개한 구간은 DB에서 다시 읽은 히스토리라 검색 결과의 비밀값이 ***다. 에이전트가 비밀값을
    # 써야 하면 리터럴 대신 환경변수를 쓴다.
    agent_id = await secret_agent(app, logged_in)
    seen: list[ChatRequest] = []
    code = "import os\nprint(len(os.environ['DEPLOY_TOKEN']))"

    run_id = await pause_on(logged_in, agent_id, script, seen, code)

    # 멈추기 전 구간에서는 모델이 원문 검색 결과를 받았다.
    assert SECRET_9 in dumped(seen[1])
    ran = await approve(logged_in, run_id)
    history = dumped(seen[-1])
    assert SECRET_9 not in history and "***" in history
    assert ran["output_excerpt"].strip().endswith(str(len(SECRET_9)))


async def test_the_waiting_approval_card_is_masked_but_runs_the_raw_input(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    # 승인 대기 런의 pending_interaction도 가려 저장한다. 새로 고친 뒤 승인 카드에 원문이 나오지
    # 않는다. 재개는 메시지 행의 tool_use 입력(원문)을 실행한다.
    agent_id = await secret_agent(app, logged_in)
    seen: list[ChatRequest] = []

    run_id = await pause_on(logged_in, agent_id, script, seen, f"print(len('{SECRET_9}'))")

    card = (await logged_in.get(f"/api/console/runs/{run_id}")).json()["pending_interaction"]
    assert card["tool_name"] == "run_python"
    assert SECRET_9 not in json.dumps(card, ensure_ascii=False)
    assert "***" in card["input"]["code"]
    ran = await approve(logged_in, run_id)
    # *** 세 글자가 아니라 원문의 길이가 찍혔다.
    assert ran["output_excerpt"].strip().endswith(str(len(SECRET_9)))


def dumped(request: ChatRequest) -> str:
    return json.dumps(
        [message.model_dump(mode="json") for message in request.messages], ensure_ascii=False
    )


async def test_numbers_continue_after_a_pause_and_gather_once(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    kb = await make_kb(logged_in, "휴가 규정", "leave")
    await seed(app, kb, "연차 규정", "연차 휴가는 1년에 15일이다.")
    await seed(app, kb, "병가 규정", "병가 기간은 60일이다.")
    agent_id, _ = await agent_with(logged_in, tools=["ask_user"], knowledge={"kb_ids": [kb["id"]]})
    script.add(
        searching([], "휴가"),
        [*fake_tool_call("ask_user", {"question": "병가도 볼까?"}), fake_end("tool_use")],
    )
    session = (
        await logged_in.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    ).json()
    first = await logged_in.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": "휴가가 며칠이야?", "response_mode": "blocking"},
    )
    assert first.json()["run"]["status"] == "waiting_input"
    script.add(searching([], "병가 휴가"), [*fake_text("연차 [1], 병가 [2]."), fake_end()])

    # 재개한 런은 DB의 스텝 메시지에서 번호표를 다시 만들어 이어 매긴다.
    resumed = await logged_in.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": "응", "response_mode": "blocking"},
    )

    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["run"]["status"] == "succeeded", body["run"]
    cited = {item["document_name"]: item["position"] for item in body["retriever_resources"]}
    assert cited == {"연차 규정": 1, "병가 규정": 2}
    events = (await logged_in.get(f"/api/console/runs/{body['run']['id']}/events")).json()
    searches = [item["data"] for item in events["items"] if item["type"] == "tool_call.completed"]
    second = searches[-1]["retriever_resources"]
    assert sorted(item["position"] for item in second) == [1, 2]
    messages = (await logged_in.get(f"/api/console/sessions/{session['id']}/messages")).json()
    steps = [item for item in messages["items"] if item["role"] == "assistant"]
    # 같은 세그먼트(연차)가 두 스텝에서 나왔지만 마지막 메시지에 한 번만 남는다.
    assert [len(item["retriever_resources"]) for item in steps] == [0, 0, 0, 2]


async def test_rerank_calls_inside_the_tool_count_toward_the_run_usage(
    app: FastAPI, logged_in: AsyncClient, script: Any
) -> None:
    kb = await make_kb(logged_in, "휴가 규정", "leave")
    await seed(app, kb, "연차 규정", "연차 휴가는 1년에 15일이다.")
    agent_id, _ = await agent_with(
        logged_in,
        knowledge={
            "kb_ids": [kb["id"]],
            "retrieval_model": {"search_method": "full_text_search", "reranking_mode": "llm"},
        },
    )
    rerank = fake_tool_call("rank_passages", {"ranking": [{"index": 1, "relevance": 0.9}]})
    script.add(searching([], "휴가"), rerank, [*fake_text("15일 [1]."), fake_end()])
    session = (
        await logged_in.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    ).json()

    sent = await logged_in.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": "휴가?", "response_mode": "blocking"},
    )

    body = sent.json()
    assert body["run"]["status"] == "succeeded", body["run"]
    events = (await logged_in.get(f"/api/console/runs/{body['run']['id']}/events")).json()
    usage = [item["data"] for item in events["items"] if item["type"] == "usage"]
    assert [item.get("tool_call_id") is not None for item in usage] == [False, True, False]
    # 재순위 호출까지 런 합계(예산·통계가 보는 run.usage)에 든다.
    total = body["run"]["usage"]
    assert total["input_tokens"] == sum(item["input_tokens"] for item in usage)


async def test_agent_without_knowledge_has_no_search_tool(
    logged_in: AsyncClient, script: Any
) -> None:
    agent_id, draft = await agent_with(logged_in)
    assert draft["knowledge"] == {"kb_ids": [], "retrieval_model": None}
    seen: list[ChatRequest] = []

    def answer(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return [*fake_text("안녕"), fake_end()]

    script.add(answer)
    session = (
        await logged_in.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    ).json()
    sent = await logged_in.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": "안녕", "response_mode": "blocking"},
    )

    assert sent.json()["retriever_resources"] == []
    assert seen[0].tools == [] and "# 지식 베이스" not in seen[0].system


async def test_builder_warns_and_publish_refuses(logged_in: AsyncClient) -> None:
    first = await make_kb(logged_in, "규정", "rules")
    second = await make_kb(logged_in, "FAQ", "faq")
    missing = str(uuid.uuid4())

    agent_id, draft = await agent_with(
        logged_in, knowledge={"kb_ids": [first["id"], second["id"], missing]}
    )

    warned = {(item["code"], item["kb_id"]) for item in draft["warnings"]}
    assert ("knowledge_base_not_found", missing) in warned
    assert ("retrieval_settings_required", None) in warned
    refused = await logged_in.post(f"/api/console/agents/{agent_id}/publish", json={})
    assert refused.status_code == 422, refused.text
    details = refused.json()["error"]["details"]
    assert [item["type"] for item in details] == ["retrieval_settings_required"]
    # 지식 탭을 모르는 화면이 knowledge 없이 저장해도 지우지 않는다.
    current = (await logged_in.get(f"/api/console/agents/{agent_id}/draft")).json()
    body = {key: current[key] for key in DRAFT_FIELDS}
    resaved = await logged_in.put(
        f"/api/console/agents/{agent_id}/draft", json={**body, "env_vars": []}
    )
    assert resaved.json()["knowledge"]["kb_ids"] == [first["id"], second["id"], missing]
    too_many = await logged_in.put(
        f"/api/console/agents/{agent_id}/draft",
        json={
            **body,
            "updated_at": resaved.json()["updated_at"],
            "env_vars": [],
            "knowledge": {"kb_ids": [first["id"]], "retrieval_model": {"top_k": 99}},
        },
    )
    assert too_many.status_code == 422, too_many.text
    fixed = await logged_in.put(
        f"/api/console/agents/{agent_id}/draft",
        json={
            **body,
            "updated_at": resaved.json()["updated_at"],
            "env_vars": [],
            "knowledge": {
                "kb_ids": [first["id"], second["id"]],
                "retrieval_model": {"top_k": 3, "search_method": "full_text_search"},
            },
        },
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["knowledge"]["retrieval_model"]["reranking_mode"] == "weighted_score"
    assert (
        await logged_in.post(f"/api/console/agents/{agent_id}/publish", json={})
    ).status_code == 201
