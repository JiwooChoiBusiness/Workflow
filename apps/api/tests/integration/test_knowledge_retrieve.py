"""검색 시험과 검색 서비스(3단계 설계 7장, M23). 가짜 임베더(LLM_FAKE)로 결정적으로 돈다.

문서는 지식 레인을 거치지 않고 app.db 행과 index.db 행을 직접 넣는다(색인 상태를 시험마다 골라
두려고). 레인이 집지 않도록 처음부터 completed 같은 상태로 넣는다.
"""

import csv
import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from sqlalchemy import text

from app.config import Settings
from app.db.engine import Database
from app.db.models import KbChildChunk, KbDocument, KbSegment
from app.db.repositories.knowledge import KbCounterRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.knowledge.tokens import estimate_tokens
from app.knowledge.units import FtsRow, UnitKind, VecRow, doc_id_of, fts_text
from app.llm.embeddings import EmbeddingConfig
from app.llm.fake_adapter import (
    FakeAdapter,
    FakeEmbedCall,
    fake_rate_limit_error,
    fake_server_error,
    fake_tool_call,
)
from app.schemas.knowledge import RetrievalSettings
from app.services.errors import ServiceError
from app.services.knowledge_runtime import KnowledgeRuntime
from app.services.knowledge_search import KnowledgeSearchService, RetrievalFailed

BASE = "/api/console/knowledge"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge"

# (부모 글, 자식 글들) 또는 글 하나.
Chunk = str | tuple[str, Sequence[str]]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    # 가짜 어댑터에는 분당 상한이 없다. 시험이 리미터의 20초 조각을 기다리지 않게 넉넉히 준다.
    return settings.model_copy(update={"llm_fake": True, "kb_embed_rpm": 6000})


@dataclass
class Seeder:
    app: FastAPI
    client: AsyncClient

    @property
    def db(self) -> Database:
        return self.app.state.db

    @property
    def runtime(self) -> KnowledgeRuntime:
        return self.app.state.knowledge

    async def workspace_id(self) -> uuid.UUID:
        async with self.db.read() as session:
            workspace = await WorkspaceRepository(session).get_default()
        assert workspace is not None
        return workspace.id

    async def kb(self, **body: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": "규정", "data_policy": {"acknowledged": True}}
        payload.update(body)
        response = await self.client.post(BASE, json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    async def document(
        self,
        kb: dict[str, Any],
        name: str,
        chunks: Sequence[Chunk],
        *,
        answers: Sequence[str] | None = None,
        vectors: bool = True,
        **columns: Any,
    ) -> uuid.UUID:
        """문서 하나와 청크(색인 행 포함)를 넣는다. vectors=False면 전문 색인만 만든다."""
        kb_id = uuid.UUID(kb["id"])
        workspace_id = await self.workspace_id()
        units: list[tuple[int, UnitKind, str, str]] = []
        async with self.db.write() as session:
            values: dict[str, Any] = {"indexing_status": "completed", **columns}
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
                **values,
            )
            session.add(document)
            await session.flush()
            counter = KbCounterRepository(session)
            for position, chunk in enumerate(chunks, start=1):
                content = chunk if isinstance(chunk, str) else chunk[0]
                answer = answers[position - 1] if answers else None
                parent = isinstance(chunk, tuple)
                unit_no = None if parent else next(iter(await counter.allocate(1)))
                segment = KbSegment(
                    workspace_id=workspace_id,
                    kb_id=kb_id,
                    document_id=document.id,
                    position=position,
                    unit_no=unit_no,
                    content=content,
                    answer=answer,
                    word_count=len(content),
                    tokens=len(content),
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    status="completed",
                    index_state={"fts": "ok", "vector": "ok"},
                )
                session.add(segment)
                await session.flush()
                if unit_no is not None:
                    units.append((unit_no, "segment", content, fts_text(content, answer)))
                    continue
                for child_position, child in enumerate(chunk[1], start=1):
                    child_no = next(iter(await counter.allocate(1)))
                    session.add(
                        KbChildChunk(
                            workspace_id=workspace_id,
                            kb_id=kb_id,
                            document_id=document.id,
                            segment_id=segment.id,
                            position=child_position,
                            unit_no=child_no,
                            content=child,
                            tokens=len(child),
                            content_hash=hashlib.sha256(child.encode()).hexdigest(),
                            index_state={"fts": "ok", "vector": "ok"},
                        )
                    )
                    units.append((child_no, "child", child, child))
            document_id = document.id
        await self.runtime.fts.ensure(kb_id, workspace_id=workspace_id)
        await self.runtime.fts.upsert(kb_id, [FtsRow(unit, body) for unit, _, _, body in units])
        if vectors and kb["indexing_technique"] == "high_quality":
            config = EmbeddingConfig.model_validate(kb["embedding"])
            await self.runtime.vectors.ensure(kb_id, config, workspace_id=workspace_id)
            embedded = await self.runtime.embeddings.embed_documents(
                [content for _, _, content, _ in units], config, priority="interactive"
            )
            rows = [
                VecRow(unit, vector, doc_id_of(document_id), kind)
                for (unit, kind, _, _), vector in zip(units, embedded.vectors, strict=True)
                if vector is not None
            ]
            await self.runtime.vectors.upsert(kb_id, rows)
        return document_id

    def service(self, llm: Any = None) -> KnowledgeSearchService:
        return KnowledgeSearchService(
            self.db, self.app.state.settings, self.runtime, llm=llm or self.app.state.llm
        )


@pytest.fixture
async def seeder(app: FastAPI, logged_in: AsyncClient) -> Seeder:
    return Seeder(app, logged_in)


def eval_docs() -> dict[str, str]:
    with (FIXTURES / "ko_eval_docs.csv").open(encoding="utf-8", newline="") as file:
        return {row["name"]: row["text"] for row in csv.DictReader(file)}


def eval_questions() -> list[tuple[str, str, str]]:
    with (FIXTURES / "ko_eval.csv").open(encoding="utf-8", newline="") as file:
        return [
            (row["question"], row["expected_document_name"], row["note"])
            for row in csv.DictReader(file)
        ]


async def seed_eval_kb(seeder: Seeder, **body: Any) -> dict[str, Any]:
    kb = await seeder.kb(**body)
    for name, paragraph in eval_docs().items():
        await seeder.document(kb, name, [paragraph])
    return kb


async def hit_test(client: AsyncClient, kb_id: str, query: str, **body: Any) -> Response:
    return await client.post(f"{BASE}/{kb_id}/retrieve", json={"query": query, **body})


def names(response: Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [record["segment"]["document"]["name"] for record in response.json()["records"]]


def warning_codes(payload: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in payload["warnings"]]


async def test_keyword_questions_come_first_on_the_synthetic_set(seeder: Seeder) -> None:
    # K3의 가짜 임베더 부분. 키워드 질문 12개가 전문 검색으로 1위이고 하이브리드 기본값도 같다.
    kb = await seed_eval_kb(seeder)
    keyword = [(q, doc) for q, doc, note in eval_questions() if note == "kw"]
    assert len(keyword) == 12

    for question, expected in keyword:
        full_text = await hit_test(
            seeder.client,
            kb["id"],
            question,
            retrieval_model={"search_method": "full_text_search"},
        )
        hybrid = await hit_test(seeder.client, kb["id"], question)
        assert names(full_text)[0] == expected, question
        assert names(hybrid)[0] == expected, question


async def test_hit_test_answers_in_the_dify_shape_with_fusion_details(seeder: Seeder) -> None:
    kb = await seed_eval_kb(seeder)

    response = await hit_test(seeder.client, kb["id"], "  ERR-4417 ")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["query"] == {"content": "ERR-4417"}
    assert len(body["records"]) == 4
    top = body["records"][0]
    assert top["segment"]["document"]["name"] == "G3"
    assert top["segment"]["document"]["indexing_status"] == "completed"
    assert set(top["segment"]) >= {
        "id",
        "position",
        "document_id",
        "content",
        "answer",
        "word_count",
        "tokens",
        "hit_count",
        "enabled",
        "status",
    }
    fusion = top["fusion"]
    assert fusion["vector_score"] is not None and fusion["keyword_score"] == 1.0
    assert 0 < fusion["cosine"] <= 1
    assert top["score"] == pytest.approx(
        0.7 * fusion["vector_score"] + 0.3 * fusion["keyword_score"]
    )
    scores = [record["score"] for record in body["records"]]
    assert scores == sorted(scores, reverse=True)
    assert body["warnings"] == []


async def test_search_settings_change_the_result(seeder: Seeder) -> None:
    kb = await seed_eval_kb(seeder)
    query = "병가 중에 월급은 얼마나 나와?"

    semantic = await hit_test(
        seeder.client,
        kb["id"],
        query,
        retrieval_model={"search_method": "semantic_search", "top_k": 2},
    )
    strict = await hit_test(
        seeder.client,
        kb["id"],
        query,
        retrieval_model={
            "search_method": "semantic_search",
            "score_threshold_enabled": True,
            "score_threshold": 0.99,
        },
    )
    too_many = await hit_test(seeder.client, kb["id"], query, retrieval_model={"top_k": 21})

    assert len(names(semantic)) == 2
    semantic_records = semantic.json()["records"]
    assert all(record["score"] == record["fusion"]["cosine"] for record in semantic_records)
    assert all(record["fusion"]["keyword_score"] is None for record in semantic_records)
    assert names(strict) == []
    assert too_many.status_code == 422
    assert too_many.json()["error"]["details"][0]["type"] == "top_k_too_large"


async def test_metadata_filter_narrows_the_documents(seeder: Seeder) -> None:
    kb = await seeder.kb()
    first = await seeder.document(kb, "2025년 출장 규정", ["출장비는 30일 안에 정산한다"])
    second = await seeder.document(kb, "2026년 출장 규정", ["출장비는 14일 안에 정산한다"])
    field = await seeder.client.post(
        f"{BASE}/{kb['id']}/metadata", json={"name": "year", "type": "number"}
    )
    assert field.status_code == 201, field.text
    for document_id, year in ((first, 2025), (second, 2026)):
        saved = await seeder.client.post(
            f"{BASE}/{kb['id']}/documents/metadata",
            json={
                "document_ids": [str(document_id)],
                "values": [{"field_id": field.json()["id"], "value": year}],
                "apply_to_all": True,
            },
        )
        assert saved.status_code == 200, saved.text
    query = "출장비 정산 기한"

    everything = await hit_test(seeder.client, kb["id"], query)
    newer = await hit_test(
        seeder.client,
        kb["id"],
        query,
        metadata_filter={
            "conditions": [{"name": "year", "comparison_operator": ">=", "value": 2026}]
        },
    )
    unknown = await hit_test(
        seeder.client,
        kb["id"],
        query,
        metadata_filter={
            "conditions": [{"name": "team", "comparison_operator": "is", "value": "a"}]
        },
    )
    wrong_operator = await hit_test(
        seeder.client,
        kb["id"],
        query,
        metadata_filter={
            "conditions": [{"name": "year", "comparison_operator": "contains", "value": "2"}]
        },
    )

    assert set(names(everything)) == {"2025년 출장 규정", "2026년 출장 규정"}
    assert names(newer) == ["2026년 출장 규정"]
    assert newer.json()["records"][0]["segment"]["document"]["doc_metadata"] == {"year": 2026.0}
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "metadata_field_not_found"
    assert wrong_operator.status_code == 422
    assert wrong_operator.json()["error"]["code"] == "metadata_condition_invalid"


async def test_unfinished_documents_are_found_by_full_text(seeder: Seeder) -> None:
    kb = await seeder.kb()
    await seeder.document(kb, "완료", ["연차는 입사 첫해에 15일이다"])
    await seeder.document(
        kb, "색인 중", ["연차 이월은 다음 해 3월까지다"], vectors=False, indexing_status="indexing"
    )

    found = await hit_test(seeder.client, kb["id"], "연차 이월")
    semantic = await hit_test(
        seeder.client, kb["id"], "연차 이월", retrieval_model={"search_method": "semantic_search"}
    )
    await seeder.client.patch(f"{BASE}/{kb['id']}", json={"search_while_indexing": False})
    hidden = await hit_test(seeder.client, kb["id"], "연차 이월")

    records = found.json()["records"]
    by_name = {record["segment"]["document"]["name"]: record for record in records}
    assert by_name["색인 중"]["segment"]["document"]["indexing_status"] == "indexing"
    assert by_name["색인 중"]["fusion"]["cosine"] is None
    assert warning_codes(found.json()) == ["partial_index"]
    assert "색인 중" not in names(semantic)
    assert names(hidden) == ["완료"]
    assert warning_codes(hidden.json()) == []


@pytest.mark.parametrize("doc_filter_limit", [1000, 0])
async def test_documents_and_chunks_that_are_off_stay_out(
    seeder: Seeder, db: Database, monkeypatch: pytest.MonkeyPatch, doc_filter_limit: int
) -> None:
    # 0이면 검색할 문서 목록을 읽지 않고(문서가 많은 KB) 유닛을 읽을 때 SQL 조건으로만 거른다.
    from app.services import knowledge_search

    monkeypatch.setattr(knowledge_search, "DOC_FILTER_LIMIT", doc_filter_limit)
    kb = await seeder.kb()
    keep = await seeder.document(kb, "켜짐", ["보안 점검은 분기마다 한다"])
    await seeder.document(kb, "꺼짐", ["보안 점검은 매달 한다"], enabled=False)
    await seeder.document(kb, "보관", ["보안 점검은 매주 한다"], archived=True)
    gone = await seeder.document(kb, "지움", ["보안 점검은 매일 한다"])
    chunk_off = await seeder.document(kb, "청크 끔", ["보안 점검 담당은 정보보호팀이다"])
    async with db.write() as session:
        await session.execute(
            text(
                "UPDATE kb_document SET deleted_at = '2026-11-01T00:00:00.000000Z' WHERE id = :id"
            ),
            {"id": str(gone)},
        )
        await session.execute(
            text("UPDATE kb_segment SET enabled = 0 WHERE document_id = :id"),
            {"id": str(chunk_off)},
        )

    found = await hit_test(seeder.client, kb["id"], "보안 점검", retrieval_model={"top_k": 10})

    assert names(found) == ["켜짐"]
    assert found.json()["records"][0]["segment"]["document_id"] == str(keep)


async def test_parent_child_results_carry_their_children(seeder: Seeder) -> None:
    kb = await seeder.kb(doc_form="hierarchical_model")
    await seeder.document(
        kb,
        "휴가 규정",
        [
            ("연차와 병가 규정 전체", ["연차는 15일이다", "병가는 60일까지 쓴다"]),
            ("경조사 휴가 규정 전체", ["결혼하면 5일을 쉰다"]),
        ],
    )

    found = await hit_test(seeder.client, kb["id"], "병가는 며칠까지 쓸 수 있어?")

    records = found.json()["records"]
    assert records[0]["segment"]["content"] == "연차와 병가 규정 전체"
    children = records[0]["child_chunks"]
    assert children[0]["content"] == "병가는 60일까지 쓴다"
    assert records[0]["score"] == max(child["score"] for child in children)
    assert len({record["segment"]["id"] for record in records}) == len(records)


async def test_qa_answers_and_economy_full_text(seeder: Seeder) -> None:
    qa = await seeder.kb(name="질문 답", doc_form="qa_model")
    await seeder.document(
        qa,
        "FAQ",
        ["비밀번호를 잊었어요", "VPN이 안 붙어요"],
        answers=["포털에서 재설정한다", "재부팅한다"],
    )
    economy = await seeder.kb(name="절약", indexing_technique="economy")
    await seeder.document(economy, "메모", ["회의록은 위키에 올린다"])
    calls_before = len(seeder.app.state.llm.embedder.calls)

    answered = await hit_test(seeder.client, qa["id"], "비밀번호 재설정")
    cheap = await hit_test(seeder.client, economy["id"], "회의록 위치")

    assert answered.json()["records"][0]["segment"]["answer"] == "포털에서 재설정한다"
    assert names(cheap) == ["메모"]
    assert cheap.json()["records"][0]["fusion"]["cosine"] is None
    # economy KB는 질의 임베딩도 부르지 않는다. 앞의 Q&A 질의 하나만 불렸다.
    assert len(seeder.app.state.llm.embedder.calls) == calls_before + 1


async def test_query_embedding_failure_falls_back_or_fails(seeder: Seeder) -> None:
    kb = await seed_eval_kb(seeder)
    embedder = seeder.app.state.llm.embedder
    embedder.push(fake_rate_limit_error("day", retry_after=3600))

    hybrid = await hit_test(seeder.client, kb["id"], "ERR-4417")
    semantic = await hit_test(
        seeder.client,
        kb["id"],
        "도쿄 출장 일비",
        retrieval_model={"search_method": "semantic_search"},
    )

    assert names(hybrid)[0] == "G3"
    warning = hybrid.json()["warnings"][0]
    assert warning["code"] == "vector_unavailable"
    assert warning["retry_after"] is not None
    assert hybrid.json()["records"][0]["fusion"]["cosine"] is None
    # 일일 한도는 그 모델을 막아 두므로 다음 질의도 임베딩을 하지 못한다.
    assert semantic.status_code == 429, semantic.text
    assert semantic.json()["error"]["code"] == "rate_limited_daily"
    assert semantic.json()["error"]["details"]["retryable"] is True
    assert int(semantic.headers["retry-after"]) >= 1


async def test_a_kb_being_reembedded_searches_full_text(seeder: Seeder, db: Database) -> None:
    kb = await seeder.kb()
    await seeder.document(kb, "출장", ["도쿄 출장 일비는 하루 8만 원이다"])
    async with db.write() as session:
        await session.execute(
            text(
                "UPDATE knowledge_base SET embedding = json_set(embedding, '$.version', 2) "
                "WHERE id = :id"
            ),
            {"id": kb["id"]},
        )

    found = await hit_test(
        seeder.client,
        kb["id"],
        "도쿄 출장 일비",
        retrieval_model={"search_method": "semantic_search"},
    )

    assert names(found) == ["출장"]
    assert found.json()["records"][0]["fusion"]["cosine"] is None
    assert warning_codes(found.json()) == ["partial_index"]


async def test_two_kbs_merge_by_score_or_by_rank(seeder: Seeder) -> None:
    first = await seeder.kb(name="인사")
    second = await seeder.kb(name="재무")
    other = await seeder.kb(name="다른 임베딩", embedding={"dimensions": 1536})
    await seeder.document(first, "연차", ["연차는 입사 첫해에 15일이다"])
    await seeder.document(second, "정산", ["출장비 정산은 30일 안에 한다"])
    await seeder.document(other, "복지", ["연차 수당은 1월에 지급한다"])
    service = seeder.service()
    workspace_id = await seeder.workspace_id()
    settings = RetrievalSettings(top_k=4)
    ids = [uuid.UUID(kb["id"]) for kb in (first, second)]

    same = await service.retrieve(workspace_id, ids, "연차는 며칠", settings, source="node")
    mixed = await service.retrieve(
        workspace_id, [*ids, uuid.UUID(other["id"])], "연차는 며칠", settings, source="node"
    )
    with pytest.raises(ServiceError) as missing_settings:
        await service.retrieve(workspace_id, ids, "연차", source="node")

    assert [record.document.name for record in same.records][0] == "연차"
    scores = [record.score for record in same.records]
    assert scores == sorted(scores, reverse=True)
    assert all(record.ranked.fusion_score is None for record in same.records)
    assert same.warnings == []
    assert [warning.code for warning in mixed.warnings] == ["mixed_kb_rrf"]
    assert {record.kb.name for record in mixed.records} == {"인사", "재무", "다른 임베딩"}
    assert all(record.ranked.fusion_score is not None for record in mixed.records)
    assert missing_settings.value.code == "retrieval_settings_required"
    resources = same.resources()
    assert [resource.position for resource in resources] == list(range(1, len(resources) + 1))
    assert resources[0].dataset_name == "인사" and resources[0].document_name == "연차"


async def test_hit_counts_and_query_logs(seeder: Seeder, app: FastAPI) -> None:
    kb = await seeder.kb()
    document_id = await seeder.document(kb, "연차", ["연차는 입사 첫해에 15일이다"])
    workspace_id = await seeder.workspace_id()

    await hit_test(seeder.client, kb["id"], "연차")
    node = await seeder.service().retrieve(
        workspace_id, [uuid.UUID(kb["id"])], "연차 일수", source="node"
    )
    logs = await seeder.client.get(f"{BASE}/{kb['id']}/query-logs")

    assert node.records[0].segment.hit_count == 0
    async with seeder.db.read() as session:
        counts = (
            await session.execute(
                text(
                    "SELECT d.hit_count, s.hit_count FROM kb_document d "
                    "JOIN kb_segment s ON s.document_id = d.id WHERE d.id = :id"
                ),
                {"id": str(document_id)},
            )
        ).one()
    assert tuple(counts) == (1, 1)
    assert logs.status_code == 200, logs.text
    items = logs.json()["items"]
    assert [item["source"] for item in items] == ["node", "hit_test"]
    assert items[1]["query"] == "연차"
    assert items[1]["results"][0]["segment_id"] == str(node.records[0].segment.id)
    assert items[1]["settings"]["retrieval_model"]["top_k"] == 4
    assert logs.json()["total"] == 2


async def test_query_logs_can_be_turned_off(seeder: Seeder, app: FastAPI) -> None:
    app.state.settings = app.state.settings.model_copy(update={"kb_query_log_days": 0})
    kb = await seeder.kb()
    await seeder.document(kb, "연차", ["연차는 입사 첫해에 15일이다"])

    await hit_test(seeder.client, kb["id"], "연차")
    logs = await seeder.client.get(f"{BASE}/{kb['id']}/query-logs")

    assert logs.json()["total"] == 0


async def test_llm_rerank_reorders_and_falls_back(seeder: Seeder, app: FastAPI) -> None:
    kb = await seeder.kb()
    for name, body in [("가", "연차 규정 하나"), ("나", "연차 규정 둘"), ("다", "연차 규정 셋")]:
        await seeder.document(kb, name, [body])
    settings = {"reranking_mode": "llm", "top_k": 3}
    baseline = names(await hit_test(seeder.client, kb["id"], "연차 규정"))

    def reverse(request: Any) -> Any:
        return fake_tool_call(
            "rank_passages",
            {"ranking": [{"index": 1, "relevance": 0.1}, {"index": 3, "relevance": 0.9}]},
        )

    app.state.llm = FakeAdapter([reverse, []])
    reranked = await hit_test(seeder.client, kb["id"], "연차 규정", retrieval_model=settings)
    failed = await hit_test(seeder.client, kb["id"], "연차 규정", retrieval_model=settings)

    assert names(reranked) == [baseline[2], baseline[0], baseline[1]]
    records = reranked.json()["records"]
    assert [record["score"] for record in records] == [0.9, 0.1, 0.0]
    assert all(record["fusion_score"] is not None for record in records)
    assert names(failed) == baseline
    assert warning_codes(failed.json()) == ["rerank_failed"]


async def test_auto_metadata_filter_is_built_by_the_model(seeder: Seeder, app: FastAPI) -> None:
    kb = await seeder.kb()
    first = await seeder.document(kb, "영업팀 규정", ["출장비 정산은 30일 안에 한다"])
    second = await seeder.document(kb, "개발팀 규정", ["출장비 정산은 14일 안에 한다"])
    field = await seeder.client.post(
        f"{BASE}/{kb['id']}/metadata", json={"name": "team", "type": "string"}
    )
    for document_id, team in ((first, "sales"), (second, "dev")):
        await seeder.client.post(
            f"{BASE}/{kb['id']}/documents/metadata",
            json={
                "document_ids": [str(document_id)],
                "values": [{"field_id": field.json()["id"], "value": team}],
                "apply_to_all": True,
            },
        )
    automatic = {"mode": "automatic", "model": {"model_id": "gemini-3.6-flash"}}
    app.state.llm = FakeAdapter(
        [
            fake_tool_call(
                "build_metadata_filter",
                {"conditions": [{"name": "team", "comparison_operator": "is", "value": "dev"}]},
            ),
            fake_tool_call(
                "build_metadata_filter",
                {"conditions": [{"name": "owner", "comparison_operator": "is", "value": "x"}]},
            ),
        ]
    )

    filtered = await hit_test(
        seeder.client, kb["id"], "개발팀 출장비 정산", metadata_filter=automatic
    )
    ignored = await hit_test(
        seeder.client, kb["id"], "개발팀 출장비 정산", metadata_filter=automatic
    )

    assert names(filtered) == ["개발팀 규정"]
    assert app.state.llm.requests[0].model == "gemini-3.6-flash"
    assert set(names(ignored)) == {"영업팀 규정", "개발팀 규정"}
    assert warning_codes(ignored.json()) == ["filter_ignored"]


async def test_unknown_and_unavailable(seeder: Seeder, app: FastAPI) -> None:
    missing = await hit_test(seeder.client, str(uuid.uuid4()), "연차")
    kb = await seeder.kb()
    empty = await hit_test(seeder.client, kb["id"], "연차")
    runtime = app.state.knowledge
    app.state.knowledge = None
    try:
        unavailable = await hit_test(seeder.client, kb["id"], "연차")
        logs = await seeder.client.get(f"{BASE}/{kb['id']}/query-logs")
    finally:
        app.state.knowledge = runtime

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "knowledge_base_not_found"
    assert names(empty) == []
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "index_unavailable"
    assert logs.status_code == 200


async def test_a_search_failure_is_a_retrieval_error(seeder: Seeder) -> None:
    kb = await seed_eval_kb(seeder)
    seeder.app.state.llm.embedder.push(fake_rate_limit_error("day"))
    workspace_id = await seeder.workspace_id()

    with pytest.raises(RetrievalFailed) as caught:
        await seeder.service().retrieve(
            workspace_id,
            [uuid.UUID(kb["id"])],
            "연차",
            RetrievalSettings(search_method="semantic_search"),
            source="tool",
        )

    assert caught.value.retryable and caught.value.retry_after


@pytest.mark.parametrize(
    "query", ["연차 AND NOT 병가", "연차*", '"연차" OR', "NEAR(연차 병가)", "^연차"]
)
async def test_query_operators_are_searched_as_text(seeder: Seeder, query: str) -> None:
    # KS6: FTS5 연산자처럼 보이는 글자도 오류 없이 글자로 찾는다(5.3, 14.3).
    kb = await seeder.kb()
    await seeder.document(kb, "휴가", ["연차는 15일이고 병가는 60일이다"])

    for method in ("full_text_search", "hybrid_search"):
        found = await hit_test(
            seeder.client, kb["id"], query, retrieval_model={"search_method": method}
        )
        assert names(found) == ["휴가"], (method, found.text)


def warning_names(result: Any) -> list[str]:
    return [warning.code for warning in result.warnings]


async def test_paid_key_missing_fails_even_for_hybrid(seeder: Seeder, db: Database) -> None:
    # 유료 키가 없으면 기다려도 그대로라 하이브리드도 전문 검색으로 낮추지 않고 실패한다.
    kb = await seeder.kb()
    await seeder.document(kb, "연차", ["연차는 입사 첫해에 15일이다"])
    async with db.write() as session:
        await session.execute(
            text(
                "UPDATE knowledge_base SET data_policy = "
                "json_set(data_policy, '$.key_tier', 'paid') WHERE id = :id"
            ),
            {"id": kb["id"]},
        )

    hybrid = await hit_test(seeder.client, kb["id"], "연차")
    full_text = await hit_test(
        seeder.client, kb["id"], "연차", retrieval_model={"search_method": "full_text_search"}
    )

    assert hybrid.status_code == 422, hybrid.text
    assert hybrid.json()["error"]["code"] == "paid_key_missing"
    assert hybrid.json()["error"]["details"]["retryable"] is False
    assert names(full_text) == ["연차"]


async def test_kb_count_duplicates_and_query_length(seeder: Seeder) -> None:
    kbs = [await seeder.kb(name=f"KB {index}") for index in range(6)]
    await seeder.document(kbs[0], "연차", ["연차는 입사 첫해에 15일이다"])
    service = seeder.service()
    workspace_id = await seeder.workspace_id()
    ids = [uuid.UUID(kb["id"]) for kb in kbs]

    with pytest.raises(ServiceError) as too_many:
        await service.retrieve(workspace_id, ids, "연차", RetrievalSettings(), source="node")
    twice = await service.retrieve(workspace_id, [ids[0], ids[0]], "연차", source="node")
    long_query = await hit_test(seeder.client, kbs[0]["id"], "가" * 251)

    assert too_many.value.status == 422 and too_many.value.code == "too_many_knowledge_bases"
    assert [record.document.name for record in twice.records] == ["연차"]
    assert long_query.status_code == 422
    assert long_query.json()["error"]["details"][0]["loc"] == ["body", "query"]


async def test_error_and_paused_documents_do_not_mark_partial_index(seeder: Seeder) -> None:
    kb = await seeder.kb()
    await seeder.document(kb, "완료", ["연차는 입사 첫해에 15일이다"])
    await seeder.document(
        kb, "실패", ["연차 이월은 3월까지다"], vectors=False, indexing_status="error"
    )
    await seeder.document(
        kb, "멈춤", ["연차 신청은 사흘 전에 한다"], vectors=False, indexing_status="paused"
    )

    found = await hit_test(seeder.client, kb["id"], "연차")

    assert set(names(found)) == {"완료", "실패", "멈춤"}
    assert warning_codes(found.json()) == []


async def test_one_kb_falling_back_to_full_text_mixes_by_rank(seeder: Seeder) -> None:
    first = await seeder.kb(name="인사")
    second = await seeder.kb(name="복지", embedding={"dimensions": 1536})
    await seeder.document(first, "연차", ["연차는 입사 첫해에 15일이다"])
    await seeder.document(second, "수당", ["연차 수당은 1월에 지급한다"])

    def fail_1536(call: FakeEmbedCall) -> Any:
        return fake_server_error() if call.dimensions == 1536 else None

    # 768 질의 하나와 1536 질의의 첫 시도·같은 키 재시도가 각본을 하나씩 쓴다.
    seeder.app.state.llm.embedder.push(fail_1536, fail_1536, fail_1536)
    result = await seeder.service().retrieve(
        await seeder.workspace_id(),
        [uuid.UUID(first["id"]), uuid.UUID(second["id"])],
        "연차",
        RetrievalSettings(),
        source="node",
    )

    assert sorted(warning_names(result)) == ["mixed_kb_rrf", "vector_unavailable"]
    unavailable = next(w for w in result.warnings if w.code == "vector_unavailable")
    assert unavailable.kb_ids == [uuid.UUID(second["id"])]
    assert {record.kb.name for record in result.records} == {"인사", "복지"}


async def test_rrf_mode_explains_the_rank_merge(seeder: Seeder) -> None:
    first = await seeder.kb(name="인사")
    second = await seeder.kb(name="재무")
    await seeder.document(first, "연차", ["연차는 입사 첫해에 15일이다"])
    await seeder.document(second, "정산", ["연차 수당 정산은 1월에 한다"])

    result = await seeder.service().retrieve(
        await seeder.workspace_id(),
        [uuid.UUID(first["id"]), uuid.UUID(second["id"])],
        "연차",
        RetrievalSettings(reranking_mode="rrf"),
        source="node",
    )

    assert warning_names(result) == ["mixed_kb_rrf"]
    assert "rrf" in result.warnings[0].message


@pytest.mark.parametrize("method", ["semantic_search", "full_text_search", "hybrid_search"])
async def test_parent_child_candidates_widen_until_enough_parents(
    seeder: Seeder, method: str
) -> None:
    # full_doc 부모 하나에 자식이 40개면 유닛 50개 깊이로는 부모가 둘만 잡힌다.
    kb = await seeder.kb(
        doc_form="hierarchical_model", process_rule={"parent": {"mode": "full_doc"}}
    )
    themes = {
        "휴가": "연차 휴가 규정 {}조: 연차 휴가는 해마다 15일이다",
        "병가": "연차 규정 {}조: 병가는 60일까지다",
        "출장": "출장 규정 {}조: 출장비는 30일 안에 정산한다",
    }
    for name, template in themes.items():
        children = [template.format(index) for index in range(40)]
        await seeder.document(kb, name, [(f"{name} 규정 전체", children)])

    found = await hit_test(
        seeder.client,
        kb["id"],
        "연차 휴가 규정",
        retrieval_model={"search_method": method, "top_k": 4},
    )

    assert sorted(names(found)) == ["병가", "출장", "휴가"]


async def test_a_long_query_is_cut_to_the_embedding_input_limit(seeder: Seeder) -> None:
    kb = await seeder.kb()
    await seeder.document(kb, "연차", ["연차는 입사 첫해에 15일이다"])
    embedder = seeder.app.state.llm.embedder
    query = "연차 " * 7000

    result = await seeder.service().retrieve(
        await seeder.workspace_id(), [uuid.UUID(kb["id"])], query, source="node"
    )

    assert warning_names(result) == ["query_truncated"]
    assert len(result.query) < len(query.strip())
    sent = embedder.calls[-1].inputs[0]
    assert estimate_tokens(sent) <= EmbeddingConfig().input_token_limit
    assert [record.document.name for record in result.records] == ["연차"]


async def test_an_unavailable_index_fails_before_the_query_embedding(seeder: Seeder) -> None:
    kb = await seeder.kb()
    await seeder.document(kb, "연차", ["연차는 입사 첫해에 15일이다"])
    runtime = seeder.runtime
    calls = len(seeder.app.state.llm.embedder.calls)
    workspace_id = await seeder.workspace_id()
    runtime.index_db.available = False
    try:
        with pytest.raises(RetrievalFailed) as caught:
            await seeder.service().retrieve(
                workspace_id, [uuid.UUID(kb["id"])], "연차", source="tool"
            )
        missing = KnowledgeSearchService(seeder.db, seeder.app.state.settings, None)
        with pytest.raises(RetrievalFailed) as no_runtime:
            await missing.retrieve(workspace_id, [uuid.UUID(kb["id"])], "연차", source="tool")
    finally:
        runtime.index_db.available = True

    assert (caught.value.status, caught.value.code) == (503, "index_unavailable")
    assert caught.value.retryable is True
    assert no_runtime.value.retryable is True
    assert len(seeder.app.state.llm.embedder.calls) == calls
