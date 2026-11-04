"""KB 단위 서비스 API(3단계 설계 8.5, K7, KS7). KB 키는 그 KB 하나만 다루고 다른 KB id는 404다.

API로 만든 문서는 지식 레인이 가짜 임베더(LLM_FAKE)로 색인하고, 나머지 시험의 문서는 레인을 거치지
않고 app.db·index.db에 바로 넣는다.
"""

import asyncio
import hashlib
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.db.models import KbDocument, KbSegment
from app.db.repositories.knowledge import KbCounterRepository, KnowledgeBaseRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.knowledge.units import FtsRow, VecRow, doc_id_of
from app.llm.embeddings import EmbeddingConfig
from app.llm.fake_adapter import FakeAdapter, fake_rate_limit_error, fake_tool_call
from app.services.knowledge_runtime import KnowledgeRuntime

BASE = "/api/console/knowledge"
V1 = "/api/v1/datasets"
FETCH = {"X-Requested-With": "fetch"}


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"llm_fake": True, "kb_embed_rpm": 6000})


async def create_kb(client: AsyncClient, name: str = "사내 규정") -> dict[str, Any]:
    response = await client.post(BASE, json={"name": name, "data_policy": {"acknowledged": True}})
    assert response.status_code == 201, response.text
    return response.json()


async def kb_key(client: AsyncClient, kb_id: str) -> dict[str, str]:
    response = await client.post(f"{BASE}/{kb_id}/api-keys", json={"name": "외부 앱"})
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["knowledge_base_id"] == kb_id
    assert created["scopes"] == ["knowledge"]
    return {"Authorization": f"Bearer {created['key']}"}


async def wait_indexed(
    client: AsyncClient, headers: dict[str, str], kb_id: str, batch: str
) -> list[dict[str, Any]]:
    """batch의 문서가 모두 completed나 error가 될 때까지 indexing-status를 본다(최대 20초)."""
    url = f"{V1}/{kb_id}/documents/{batch}/indexing-status"
    items: list[dict[str, Any]] = []
    for _ in range(200):
        response = await client.get(url, headers=headers)
        assert response.status_code == 200, response.text
        items = response.json()["data"]
        if items and all(item["indexing_status"] in ("completed", "error") for item in items):
            return items
        await asyncio.sleep(0.1)
    raise AssertionError(f"색인이 끝나지 않았다: {items}")


async def seed(app: FastAPI, kb: dict[str, Any], name: str, text: str) -> uuid.UUID:
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
            size_bytes=len(text.encode()),
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            storage_key=f"seed/{uuid.uuid4().hex}",
            batch="seed",
            indexing_status="completed",
            word_count=len(text),
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
                content=text,
                word_count=len(text),
                tokens=len(text),
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                status="completed",
                index_state={"fts": "ok", "vector": "ok"},
            )
        )
        document_id = document.id
    config = EmbeddingConfig.model_validate(kb["embedding"])
    await runtime.fts.ensure(kb_id, workspace_id=workspace_id)
    await runtime.fts.upsert(kb_id, [FtsRow(unit_no, text)])
    await runtime.vectors.ensure(kb_id, config, workspace_id=workspace_id)
    vector = await runtime.embeddings.embed_single(text, config)
    await runtime.vectors.upsert(kb_id, [VecRow(unit_no, vector, doc_id_of(document_id))])
    async with app.state.db.write() as session:
        await KnowledgeBaseRepository(session, workspace_id).refresh_counts(kb_id)
    return document_id


async def test_a_kb_key_reads_and_searches_its_dataset(
    logged_in: AsyncClient, client: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    await seed(app, kb, "휴가 규정", "연차휴가는 입사 첫해에 15일이다")
    await seed(app, kb, "출장 규정", "출장비는 30일 안에 정산한다")
    headers = await kb_key(logged_in, kb["id"])

    listed = await client.get(V1, headers=headers)
    one = await client.get(f"{V1}/{kb['id']}", headers=headers)
    found = await client.post(
        f"{V1}/{kb['id']}/retrieve", headers=headers, json={"query": "연차 며칠"}
    )
    dify_shaped = await client.post(
        f"{V1}/{kb['id']}/retrieve",
        headers=headers,
        json={
            "query": "출장비 정산",
            "retrieval_model": {
                "search_method": "keyword_search",
                "reranking_enable": False,
                "top_k": 1,
                "score_threshold_enabled": False,
                "score_threshold": None,
            },
        },
    )

    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["data"]] == [kb["id"]]
    assert listed.json()["total"] == 1
    dataset = one.json()
    assert dataset["embedding_model"] == "gemini-embedding-2"
    assert dataset["indexing_technique"] == "high_quality"
    assert dataset["doc_form"] == "text_model"
    assert dataset["document_count"] == 2
    assert dataset["word_count"] == len("연차휴가는 입사 첫해에 15일이다") + len(
        "출장비는 30일 안에 정산한다"
    )
    assert found.status_code == 200, found.text
    records = found.json()["records"]
    assert records[0]["segment"]["document"]["name"] == "휴가 규정"
    assert found.json()["query"] == {"content": "연차 며칠"}
    assert dify_shaped.status_code == 200, dify_shaped.text
    assert [r["segment"]["document"]["name"] for r in dify_shaped.json()["records"]] == [
        "출장 규정"
    ]
    logs = await logged_in.get(f"{BASE}/{kb['id']}/query-logs")
    assert {item["source"] for item in logs.json()["items"]} == {"api"}


async def test_documents_are_created_and_followed_by_batch(
    logged_in: AsyncClient, client: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    headers = await kb_key(logged_in, kb["id"])
    field = await client.post(
        f"{V1}/{kb['id']}/metadata", headers=headers, json={"name": "year", "type": "number"}
    )

    by_text = await client.post(
        f"{V1}/{kb['id']}/document/create-by-text",
        headers=headers,
        json={
            "name": "회의록",
            "text": "11월 회의에서 채용 계획을 정했다",
            "indexing_technique": "high_quality",
            "doc_metadata": {"year": 2026},
        },
    )
    unknown = await client.post(
        f"{V1}/{kb['id']}/document/create-by-text",
        headers=headers,
        json={"name": "메모", "text": "아무 내용", "doc_metadata": {"owner": "me"}},
    )
    by_file = await client.post(
        f"{V1}/{kb['id']}/document/create-by-file",
        headers=headers,
        data={"data": '{"allow_duplicate": true}'},
        files={"file": ("notes.txt", "파일로 올린 메모".encode(), "text/plain")},
    )
    batch = by_text.json()["batch"]
    status = await wait_indexed(client, headers, kb["id"], batch)
    found = await client.post(
        f"{V1}/{kb['id']}/retrieve", headers=headers, json={"query": "채용 계획"}
    )
    listed = await client.get(f"{V1}/{kb['id']}/documents", headers=headers)

    assert field.status_code == 201, field.text
    assert by_text.status_code == 200, by_text.text
    document = by_text.json()["document"]
    assert document["data_source_type"] == "api"
    assert [(item["name"], item["value"]) for item in document["doc_metadata"]] == [
        ("year", 2026.0)
    ]
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "metadata_field_not_found"
    assert by_file.status_code == 200, by_file.text
    assert by_file.json()["document"]["filename"] == "notes.txt"
    # 지식 레인이 가짜 임베더로 색인을 끝내면 KB 키로 바로 찾힌다(K7).
    assert [item["id"] for item in status] == [document["id"]]
    assert status[0]["indexing_status"] == "completed"
    assert status[0]["completed_segments"] == status[0]["total_segments"] == 1
    assert found.json()["records"][0]["segment"]["document"]["name"] == "회의록"
    assert listed.json()["total"] == 2
    assert {item["name"] for item in listed.json()["data"]} == {"회의록", "notes.txt"}

    deleted = await client.delete(f"{V1}/{kb['id']}/documents/{document['id']}", headers=headers)
    after = await client.get(f"{V1}/{kb['id']}/documents", headers=headers)
    assert deleted.status_code == 204
    assert after.json()["total"] == 1


async def test_document_status_segments_and_metadata(
    logged_in: AsyncClient, client: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    document_id = await seed(app, kb, "보안 규정", "비밀번호는 12자 이상이다")
    headers = await kb_key(logged_in, kb["id"])
    docs = f"{V1}/{kb['id']}/documents"

    archived = await client.patch(
        f"{docs}/status/archive", headers=headers, json={"document_ids": [str(document_id)]}
    )
    hidden = await client.post(
        f"{V1}/{kb['id']}/retrieve", headers=headers, json={"query": "비밀번호 길이"}
    )
    restored = await client.patch(
        f"{docs}/status/un_archive", headers=headers, json={"document_ids": [str(document_id)]}
    )
    segments = await client.get(f"{docs}/{document_id}/segments", headers=headers)
    segment_id = segments.json()["data"][0]["id"]
    one = await client.get(f"{docs}/{document_id}/segments/{segment_id}", headers=headers)
    built_in = await client.post(f"{V1}/{kb['id']}/metadata/built-in/enable", headers=headers)
    fields = await client.get(f"{V1}/{kb['id']}/metadata", headers=headers)

    assert archived.status_code == 200, archived.text
    assert archived.json() == {"result": "success", "skipped": []}
    assert hidden.json()["records"] == []
    assert restored.status_code == 200, restored.text
    assert segments.status_code == 200, segments.text
    assert segments.json()["doc_form"] == "text_model"
    assert segments.json()["total"] == 1
    assert one.json()["data"]["content"] == "비밀번호는 12자 이상이다"
    assert built_in.status_code == 200, built_in.text
    names = {field["name"] for field in fields.json()["items"]}
    assert {"document_name", "upload_date", "source"} <= names


async def test_other_kbs_and_other_keys_are_refused(
    logged_in: AsyncClient, client: AsyncClient, app: FastAPI
) -> None:
    mine = await create_kb(logged_in, "내 KB")
    other = await create_kb(logged_in, "남의 KB")
    await seed(app, other, "남의 문서", "남의 KB에만 있는 글")
    headers = await kb_key(logged_in, mine["id"])
    agent = await logged_in.post("/api/console/agents", json={"name": "Helper"})
    agent_key = await logged_in.post(
        f"/api/console/agents/{agent.json()['id']}/api-keys", json={"name": "에이전트"}
    )
    agent_headers = {"Authorization": f"Bearer {agent_key.json()['key']}"}

    responses = [
        await client.get(f"{V1}/{other['id']}", headers=headers),
        await client.post(f"{V1}/{other['id']}/retrieve", headers=headers, json={"query": "글"}),
        await client.get(f"{V1}/{other['id']}/documents", headers=headers),
        await client.post(
            f"{V1}/{other['id']}/document/create-by-text",
            headers=headers,
            json={"name": "끼어들기", "text": "x"},
        ),
    ]
    wrong = await client.get(V1, headers=agent_headers)
    anonymous = await client.get(V1)
    keys = await logged_in.get(f"{BASE}/{mine['id']}/api-keys")
    key_id = keys.json()["items"][0]["id"]
    revoked = await logged_in.delete(f"{BASE}/{mine['id']}/api-keys/{key_id}", headers=FETCH)
    after = await client.get(V1, headers=headers)

    for response in responses:
        assert response.status_code == 404, response.text
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "wrong_target"
    assert wrong.json()["error"]["details"] == {"key_target": "agent", "expected": ["knowledge"]}
    assert anonymous.status_code == 401
    assert revoked.status_code == 204
    assert after.status_code == 401
    other_docs = await logged_in.get(f"{BASE}/{other['id']}/documents")
    assert [item["name"] for item in other_docs.json()["items"]] == ["남의 문서"]


async def test_retrieve_takes_dify_shapes_but_not_model_calls_by_default(
    logged_in: AsyncClient, client: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    first = await seed(app, kb, "영업팀 규정", "출장비 정산은 30일 안에 한다")
    second = await seed(app, kb, "개발팀 규정", "출장비 정산은 14일 안에 한다")
    headers = await kb_key(logged_in, kb["id"])
    field = await client.post(
        f"{V1}/{kb['id']}/metadata", headers=headers, json={"name": "team", "type": "string"}
    )
    for document_id, team in ((first, "sales"), (second, "dev")):
        saved = await client.post(
            f"{V1}/{kb['id']}/documents/metadata",
            headers=headers,
            json={
                "document_ids": [str(document_id)],
                "values": [{"field_id": field.json()["id"], "value": team}],
                "apply_to_all": True,
            },
        )
        assert saved.status_code == 200, saved.text
    chat = FakeAdapter(
        [fake_tool_call("rank_passages", {"ranking": [{"index": 1, "relevance": 0.4}]})]
    )
    app.state.llm = chat
    url = f"{V1}/{kb['id']}/retrieve"
    dify_filter = {
        "logical_operator": "AND",
        "conditions": [{"name": "team", "comparison_operator": "is", "value": "dev"}],
    }

    filtered = await client.post(
        url,
        headers=headers,
        json={
            "query": "출장비 정산",
            "retrieval_model": {
                "search_method": "hybrid_search",
                "reranking_mode": "reranking_model",
                "top_k": 3,
                "metadata_filtering_conditions": dify_filter,
            },
        },
    )
    asked = await client.post(
        url,
        headers=headers,
        json={
            "query": "출장비 정산",
            "retrieval_model": {"reranking_enable": True, "reranking_mode": "reranking_model"},
        },
    )
    automatic = await client.post(
        url,
        headers=headers,
        json={"query": "개발팀 출장비", "metadata_filtering_conditions": {"mode": "automatic"}},
    )
    too_long = await client.post(url, headers=headers, json={"query": "가" * 251})

    assert filtered.status_code == 200, filtered.text
    assert [r["segment"]["document"]["name"] for r in filtered.json()["records"]] == ["개발팀 규정"]
    # reranking_enable이 없으면 재순위 모델 설정이어도 채팅 호출을 하지 않는다.
    assert filtered.json()["records"][0]["fusion_score"] is None
    assert asked.status_code == 200, asked.text
    assert asked.json()["records"][0]["score"] == 0.4
    assert len(chat.requests) == 1
    assert automatic.status_code == 422
    assert automatic.json()["error"]["code"] == "auto_filter_not_supported"
    assert too_long.status_code == 422


async def test_document_metadata_values_are_checked_before_creating(
    logged_in: AsyncClient, client: AsyncClient
) -> None:
    kb = await create_kb(logged_in)
    headers = await kb_key(logged_in, kb["id"])
    await client.post(
        f"{V1}/{kb['id']}/metadata", headers=headers, json={"name": "year", "type": "number"}
    )

    by_text = await client.post(
        f"{V1}/{kb['id']}/document/create-by-text",
        headers=headers,
        json={"name": "회의록", "text": "11월 회의", "doc_metadata": {"year": "올해"}},
    )
    by_file = await client.post(
        f"{V1}/{kb['id']}/document/create-by-file",
        headers=headers,
        data={"data": '{"doc_metadata": {"year": "작년"}}'},
        files={"file": ("notes.txt", "메모".encode(), "text/plain")},
    )
    listed = await client.get(f"{V1}/{kb['id']}/documents", headers=headers)

    for response in (by_text, by_file):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"][0]["type"] == "metadata_value_invalid"
    assert listed.json()["total"] == 0


async def test_document_metadata_also_takes_dify_operation_data(
    logged_in: AsyncClient, client: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    first = str(await seed(app, kb, "영업팀 규정", "출장비 정산은 30일 안에 한다"))
    second = str(await seed(app, kb, "개발팀 규정", "출장비 정산은 14일 안에 한다"))
    headers = await kb_key(logged_in, kb["id"])
    fields_url = f"{V1}/{kb['id']}/metadata"
    team = await client.post(fields_url, headers=headers, json={"name": "team", "type": "string"})
    await client.post(fields_url, headers=headers, json={"name": "year", "type": "number"})
    enabled = await client.post(f"{fields_url}/built-in/enable", headers=headers)
    assert enabled.status_code == 200, enabled.text
    [name_field] = [f for f in enabled.json()["items"] if f["name"] == "document_name"]
    url = f"{V1}/{kb['id']}/documents/metadata"

    async def send(*operations: dict[str, Any]) -> Any:
        return await client.post(url, headers=headers, json={"operation_data": list(operations)})

    full = await send(
        {
            "document_id": first,
            "metadata_list": [
                {"id": team.json()["id"], "name": "team", "value": "sales"},
                {"name": "year", "value": 2024},
                # 조회한 목록을 그대로 돌려보내도 기본 필드는 시스템 값을 지킨다.
                {"id": name_field["id"], "name": "document_name", "value": "바꾼 이름"},
            ],
        },
        {"document_id": second, "metadata_list": [{"name": "team", "value": "dev"}]},
    )
    partial = await send(
        {
            "document_id": first,
            "metadata_list": [{"name": "team", "value": "hr"}],
            "partial_update": True,
        }
    )
    replaced = await send(
        {"document_id": first, "metadata_list": [{"name": "team", "value": "ops"}]}
    )
    unknown = await send(
        {"document_id": second, "metadata_list": [{"name": "owner", "value": "x"}]}
    )

    def values(response: Any, *, built_in: bool = False) -> dict[str, dict[str, Any]]:
        assert response.status_code == 200, response.text
        return {
            item["document_id"]: {
                value["name"]: value["value"]
                for value in item["doc_metadata"]
                if value["built_in"] is built_in
            }
            for item in response.json()["items"]
        }

    assert values(full) == {first: {"team": "sales", "year": 2024}, second: {"team": "dev"}}
    assert values(full, built_in=True)[first]["document_name"] == "영업팀 규정"
    assert values(partial) == {first: {"team": "hr", "year": 2024}}
    # partial_update가 거짓(기본)이면 목록에 없는 사용자 필드 값은 지운다.
    assert values(replaced) == {first: {"team": "ops"}}
    assert unknown.status_code == 422, unknown.text
    [problem] = unknown.json()["error"]["details"]
    assert problem["type"] == "metadata_field_not_found"
    assert problem["loc"] == ["body", "operation_data", 0, "metadata_list", 0]


async def test_an_unavailable_index_hides_its_reason_from_the_service_api(
    logged_in: AsyncClient, client: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    headers = await kb_key(logged_in, kb["id"])
    index_db = app.state.knowledge.index_db
    reason = "OSError: C:\\secret\\data\\knowledge\\index.db"
    index_db.available = False
    index_db.unavailable_reason = reason
    try:
        service_api = await client.post(
            f"{V1}/{kb['id']}/retrieve", headers=headers, json={"query": "연차"}
        )
        console = await logged_in.post(f"{BASE}/{kb['id']}/retrieve", json={"query": "연차"})
    finally:
        index_db.available = True
        index_db.unavailable_reason = None

    assert service_api.status_code == 503
    error = service_api.json()["error"]
    assert error["code"] == "index_unavailable"
    assert error["details"] == {"retryable": True}
    assert "secret" not in service_api.text
    assert console.status_code == 503
    assert console.json()["error"]["details"]["reason"] == reason


async def test_a_rate_limited_search_says_when_to_retry(
    logged_in: AsyncClient, client: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    await seed(app, kb, "휴가 규정", "연차휴가는 입사 첫해에 15일이다")
    headers = await kb_key(logged_in, kb["id"])
    app.state.llm.embedder.push(fake_rate_limit_error("day", retry_after=120))

    response = await client.post(
        f"{V1}/{kb['id']}/retrieve",
        headers=headers,
        json={"query": "연차", "retrieval_model": {"search_method": "semantic_search"}},
    )

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "rate_limited_daily"
    assert int(response.headers["retry-after"]) >= 1


async def test_documents_are_replaced_by_text_and_file(
    logged_in: AsyncClient, client: AsyncClient
) -> None:
    kb = await create_kb(logged_in)
    other = await create_kb(logged_in, "남의 KB")
    headers = await kb_key(logged_in, kb["id"])
    created = await client.post(
        f"{V1}/{kb['id']}/document/create-by-text",
        headers=headers,
        json={"name": "규정", "text": "연차는 입사 첫해에 15일이다"},
    )
    memo = await client.post(
        f"{V1}/{kb['id']}/document/create-by-text",
        headers=headers,
        json={"name": "메모", "text": "회의록은 위키에 올린다"},
    )
    document = created.json()["document"]
    await wait_indexed(client, headers, kb["id"], created.json()["batch"])
    await wait_indexed(client, headers, kb["id"], memo.json()["batch"])
    url = f"{V1}/{kb['id']}/documents/{document['id']}"
    search = f"{V1}/{kb['id']}/retrieve"

    by_text = await client.post(
        f"{url}/update-by-text",
        headers=headers,
        json={"text": "병가는 60일까지 쓴다", "indexing_technique": "high_quality"},
    )
    await wait_indexed(client, headers, kb["id"], by_text.json()["batch"])
    new_text = await client.post(search, headers=headers, json={"query": "병가 며칠"})
    old_text = await client.post(
        search,
        headers=headers,
        json={"query": "입사 첫해", "retrieval_model": {"search_method": "full_text_search"}},
    )
    by_file = await client.post(
        f"{url}/update-by-file",
        headers=headers,
        data={"data": '{"name": "출장 규정"}'},
        files={"file": ("trip.txt", "출장비는 30일 안에 정산한다".encode(), "text/plain")},
    )
    await wait_indexed(client, headers, kb["id"], by_file.json()["batch"])
    duplicate = await client.post(
        f"{url}/update-by-text", headers=headers, json={"text": "회의록은 위키에 올린다"}
    )
    foreign = await logged_in.post(
        f"{BASE}/{other['id']}/documents", json={"name": "남의 문서", "text": "남의 글"}
    )
    foreign_id = foreign.json()["documents"][0]["id"]
    other_document = await client.post(
        f"{V1}/{kb['id']}/documents/{foreign_id}/update-by-text",
        headers=headers,
        json={"text": "끼어들기"},
    )
    other_kb = await client.post(
        f"{V1}/{other['id']}/documents/{foreign_id}/update-by-text",
        headers=headers,
        json={"text": "끼어들기"},
    )

    assert by_text.status_code == 200, by_text.text
    assert by_text.json()["document"]["id"] == document["id"]
    assert by_text.json()["document"]["name"] == "규정"
    assert by_text.json()["batch"] == created.json()["batch"]
    records = new_text.json()["records"]
    assert records[0]["segment"]["content"] == "병가는 60일까지 쓴다"
    assert records[0]["segment"]["document"]["id"] == document["id"]
    assert old_text.json()["records"] == []
    assert by_file.status_code == 200, by_file.text
    assert by_file.json()["document"]["name"] == "출장 규정"
    assert by_file.json()["document"]["filename"] == "trip.txt"
    assert by_file.json()["document"]["indexing_status"] == "waiting"
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "duplicate_document"
    assert other_document.status_code == 404
    assert other_kb.status_code == 404
