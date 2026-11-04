"""평가셋과 평가 실행(3단계 설계 7.7, M23). 합성 평가셋(문단 48, 질문 53)을 가짜 임베더로 돌린다.

문단은 지식 레인을 거치지 않고 문서·청크 행과 index.db 행으로 바로 넣는다.
"""

import csv
import hashlib
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.config import Settings
from app.db.models import KbDocument, KbSegment
from app.db.repositories.knowledge import KbCounterRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.knowledge.units import FtsRow, VecRow, doc_id_of
from app.llm.embeddings import EmbeddingConfig
from app.llm.fake_adapter import FakeAdapter
from app.services.knowledge_eval import RERANK_EVAL_MAX_CASES
from app.services.knowledge_runtime import KnowledgeRuntime

BASE = "/api/console/knowledge"
FETCH = {"X-Requested-With": "fetch"}
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"llm_fake": True, "kb_embed_rpm": 6000})


async def create_kb(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(BASE, json={"name": "평가", "data_policy": {"acknowledged": True}})
    assert response.status_code == 201, response.text
    return response.json()


async def seed(app: FastAPI, kb: dict[str, Any], paragraphs: dict[str, str]) -> dict[str, str]:
    """문단마다 문서 하나·청크 하나. 문서 이름 → 청크 id."""
    db = app.state.db
    runtime: KnowledgeRuntime = app.state.knowledge
    kb_id = uuid.UUID(kb["id"])
    units: list[tuple[int, uuid.UUID, str]] = []
    segments: dict[str, str] = {}
    async with db.write() as session:
        workspace = await WorkspaceRepository(session).get_default()
        assert workspace is not None
        workspace_id = workspace.id
        counter = KbCounterRepository(session)
        for position, (name, paragraph) in enumerate(paragraphs.items(), start=1):
            document = KbDocument(
                workspace_id=workspace_id,
                kb_id=kb_id,
                position=position,
                name=name,
                data_source_type="paste_text",
                filename=f"{name}.txt",
                mime="text/plain",
                size_bytes=len(paragraph.encode()),
                sha256=hashlib.sha256(paragraph.encode()).hexdigest(),
                storage_key=f"seed/{uuid.uuid4().hex}",
                batch="seed",
                indexing_status="completed",
            )
            session.add(document)
            await session.flush()
            unit_no = next(iter(await counter.allocate(1)))
            segment = KbSegment(
                workspace_id=workspace_id,
                kb_id=kb_id,
                document_id=document.id,
                position=1,
                unit_no=unit_no,
                content=paragraph,
                word_count=len(paragraph),
                tokens=len(paragraph),
                content_hash=hashlib.sha256(paragraph.encode()).hexdigest(),
                status="completed",
                index_state={"fts": "ok", "vector": "ok"},
            )
            session.add(segment)
            await session.flush()
            units.append((unit_no, document.id, paragraph))
            segments[name] = str(segment.id)
    config = EmbeddingConfig.model_validate(kb["embedding"])
    await runtime.fts.ensure(kb_id, workspace_id=workspace_id)
    await runtime.fts.upsert(kb_id, [FtsRow(unit, body) for unit, _, body in units])
    await runtime.vectors.ensure(kb_id, config, workspace_id=workspace_id)
    embedded = await runtime.embeddings.embed_documents(
        [body for _, _, body in units], config, priority="interactive"
    )
    await runtime.vectors.upsert(
        kb_id,
        [
            VecRow(unit, vector, doc_id_of(document_id))
            for (unit, document_id, _), vector in zip(units, embedded.vectors, strict=True)
            if vector is not None
        ],
    )
    return segments


def synthetic_docs() -> dict[str, str]:
    with (FIXTURES / "ko_eval_docs.csv").open(encoding="utf-8", newline="") as file:
        return {row["name"]: row["text"] for row in csv.DictReader(file)}


async def test_the_synthetic_set_imports_runs_and_is_saved(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    await seed(app, kb, synthetic_docs())
    csv_text = (FIXTURES / "ko_eval.csv").read_text(encoding="utf-8")

    imported = await logged_in.post(f"{BASE}/{kb['id']}/eval/cases/import", json={"csv": csv_text})
    run = await logged_in.post(f"{BASE}/{kb['id']}/eval/run", json={})

    assert imported.status_code == 200, imported.text
    assert imported.json() == {"created": 53, "problems": [], "total": 53}
    assert run.status_code == 200, run.text
    body = run.json()
    metrics = body["metrics"]
    assert metrics["cases"] == 53
    # 가짜 임베더(bigram 해싱)의 하이브리드 기본값은 R@1 0.868, R@3 0.943이다(벡터만이면 0.792,
    # 0.906). 실제 키 수치(R@1 0.98 이상)는 live 시험이 본다.
    assert metrics["r1"] >= 0.85 and metrics["r3"] >= 0.9
    assert metrics["r1"] <= metrics["r3"] <= metrics["r5"]
    assert 0 < metrics["mrr10"] <= 1
    assert body["settings"]["top_k"] == 4
    keyword = {
        row["question"] for row in csv.DictReader(csv_text.splitlines()) if row["note"] == "kw"
    }
    ranks = {case["question"]: case["rank"] for case in body["cases"]}
    assert all(ranks[question] == 1 for question in keyword)
    assert all(len(case["top"]) == 5 for case in body["cases"])
    assert body["id"] is not None
    listed = await logged_in.get(f"{BASE}/{kb['id']}/eval/runs")
    fetched = await logged_in.get(f"{BASE}/{kb['id']}/eval/runs/{body['id']}")
    logs = await logged_in.get(f"{BASE}/{kb['id']}/query-logs", params={"limit": 1})
    assert [item["id"] for item in listed.json()["items"]] == [body["id"]]
    assert fetched.json()["metrics"] == metrics
    assert fetched.json()["cases"] == body["cases"]
    assert logs.json()["total"] == 53
    assert logs.json()["items"][0]["source"] == "eval"


async def test_changing_settings_reuses_the_query_embeddings(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    docs = synthetic_docs()
    await seed(app, kb, {name: docs[name] for name in ("A1", "A2", "B2", "G6")})
    lines = [
        "question,expected_document_name,note",
        "입사 첫해에 연차가 며칠이야?,A1,sem",
        "ERR-4417,G6,kw",
    ]
    await logged_in.post(f"{BASE}/{kb['id']}/eval/cases/import", json={"csv": "\n".join(lines)})
    embedder = app.state.llm.embedder

    first = await logged_in.post(f"{BASE}/{kb['id']}/eval/run", json={"save": False})
    calls = len(embedder.calls)
    second = await logged_in.post(
        f"{BASE}/{kb['id']}/eval/run",
        json={
            "save": False,
            "retrieval_model": {"weights": {"vector_weight": 0.8, "keyword_weight": 0.2}},
        },
    )
    rrf = await logged_in.post(
        f"{BASE}/{kb['id']}/eval/run",
        json={"save": False, "retrieval_model": {"reranking_mode": "rrf"}},
    )

    assert first.status_code == second.status_code == rrf.status_code == 200
    assert len(embedder.calls) == calls
    assert first.json()["id"] is None
    assert second.json()["settings"]["weights"]["vector_weight"] == 0.8
    assert rrf.json()["metrics"]["cases"] == 2
    assert (await logged_in.get(f"{BASE}/{kb['id']}/eval/runs")).json()["items"] == []


async def test_import_reports_bad_lines_and_can_replace(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    segments = await seed(app, kb, {"규정": "연차는 15일이다", "메모": "회의는 화요일이다"})
    rows = [
        "question,expected_document_name,expected_segment_id,note",
        "연차는?,규정,,",
        "회의는?,,{seg},".format(seg=segments["메모"]),
        "없는 문서,없음,,",
        "깨진 id,,not-a-uuid,",
        "남의 청크,," + str(uuid.uuid4()) + ",",
    ]

    first = await logged_in.post(
        f"{BASE}/{kb['id']}/eval/cases/import", json={"csv": "\n".join(rows)}
    )
    replaced = await logged_in.post(
        f"{BASE}/{kb['id']}/eval/cases/import",
        params={"replace": "true"},
        files={"file": ("cases.csv", "question,expected_document_name\n연차?,규정\n".encode())},
        headers=FETCH,
    )
    cases = await logged_in.get(f"{BASE}/{kb['id']}/eval/cases")
    wrong = await logged_in.post(f"{BASE}/{kb['id']}/eval/cases/import", json={"csv": "q,a\n1,2\n"})

    assert first.status_code == 200, first.text
    assert first.json()["created"] == 2
    assert [problem["line"] for problem in first.json()["problems"]] == [4, 5, 6]
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["created"] == 1 and replaced.json()["total"] == 1
    assert [case["question"] for case in cases.json()["items"]] == ["연차?"]
    assert wrong.json()["created"] == 0
    assert wrong.json()["problems"][0]["line"] == 1


async def test_cases_are_managed_one_by_one(logged_in: AsyncClient, app: FastAPI) -> None:
    kb = await create_kb(logged_in)
    other = await create_kb(logged_in)
    segments = await seed(app, kb, {"규정": "연차는 15일이다"})
    await seed(app, other, {"남의 문서": "보안 규정"})
    documents = await logged_in.get(f"{BASE}/{kb['id']}/documents")
    document_id = documents.json()["items"][0]["id"]
    foreign = (await logged_in.get(f"{BASE}/{other['id']}/documents")).json()["items"][0]["id"]
    url = f"{BASE}/{kb['id']}/eval/cases"

    empty_run = await logged_in.post(f"{BASE}/{kb['id']}/eval/run", json={})
    created = await logged_in.post(
        url, json={"question": "연차 며칠?", "expected": {"document_ids": [document_id]}}
    )
    by_segment = await logged_in.post(
        url,
        json={"question": "연차?", "expected": {"segment_ids": [segments["규정"]]}, "note": "청크"},
    )
    no_answer = await logged_in.post(url, json={"question": "?", "expected": {}})
    stranger = await logged_in.post(
        url, json={"question": "보안?", "expected": {"document_ids": [foreign]}}
    )
    run = await logged_in.post(f"{BASE}/{kb['id']}/eval/run", json={"save": False})
    deleted = await logged_in.delete(f"{url}/{created.json()['id']}", headers=FETCH)
    again = await logged_in.delete(f"{url}/{created.json()['id']}", headers=FETCH)

    assert empty_run.status_code == 422
    assert empty_run.json()["error"]["code"] == "eval_cases_empty"
    assert created.status_code == 201, created.text
    assert created.json()["expected"] == {"document_ids": [document_id], "segment_ids": []}
    assert by_segment.json()["note"] == "청크"
    assert no_answer.status_code == 422
    assert no_answer.json()["error"]["code"] == "eval_expected_required"
    assert stranger.status_code == 404
    assert [case["rank"] for case in run.json()["cases"]] == [1, 1]
    assert deleted.status_code == 204 and again.status_code == 404
    assert (await logged_in.get(url)).json()["total"] == 1


async def test_only_the_latest_thirty_runs_are_kept(logged_in: AsyncClient, app: FastAPI) -> None:
    kb = await create_kb(logged_in)
    await seed(app, kb, {"규정": "연차는 15일이다"})
    await logged_in.post(
        f"{BASE}/{kb['id']}/eval/cases/import",
        json={"csv": "question,expected_document_name\n연차?,규정\n"},
    )

    ids = []
    for _ in range(31):
        response = await logged_in.post(f"{BASE}/{kb['id']}/eval/run", json={})
        ids.append(response.json()["id"])
    listed = await logged_in.get(f"{BASE}/{kb['id']}/eval/runs")
    oldest = await logged_in.get(f"{BASE}/{kb['id']}/eval/runs/{ids[0]}")

    items = listed.json()["items"]
    assert len(items) == 30
    assert items[0]["id"] == ids[-1]
    assert oldest.status_code == 404


async def test_replace_keeps_the_set_when_nothing_is_importable(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    await seed(app, kb, {"규정": "연차는 15일이다"})
    url = f"{BASE}/{kb['id']}/eval/cases/import"
    await logged_in.post(url, json={"csv": "question,expected_document_name\n연차?,규정\n"})

    bad_header = await logged_in.post(url, json={"csv": "q,a\n연차?,규정\n", "replace": True})
    unknown_only = await logged_in.post(
        url,
        json={"csv": "question,expected_document_name\n병가?,없는 문서\n", "replace": True},
    )
    cases = await logged_in.get(f"{BASE}/{kb['id']}/eval/cases")

    for response in (bad_header, unknown_only):
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "eval_import_empty"
    assert [p["line"] for p in unknown_only.json()["error"]["details"]["problems"]] == [2]
    assert [case["question"] for case in cases.json()["items"]] == ["연차?"]


async def test_llm_rerank_runs_are_capped_before_any_model_call(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    await seed(app, kb, {"규정": "연차는 15일이다"})
    rows = ["question,expected_document_name"]
    rows += [f"연차 질문 {index}?,규정" for index in range(RERANK_EVAL_MAX_CASES + 1)]
    imported = await logged_in.post(
        f"{BASE}/{kb['id']}/eval/cases/import", json={"csv": "\n".join(rows) + "\n"}
    )
    assert imported.json()["created"] == RERANK_EVAL_MAX_CASES + 1
    chat = FakeAdapter([])
    app.state.llm = chat

    refused = await logged_in.post(
        f"{BASE}/{kb['id']}/eval/run",
        json={"retrieval_model": {"reranking_mode": "llm"}, "save": False},
    )

    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "eval_rerank_limit"
    assert error["details"] == {"max": RERANK_EVAL_MAX_CASES, "cases": RERANK_EVAL_MAX_CASES + 1}
    assert chat.requests == []
