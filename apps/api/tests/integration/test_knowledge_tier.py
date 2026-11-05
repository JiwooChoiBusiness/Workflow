"""검색의 키 등급 전파(3단계 설계 14.4). 유료 KB를 검색하면 질의 임베딩과 채팅 호출(자동 필터·
LLM 재순위)이 유료 키로 간다. 가짜 실행 객체는 유료 키를 따로 갖고 있어 어느 키로 불렸는지 보인다.
"""

import hashlib
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import update

from app.config import Settings
from app.db.models import KbDocument, KbSegment
from app.db.repositories.knowledge import KbCounterRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.knowledge.units import FtsRow, VecRow, doc_id_of
from app.llm.embeddings import EmbeddingConfig
from app.llm.fake_adapter import FakeAdapter, FakeEmbedder, fake_tool_call
from app.schemas.knowledge import RetrievalSettings
from app.schemas.retrieval import MetadataFilter
from app.services.knowledge_runtime import _FAKE_KEY, _FAKE_PAID_KEY, KnowledgeRuntime
from app.services.knowledge_search import KnowledgeSearchService

BASE = "/api/console/knowledge"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "llm_fake": True,
            "kb_embed_rpm": 6000,
            "gemini_paid_api_key": SecretStr("paid-" + "7" * 30),
        }
    )


async def make_kb(client: AsyncClient, name: str, tier: str) -> dict[str, Any]:
    response = await client.post(
        BASE,
        json={"name": name, "data_policy": {"acknowledged": True, "key_tier": tier}},
    )
    assert response.status_code == 201, response.text
    return response.json()


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
                content=text,
                word_count=len(text),
                tokens=len(text),
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                status="completed",
                index_state={"fts": "pending", "vector": "pending"},
            )
        )
        document_id = document.id
    config = EmbeddingConfig.model_validate(kb["embedding"])
    await runtime.fts.ensure(kb_id, workspace_id=workspace_id)
    await runtime.fts.upsert(kb_id, [FtsRow(unit_no, text)])
    await runtime.vectors.ensure(kb_id, config, workspace_id=workspace_id)
    vector = await runtime.embeddings.embed_single(text, config)
    await runtime.vectors.upsert(kb_id, [VecRow(unit_no, vector, doc_id_of(document_id))])
    # 레인 작업처럼 색인 행을 다 쓴 뒤에 ok와 completed를 적는다(이유는 test_knowledge_retrieve.py의
    # Seeder.document).
    async with app.state.db.write() as session:
        await session.execute(
            update(KbSegment)
            .where(KbSegment.document_id == document_id)
            .values(index_state={"fts": "ok", "vector": "ok"})
        )
        await session.execute(
            update(KbDocument)
            .where(KbDocument.id == document_id)
            .values(indexing_status="completed")
        )
    return document_id


def query_keys(embedder: FakeEmbedder, start: int) -> list[str]:
    # 질의 임베딩은 질의 접두어가 붙어 문서 임베딩과 구별된다.
    return [
        call.api_key
        for call in embedder.calls[start:]
        if call.inputs and call.inputs[0].startswith("task: search result")
    ]


async def test_a_paid_kb_sends_every_call_with_the_paid_key(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    paid = await make_kb(logged_in, "급여", "paid")
    await seed(app, paid, "급여 규정", "급여는 매달 25일에 준다")
    field = await logged_in.post(
        f"{BASE}/{paid['id']}/metadata", json={"name": "team", "type": "string"}
    )
    assert field.status_code == 201, field.text
    embedder: FakeEmbedder = app.state.llm.embedder
    start = len(embedder.calls)
    chat = FakeAdapter(
        [
            fake_tool_call("build_metadata_filter", {"conditions": []}),
            fake_tool_call("rank_passages", {"ranking": [{"index": 1, "relevance": 0.9}]}),
        ]
    )
    service = KnowledgeSearchService(
        app.state.db, app.state.settings, app.state.knowledge, llm=chat
    )
    async with app.state.db.read() as session:
        workspace = await WorkspaceRepository(session).get_default()
    assert workspace is not None

    result = await service.retrieve(
        workspace.id,
        [uuid.UUID(paid["id"])],
        "급여일",
        RetrievalSettings(reranking_mode="llm"),
        metadata_filter=MetadataFilter(mode="automatic"),
        source="node",
        tier="free",
    )

    assert [record.document.name for record in result.records] == ["급여 규정"]
    assert query_keys(embedder, start) == [_FAKE_PAID_KEY]
    assert chat.tiers == ["paid", "paid"]
    assert [call.purpose for call in result.model_calls] == ["metadata_filter", "rerank"]


async def test_a_paid_run_upgrades_a_free_kb_and_a_free_run_stays_free(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    free = await make_kb(logged_in, "공지", "free")
    await seed(app, free, "공지", "휴무일은 매달 첫째 월요일이다")
    embedder: FakeEmbedder = app.state.llm.embedder
    chat = FakeAdapter(
        [
            fake_tool_call("rank_passages", {"ranking": [{"index": 1, "relevance": 0.5}]}),
            fake_tool_call("rank_passages", {"ranking": [{"index": 1, "relevance": 0.5}]}),
        ]
    )
    service = KnowledgeSearchService(
        app.state.db, app.state.settings, app.state.knowledge, llm=chat
    )
    async with app.state.db.read() as session:
        workspace = await WorkspaceRepository(session).get_default()
    assert workspace is not None
    settings = RetrievalSettings(reranking_mode="llm")
    kb_ids = [uuid.UUID(free["id"])]

    start = len(embedder.calls)
    await service.retrieve(workspace.id, kb_ids, "휴무일", settings, source="node", tier="paid")
    paid_keys = query_keys(embedder, start)
    start = len(embedder.calls)
    await service.retrieve(workspace.id, kb_ids, "첫째 월요일", settings, source="node")
    free_keys = query_keys(embedder, start)

    assert paid_keys == [_FAKE_PAID_KEY]
    assert free_keys == [_FAKE_KEY]
    assert chat.tiers == ["paid", "free"]
