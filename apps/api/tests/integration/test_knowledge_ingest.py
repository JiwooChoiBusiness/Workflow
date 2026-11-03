"""지식 레인 인제스트(3단계 설계 4.4, 5.4, 6.1~6.7, M22). 가짜 임베더로 돈다.

pdf·docx는 샌드박스 파서가 필요해 tests/windows/test_kb_ingest_pdf.py가 본다. 여기서는 서버 안에서
읽는 txt·md·csv·html과 붙여 넣은 글로 단계·색인·편집·멈춤·삭제·복구·정합 검사를 본다.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app import sysinfo
from app.config import Settings
from app.db.engine import Database
from app.knowledge.units import FtsRow, VecRow, doc_id_of
from app.llm.embeddings import DOCUMENT_PREFIX, EmbeddingConfig, content_hash, pack_vector
from app.llm.fake_adapter import (
    FakeAdapter,
    FakeEmbedder,
    fake_embedding,
    fake_rate_limit_error,
    fake_server_error,
)
from app.logsetup import configure_logging
from app.main import create_app
from app.services.knowledge_runtime import KnowledgeRuntime

FETCH = {"X-Requested-With": "fetch"}
BASE = "/api/console/knowledge"
EMAIL = "owner@example.com"
PASSWORD = "correct-horse-battery"  # noqa: S105  테스트 계정
DONE = ("completed", "error", "paused")


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def fast_settings(settings: Settings) -> Settings:
    # 가짜 임베더라 무료 상한이 없다. 분당 몫을 크게 줘서 20초 조각을 기다리지 않게 한다.
    return settings.model_copy(update={"kb_embed_rpm": 60_000, "shutdown_grace_sec": 2})


@asynccontextmanager
async def serve(
    settings: Settings, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    application = create_app(settings)
    application.state.llm = FakeAdapter(embedder=embedder)
    try:
        async with application.router.lifespan_context(application):
            yield application
    finally:
        configure_logging("INFO")


@pytest.fixture
async def app(
    fast_settings: Settings, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FastAPI]:
    async with serve(fast_settings, embedder, monkeypatch) as application:
        yield application


def runtime_of(app: FastAPI) -> KnowledgeRuntime:
    return app.state.knowledge


async def create_kb(client: AsyncClient, **body: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "사내 문서", "data_policy": {"acknowledged": True}}
    payload.update(body)
    response = await client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def add_text(client: AsyncClient, kb_id: str, body: str, name: str = "글") -> dict[str, Any]:
    response = await client.post(f"{BASE}/{kb_id}/documents", json={"name": name, "text": body})
    assert response.status_code == 201, response.text
    return response.json()["documents"][0]


async def wait_document(
    client: AsyncClient,
    kb_id: str,
    document_id: str,
    statuses: tuple[str, ...] = DONE,
    timeout: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        document = (await client.get(f"{BASE}/{kb_id}/documents/{document_id}")).json()
        if document["indexing_status"] in statuses:
            return document
        if time.monotonic() > deadline:
            raise AssertionError(f"문서가 {statuses}가 되지 않았다: {document['indexing_status']}")
        await asyncio.sleep(0.05)


async def segments(client: AsyncClient, kb_id: str, document_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"{BASE}/{kb_id}/documents/{document_id}/segments?limit=100")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def paragraphs(count: int, words: int = 30) -> str:
    return "\n\n".join(
        " ".join(f"문단{index}의 낱말{word}" for word in range(words)) for index in range(count)
    )


async def test_text_document_goes_through_every_stage(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    kb = await create_kb(logged_in)
    document = await add_text(logged_in, kb["id"], paragraphs(5, 200))

    done = await wait_document(logged_in, kb["id"], document["id"])

    assert done["indexing_status"] == "completed", done
    stamps = [
        done[name]
        for name in (
            "processing_started_at",
            "parsing_completed_at",
            "cleaning_completed_at",
            "splitting_completed_at",
            "completed_at",
        )
    ]
    assert all(stamps) and stamps == sorted(stamps)
    items = await segments(logged_in, kb["id"], document["id"])
    assert len(items) == done["total_segments"] == done["completed_segments"] > 1
    assert all(item["index_state"]["fts"] == "ok" for item in items)
    assert all(item["index_state"]["vector"] == "ok" for item in items)
    runtime = runtime_of(app)
    kb_id = uuid.UUID(kb["id"])
    assert await runtime.fts.count(kb_id) == len(items)
    assert await runtime.vectors.count(kb_id) == len(items)
    first = items[0]
    vector = await runtime.vectors.get(kb_id, first["unit_no"])
    assert vector == pack_vector(fake_embedding(DOCUMENT_PREFIX + first["content"]))
    assert embedder.embedded_items == len(items)


async def upload(
    client: AsyncClient, kb_id: str, files: list[tuple[str, bytes, str]], **options: Any
) -> list[dict[str, Any]]:
    import json

    response = await client.post(
        f"{BASE}/{kb_id}/documents",
        files=[("files", item) for item in files],
        data={"data": json.dumps(options)} if options else None,
        headers=FETCH,
    )
    assert response.status_code == 201, response.text
    return response.json()["documents"]


async def test_txt_md_csv_and_html_files_are_read_on_the_server(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    files = [
        ("메모.txt", "첫 줄\n\n둘째 문단입니다".encode("cp949"), "text/plain"),
        ("안내.md", "# 제목\n\n본문 내용".encode(), "text/markdown"),
        ("표.csv", "이름,값\n가,1\n나,2\n".encode(), "text/csv"),
        (
            "페이지.html",
            "<html><head><script>x()</script></head><body><p>보이는 글</p></body></html>".encode(),
            "text/html",
        ),
    ]

    documents = await upload(logged_in, kb["id"], files)
    results = [await wait_document(logged_in, kb["id"], document["id"]) for document in documents]

    assert [result["indexing_status"] for result in results] == ["completed"] * 4
    texts = {
        result["name"]: " ".join(
            item["content"] for item in await segments(logged_in, kb["id"], result["id"])
        )
        for result in results
    }
    assert "둘째 문단입니다" in texts["메모.txt"]  # cp949도 읽는다
    assert "본문 내용" in texts["안내.md"]
    assert "가,1" in texts["표.csv"]
    assert "보이는 글" in texts["페이지.html"] and "x()" not in texts["페이지.html"]
    assert all(result["extract"]["mode"] == "text" for result in results)


async def test_economy_kb_makes_no_embedding_calls(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    kb = await create_kb(logged_in, indexing_technique="economy")
    document = await add_text(logged_in, kb["id"], paragraphs(4, 150))

    done = await wait_document(logged_in, kb["id"], document["id"])

    assert done["indexing_status"] == "completed"
    items = await segments(logged_in, kb["id"], document["id"])
    assert items and all(
        item["index_state"] == {**item["index_state"], "fts": "ok", "vector": "skip"}
        for item in items
    )
    assert all(item["status"] == "completed" for item in items)
    assert embedder.calls == []
    runtime = runtime_of(app)
    kb_id = uuid.UUID(kb["id"])
    assert await runtime.fts.count(kb_id) == len(items)
    hits = await runtime.fts.search(kb_id, "문단2의", 3)
    assert hits and hits[0].unit_no == items[2]["unit_no"]


async def test_parent_child_and_qa_documents(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    tree = await create_kb(logged_in, name="부모 자식", doc_form="hierarchical_model")
    qa = await create_kb(logged_in, name="문답", doc_form="qa_model")
    tree_doc = await add_text(logged_in, tree["id"], paragraphs(3, 120))
    qa_doc = await add_text(
        logged_in, qa["id"], "question,answer\n환불 기간은?,7일\n배송비는?,무료\n,빈 칸\n"
    )
    bad_qa = await add_text(logged_in, qa["id"], "그냥 글이다", name="쌍 없음")

    tree_done = await wait_document(logged_in, tree["id"], tree_doc["id"])
    qa_done = await wait_document(logged_in, qa["id"], qa_doc["id"])
    bad_done = await wait_document(logged_in, qa["id"], bad_qa["id"])

    assert tree_done["indexing_status"] == "completed"
    parents = await segments(logged_in, tree["id"], tree_doc["id"])
    assert all(parent["unit_no"] is None and parent["status"] == "completed" for parent in parents)
    children = [child for parent in parents for child in parent["child_chunks"]]
    assert children and all(child["index_state"]["vector"] == "ok" for child in children)
    assert await runtime_of(app).vectors.count(uuid.UUID(tree["id"])) == len(children)
    assert qa_done["indexing_status"] == "completed"
    pairs = await segments(logged_in, qa["id"], qa_doc["id"])
    assert [(item["content"], item["answer"]) for item in pairs] == [
        ("환불 기간은?", "7일"),
        ("배송비는?", "무료"),
    ]
    assert "qa_empty_rows" in qa_done["extract"]["warnings"]
    # Q&A는 질문만 임베딩한다.
    assert {call.inputs[0] for call in embedder.calls if len(call.inputs) == 2} == {
        DOCUMENT_PREFIX + "환불 기간은?"
    }
    assert bad_done["indexing_status"] == "error"
    assert bad_done["error"]["code"] == "qa_no_pairs"


async def completed(
    client: AsyncClient, kb_id: str, body: str, name: str = "글"
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = await add_text(client, kb_id, body, name)
    done = await wait_document(client, kb_id, document["id"])
    assert done["indexing_status"] == "completed", done
    return done, await segments(client, kb_id, document["id"])


async def test_editing_a_chunk_reaches_index_db_in_the_request(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    # 청크 편집은 요청 안에서 index.db까지 반영한다(M21 검토 1-1).
    kb = await create_kb(logged_in)
    document, items = await completed(logged_in, kb["id"], paragraphs(3, 120))
    runtime = runtime_of(app)
    kb_id = uuid.UUID(kb["id"])
    target = items[1]
    url = f"{BASE}/{kb['id']}/documents/{document['id']}/segments/{target['id']}"

    edited = await logged_in.patch(url, json={"content": "고친 청크에 쿼크젤리 붙임"})

    body = edited.json()
    assert edited.status_code == 200, edited.text
    assert (body["status"], body["index_state"]["fts"], body["index_state"]["vector"]) == (
        "completed",
        "ok",
        "ok",
    )
    assert await runtime.vectors.get(kb_id, target["unit_no"]) == pack_vector(
        fake_embedding(DOCUMENT_PREFIX + "고친 청크에 쿼크젤리 붙임")
    )
    hits = await runtime.fts.search(kb_id, "쿼크젤리", 5)
    assert [hit.unit_no for hit in hits] == [target["unit_no"]]
    # 고치기 전 글의 벡터는 캐시에 있다(편집을 되돌리면 API를 부르지 않는다).
    config = EmbeddingConfig()
    cached = await runtime.cache.lookup(config.key(), [content_hash(target["content"])])
    assert list(cached) == [content_hash(target["content"])]
    calls = len(embedder.calls)
    reverted = await logged_in.patch(url, json={"content": target["content"]})
    assert reverted.json()["index_state"]["vector"] == "ok"
    assert len(embedder.calls) == calls
    refreshed = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{document['id']}")).json()
    assert refreshed["indexing_status"] == "completed"
    assert refreshed["completed_segments"] == refreshed["total_segments"]


async def test_disabling_moves_the_vector_to_the_cache_and_enabling_restores_it(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    kb = await create_kb(logged_in)
    document, items = await completed(logged_in, kb["id"], paragraphs(2, 120))
    runtime = runtime_of(app)
    kb_id = uuid.UUID(kb["id"])
    target = items[0]
    url = f"{BASE}/{kb['id']}/documents/{document['id']}/segments/{target['id']}"
    before = await runtime.vectors.get(kb_id, target["unit_no"])

    off = await logged_in.patch(url, json={"enabled": False})

    assert off.status_code == 200, off.text
    assert off.json()["enabled"] is False
    assert await runtime.vectors.get(kb_id, target["unit_no"]) is None
    assert target["unit_no"] not in {
        hit.unit_no for hit in await runtime.fts.search(kb_id, "문단0의", 10)
    }
    cached = await runtime.cache.lookup(EmbeddingConfig().key(), [content_hash(target["content"])])
    assert cached == {content_hash(target["content"]): before}

    calls = len(embedder.calls)
    on = await logged_in.patch(url, json={"enabled": True})

    assert on.json()["index_state"]["vector"] == "ok" and on.json()["enabled"] is True
    assert await runtime.vectors.get(kb_id, target["unit_no"]) == before
    assert len(embedder.calls) == calls


async def test_adding_and_deleting_chunks(logged_in: AsyncClient, app: FastAPI) -> None:
    kb = await create_kb(logged_in)
    document, items = await completed(logged_in, kb["id"], paragraphs(2, 120))
    runtime = runtime_of(app)
    kb_id = uuid.UUID(kb["id"])
    url = f"{BASE}/{kb['id']}/documents/{document['id']}/segments"

    added = await logged_in.post(url, json={"content": "손으로 넣은 청크 쿼크젤리"})

    assert added.status_code == 201, added.text
    unit_no = added.json()["unit_no"]
    assert added.json()["index_state"]["vector"] == "ok"
    assert await runtime.vectors.get(kb_id, unit_no) is not None
    removed = await logged_in.delete(f"{url}/{added.json()['id']}", headers=FETCH)
    assert removed.status_code == 204
    # 색인 행부터 지운다(M21 검토 2-10).
    assert await runtime.vectors.get(kb_id, unit_no) is None
    assert await runtime.fts.search(kb_id, "쿼크젤리", 5) == []
    assert await runtime.vectors.count(kb_id) == len(items)


async def test_a_failed_edit_queues_the_document_for_the_lane(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    kb = await create_kb(logged_in)
    document, items = await completed(logged_in, kb["id"], paragraphs(2, 120))
    # 키가 하나라 같은 키로 한 번 더 보낸 뒤 실패한다. 편집은 app.db에 남고 문서가 레인에 들어간다.
    embedder.push(fake_server_error(503), fake_server_error(503))
    url = f"{BASE}/{kb['id']}/documents/{document['id']}/segments/{items[0]['id']}"

    edited = await logged_in.patch(url, json={"content": "429 뒤에 레인이 색인"})

    assert edited.status_code == 200, edited.text
    assert edited.json()["index_state"]["vector"] == "pending"
    done = await wait_document(logged_in, kb["id"], document["id"])
    assert done["indexing_status"] == "completed"
    again = (await logged_in.get(url)).json()
    assert again["index_state"]["vector"] == "ok"
    assert await runtime_of(app).vectors.get(
        uuid.UUID(kb["id"]), items[0]["unit_no"]
    ) == pack_vector(fake_embedding(DOCUMENT_PREFIX + "429 뒤에 레인이 색인"))


def small_batches(app: FastAPI, monkeypatch: pytest.MonkeyPatch, size: int = 5) -> None:
    # 임베딩 묶음을 작게 해 작업이 여러 묶음을 돌게 한다(묶음 경계에서만 멈춘다).
    monkeypatch.setattr(runtime_of(app).limiter, "batch_items", lambda tier, priority: size)


async def wait_until(check: Callable[[], Awaitable[bool]], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not await check():
        if time.monotonic() > deadline:
            raise AssertionError("조건이 채워지지 않았다")
        await asyncio.sleep(0.02)


def lane_idle(app: FastAPI) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return not app.state.knowledge_supervisor.stats()["active"]

    return check


async def test_pause_stops_at_a_batch_boundary_and_resume_continues(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_batches(app, monkeypatch)
    embedder.delay_sec = 0.05
    kb = await create_kb(logged_in)
    document = await add_text(logged_in, kb["id"], paragraphs(40, 60))
    url = f"{BASE}/{kb['id']}/documents/{document['id']}"

    async def some_done() -> bool:
        return (await logged_in.get(url)).json()["completed_segments"] >= 5

    await wait_until(some_done)
    paused = await logged_in.post(
        f"{BASE}/{kb['id']}/documents/status/pause", json={"document_ids": [document["id"]]}
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["items"][0]["indexing_status"] == "paused"
    assert paused.json()["items"][0]["paused_at"] is not None
    await wait_until(lane_idle(app))
    stopped = (await logged_in.get(url)).json()
    calls = embedder.embedded_items
    await asyncio.sleep(0.3)
    assert embedder.embedded_items == calls
    assert stopped["indexing_status"] == "paused"
    total = stopped["total_segments"]
    assert 0 < stopped["completed_segments"] < total

    resumed = await logged_in.post(
        f"{BASE}/{kb['id']}/documents/status/resume", json={"document_ids": [document["id"]]}
    )
    assert resumed.json()["items"][0]["indexing_status"] == "waiting"
    done = await wait_document(logged_in, kb["id"], document["id"], ("completed",))

    assert done["completed_segments"] == total
    # 재개는 pending 유닛만 잇는다. 같은 청크를 두 번 보내지 않았다.
    assert embedder.embedded_items == total


async def test_a_paused_document_left_in_the_queue_is_not_run(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    small_batches(app, monkeypatch)
    embedder.delay_sec = 0.05
    kb = await create_kb(logged_in)
    first = await add_text(logged_in, kb["id"], paragraphs(20, 60), name="먼저")
    second = await add_text(logged_in, kb["id"], paragraphs(3, 60), name="나중")
    lane = app.state.knowledge_supervisor
    assert lane.stats()["queued"] == 1

    paused = await logged_in.post(
        f"{BASE}/{kb['id']}/documents/status/pause", json={"document_ids": [second["id"]]}
    )

    assert paused.json()["items"][0]["indexing_status"] == "paused"
    assert lane.stats()["queued"] == 0
    await wait_document(logged_in, kb["id"], first["id"], ("completed",))
    await asyncio.sleep(0.2)
    left = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{second['id']}")).json()
    assert (left["indexing_status"], left["processing_started_at"]) == ("paused", None)
    await logged_in.post(
        f"{BASE}/{kb['id']}/documents/status/resume", json={"document_ids": [second["id"]]}
    )
    done = await wait_document(logged_in, kb["id"], second["id"])
    assert done["indexing_status"] == "completed"


async def test_daily_limit_ends_documents_with_retry_after_and_no_requeue(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    embedder.push(fake_rate_limit_error("day", retry_after=None))
    kb = await create_kb(logged_in)
    hit = await add_text(logged_in, kb["id"], paragraphs(3, 60), name="한도에 걸림")

    failed = await wait_document(logged_in, kb["id"], hit["id"])

    assert failed["indexing_status"] == "error"
    error = failed["error"]
    assert (error["code"], error["retryable"]) == ("rate_limited_daily", True)
    assert error["retry_after"] is not None
    calls = len(embedder.calls)
    # 같은 모델의 다음 문서는 API를 부르지 않고 같은 사유로 끝난다. 전문 색인은 먼저 만든다.
    other = await add_text(logged_in, kb["id"], paragraphs(2, 60), name="다음 문서")
    other_done = await wait_document(logged_in, kb["id"], other["id"])
    assert other_done["error"]["code"] == "rate_limited_daily"
    assert len(embedder.calls) == calls
    items = await segments(logged_in, kb["id"], other["id"])
    assert items and all(item["index_state"]["fts"] == "ok" for item in items)
    # 자정 전 다시 시도는 안내만 한다. 저절로 다시 들어가지도 않는다.
    retried = await logged_in.post(
        f"{BASE}/{kb['id']}/documents/status/retry", json={"document_ids": [hit["id"]]}
    )
    assert retried.json()["items"] == []
    assert retried.json()["skipped"][0]["code"] == "retry_after"
    await asyncio.sleep(0.3)
    still = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{hit['id']}")).json()
    assert still["indexing_status"] == "error"
    stats = app.state.knowledge_supervisor.stats()
    assert (stats["active"], stats["queued"]) == (0, 0)


async def test_status_actions(logged_in: AsyncClient, app: FastAPI, db: Database) -> None:
    kb = await create_kb(logged_in)
    document, _ = await completed(logged_in, kb["id"], paragraphs(2, 60))
    ids = {"document_ids": [document["id"], str(uuid.uuid4())]}
    url = f"{BASE}/{kb['id']}/documents/status"

    disabled = await logged_in.post(f"{url}/disable", json=ids)
    archived = await logged_in.post(f"{url}/archive", json=ids)
    unarchived = await logged_in.post(f"{url}/unarchive", json=ids)
    enabled = await logged_in.post(f"{url}/enable", json=ids)
    not_paused = await logged_in.post(f"{url}/resume", json=ids)
    async with db.write() as session:
        await session.execute(
            text("UPDATE kb_document SET indexing_status = :status WHERE id = :id"),
            {"id": document["id"], "status": "indexing"},
        )
    busy = await logged_in.post(f"{url}/disable", json=ids)

    assert disabled.json()["items"][0]["display_status"] == "disabled"
    assert disabled.json()["skipped"][0]["code"] == "not_found"
    assert archived.json()["items"][0]["display_status"] == "archived"
    assert unarchived.json()["items"][0]["archived"] is False
    assert enabled.json()["items"][0]["display_status"] == "available"
    assert {item["code"] for item in not_paused.json()["skipped"]} == {"not_found", "not_paused"}
    assert busy.status_code == 400 and busy.json()["error"]["code"] == "document_indexing"


async def count_rows(db: Database, table: str, column: str, value: str) -> int:
    async with db.read() as session:
        result = await session.execute(
            text(f"SELECT count(*) FROM {table} WHERE {column} = :value"),  # noqa: S608
            {"value": value},
        )
        return int(result.scalar_one())


async def test_delete_returns_at_once_and_the_lane_removes_everything(
    logged_in: AsyncClient, app: FastAPI, db: Database
) -> None:
    kb = await create_kb(logged_in)
    document, items = await completed(logged_in, kb["id"], paragraphs(30, 60))
    runtime = runtime_of(app)
    kb_id = uuid.UUID(kb["id"])
    storage = app.state.storage
    async with db.read() as session:
        key = (
            await session.execute(
                text("SELECT storage_key FROM kb_document WHERE id = :id"), {"id": document["id"]}
            )
        ).scalar_one()
    assert storage.local_path(key).is_file()

    started = time.monotonic()
    deleted = await logged_in.delete(f"{BASE}/{kb['id']}/documents/{document['id']}", headers=FETCH)
    elapsed = time.monotonic() - started

    assert deleted.status_code == 204
    assert elapsed < 1.0

    async def gone() -> bool:
        return await count_rows(db, "kb_document", "id", document["id"]) == 0

    await wait_until(gone)
    assert await count_rows(db, "kb_segment", "document_id", document["id"]) == 0
    assert await runtime.vectors.count(kb_id) == 0
    assert await runtime.fts.count(kb_id) == 0
    assert not storage.local_path(key).exists()
    # 지운 유닛의 벡터는 캐시에 남아 같은 글을 다시 올리면 API를 부르지 않는다.
    cached = await runtime.cache.lookup(
        EmbeddingConfig().key(), [content_hash(item["content"]) for item in items]
    )
    assert len(cached) == len(items)


async def test_reindex_splits_again_from_the_original_without_new_calls(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    kb = await create_kb(logged_in)
    document, items = await completed(logged_in, kb["id"], paragraphs(6, 60))
    calls = embedder.embedded_items
    url = f"{BASE}/{kb['id']}/documents/{document['id']}/reindex"

    model = await logged_in.post(url, json={"extract_mode": "model"})
    reindexed = await logged_in.post(url, json={})

    assert model.status_code == 422 and model.json()["error"]["code"] == "model_read_pdf_only"
    assert reindexed.status_code == 200, reindexed.text
    done = await wait_document(logged_in, kb["id"], document["id"], ("completed",))
    again = await segments(logged_in, kb["id"], document["id"])
    assert [item["content"] for item in again] == [item["content"] for item in items]
    assert {item["id"] for item in again}.isdisjoint({item["id"] for item in items})
    assert done["completed_segments"] == len(items)
    # 지운 유닛의 벡터가 캐시에 있어 같은 청크는 API를 다시 부르지 않는다.
    assert embedder.embedded_items == calls
    assert await runtime_of(app).vectors.count(uuid.UUID(kb["id"])) == len(items)


async def test_deleting_a_kb_stops_its_jobs_and_the_hourly_purge_clears_it(
    logged_in: AsyncClient,
    app: FastAPI,
    embedder: FakeEmbedder,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    small_batches(app, monkeypatch)
    embedder.delay_sec = 0.05
    kb = await create_kb(logged_in)
    document = await add_text(logged_in, kb["id"], paragraphs(30, 60))
    url = f"{BASE}/{kb['id']}/documents/{document['id']}"

    async def indexing() -> bool:
        return (await logged_in.get(url)).json()["completed_segments"] >= 5

    await wait_until(indexing)
    assert (await logged_in.delete(f"{BASE}/{kb['id']}", headers=FETCH)).status_code == 204
    await wait_until(lane_idle(app))
    calls = embedder.embedded_items
    await asyncio.sleep(0.2)
    assert embedder.embedded_items == calls

    result = await app.state.knowledge_maintenance.purge_deleted()

    assert (result["kbs"], result["documents"]) == (1, 1)
    assert await count_rows(db, "kb_document", "kb_id", kb["id"]) == 0
    assert await count_rows(db, "kb_segment", "kb_id", kb["id"]) == 0
    assert await runtime_of(app).index_db.describe(uuid.UUID(kb["id"])) is None


@asynccontextmanager
async def signed_in(application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://127.0.0.1:8500"
    ) as client:
        setup = await client.get("/api/console/setup")
        if setup.json().get("needs_setup"):
            created = await client.post(
                "/api/console/setup", json={"email": EMAIL, "password": PASSWORD}
            )
            assert created.status_code == 201, created.text
        login = await client.post(
            "/api/console/auth/login", json={"email": EMAIL, "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
        yield client


async def test_restart_resumes_indexing_and_only_embeds_what_was_left(
    fast_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = FakeEmbedder(delay_sec=0.05)
    async with (
        serve(fast_settings, first, monkeypatch) as application,
        signed_in(application) as client,
    ):
        small_batches(application, monkeypatch)
        kb = await create_kb(client)
        document = await add_text(client, kb["id"], paragraphs(40, 60))
        url = f"{BASE}/{kb['id']}/documents/{document['id']}"

        async def some_done() -> bool:
            return (await client.get(url)).json()["completed_segments"] >= 5

        await wait_until(some_done)
    # 종료 유예 안에 묶음 경계에서 멈췄다. 문서는 indexing으로 남고 다음 기동이 잇는다.
    async with (
        serve(fast_settings, FakeEmbedder(), monkeypatch) as application,
        signed_in(application) as client,
    ):
        left = (await client.get(url)).json()
        assert left["indexing_status"] in ("waiting", "indexing")
        done = await wait_document(client, kb["id"], document["id"], ("completed",))
        second: FakeEmbedder = application.state.llm.embedder

    assert done["completed_segments"] == done["total_segments"]
    assert first.embedded_items + second.embedded_items == done["total_segments"]
    assert second.embedded_items == done["total_segments"] - left["completed_segments"]


async def test_restart_during_splitting_starts_over_without_new_calls(
    fast_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        serve(fast_settings, FakeEmbedder(), monkeypatch) as application,
        signed_in(application) as client,
    ):
        kb = await create_kb(client)
        document, items = await completed(client, kb["id"], paragraphs(8, 60))
        db: Database = application.state.db
        # 분할 도중에 죽은 문서처럼 만든다(분할 완료 시각이 없다).
        async with db.write() as session:
            await session.execute(
                text(
                    "UPDATE kb_document SET indexing_status = 'splitting',"
                    " splitting_completed_at = NULL WHERE id = :id"
                ),
                {"id": document["id"]},
            )
    embedder = FakeEmbedder()
    async with (
        serve(fast_settings, embedder, monkeypatch) as application,
        signed_in(application) as client,
    ):
        done = await wait_document(client, kb["id"], document["id"], ("completed",))
        again = await segments(client, kb["id"], document["id"])

    assert [item["content"] for item in again] == [item["content"] for item in items]
    assert {item["id"] for item in again}.isdisjoint({item["id"] for item in items})
    assert done["completed_segments"] == len(items)
    # 지운 부분 결과의 벡터는 캐시로 갔다가 그대로 다시 쓰인다.
    assert embedder.embedded_items == 0


async def test_orphan_rows_are_removed_and_lost_rows_are_requeued(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    kb = await create_kb(logged_in)
    document, items = await completed(logged_in, kb["id"], paragraphs(4, 60))
    runtime = runtime_of(app)
    kb_id = uuid.UUID(kb["id"])
    stray = 9_000_000
    await runtime.fts.upsert(kb_id, [FtsRow(stray, "손으로 넣은 고아 행")])
    await runtime.vectors.upsert(
        kb_id, [VecRow(stray, pack_vector(fake_embedding("고아")), doc_id_of(uuid.uuid4()))]
    )
    lost = items[1]["unit_no"]
    await runtime.vectors.delete(kb_id, [lost])
    calls = embedder.embedded_items

    result = await app.state.knowledge_maintenance.purge_orphans()

    assert (result["vec"], result["fts"], result["requeued"]) == (1, 1, 1)
    assert await runtime.vectors.get(kb_id, stray) is None
    done = await wait_document(logged_in, kb["id"], document["id"], ("completed",))
    assert done["completed_segments"] == len(items)
    assert await runtime.vectors.get(kb_id, lost) is not None
    assert embedder.embedded_items == calls + 1


async def test_a_changed_embedding_setting_rebuilds_the_vector_table(
    logged_in: AsyncClient, app: FastAPI, db: Database
) -> None:
    # 차원을 바꾼 app.db(옛 index.db 백업을 새 app.db와 쓰는 경우)를 감지해 다시 만든다(5.4, KS10).
    kb = await create_kb(logged_in)
    document, items = await completed(logged_in, kb["id"], paragraphs(4, 60))
    runtime = runtime_of(app)
    kb_id = uuid.UUID(kb["id"])
    async with db.write() as session:
        await session.execute(
            text(
                "UPDATE knowledge_base SET embedding = json_set(embedding, '$.dimensions', 1536)"
                " WHERE id = :id"
            ),
            {"id": kb["id"]},
        )

    result = await app.state.knowledge_maintenance.reconcile()

    assert (result["kbs"], result["documents"]) == (1, 1)
    done = await wait_document(logged_in, kb["id"], document["id"], ("completed",))
    assert done["completed_segments"] == len(items)
    meta = await runtime.vectors.describe(kb_id)
    assert meta is not None and meta.dims == 1536
    blob = await runtime.vectors.get(kb_id, items[0]["unit_no"])
    assert blob is not None and len(blob) == 1536 * 4


async def test_unit_numbers_never_go_back_below_index_rows(
    fast_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # app.db만 옛 백업으로 되돌린 경우처럼 index.db에 더 큰 rowid가 있으면 기동 때 번호를 올린다
    # (M21 검토 1-4).
    async with (
        serve(fast_settings, FakeEmbedder(), monkeypatch) as application,
        signed_in(application) as client,
    ):
        kb = await create_kb(client)
        await completed(client, kb["id"], paragraphs(2, 60))
        runtime = runtime_of(application)
        await runtime.fts.upsert(uuid.UUID(kb["id"]), [FtsRow(5_000_000, "미래의 행")])
    async with (
        serve(fast_settings, FakeEmbedder(), monkeypatch) as application,
        signed_in(application) as client,
    ):
        document, items = await completed(client, kb["id"], paragraphs(3, 60), name="새 글")

    assert min(item["unit_no"] for item in items) > 5_000_000


async def test_backup_with_the_index_keeps_knn_working_and_without_it_rebuilds(
    fast_settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app import backup
    from app.knowledge.index_db import IndexDb

    async with (
        serve(fast_settings, FakeEmbedder(), monkeypatch) as application,
        signed_in(application) as client,
    ):
        kb = await create_kb(client)
        document, items = await completed(client, kb["id"], paragraphs(4, 60))
        full = await asyncio.to_thread(backup.create_backup, fast_settings, out=tmp_path / "full")
        bare = await asyncio.to_thread(
            backup.create_backup, fast_settings, out=tmp_path / "bare", with_index=False
        )

    assert full.manifest["knowledge_index"]["included"] is True
    assert bare.manifest["knowledge_index"] == {
        "included": False,
        "schema_version": None,
        "bytes": 0,
    }
    copy = IndexDb(tmp_path / "full" / "knowledge" / "index.db")
    assert await copy.open()
    try:
        from app.knowledge.vector_index import SqliteVecIndex

        query = pack_vector(fake_embedding(DOCUMENT_PREFIX + items[-1]["content"]))
        hits = await SqliteVecIndex(copy).search(uuid.UUID(kb["id"]), query, 3)
        assert hits[0].unit_no == items[-1]["unit_no"]
        assert hits[0].cosine == pytest.approx(1.0, abs=1e-5)
    finally:
        await copy.close()

    # index.db 없이 복구하면 모든 문서를 다시 색인한다(5.5).
    restored = fast_settings.model_copy(update={"data_dir": tmp_path / "bare"})
    embedder = FakeEmbedder()
    async with (
        serve(restored, embedder, monkeypatch) as application,
        signed_in(application) as client,
    ):
        done = await wait_document(client, kb["id"], document["id"], ("completed",))
        runtime = runtime_of(application)
        assert await runtime.vectors.count(uuid.UUID(kb["id"])) == len(items)
        assert await runtime.fts.count(uuid.UUID(kb["id"])) == len(items)
    assert done["completed_segments"] == len(items)
    assert embedder.embedded_items == len(items)


async def test_interactive_permits_are_not_blocked_by_bulk_ingest(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # K13 대신(검색 API는 M23): 인제스트가 분당 몫을 다 써 다음 20초 조각을 기다리는 동안에도
    # interactive 임베딩은 2초 안에 허가받는다.
    slow = settings.model_copy(update={"kb_embed_rpm": 90, "shutdown_grace_sec": 0.5})
    async with (
        serve(slow, FakeEmbedder(), monkeypatch) as application,
        signed_in(application) as client,
    ):
        kb = await create_kb(client)
        document = await add_text(client, kb["id"], paragraphs(120, 40))
        limiter = runtime_of(application).limiter

        async def bulk_is_waiting() -> bool:
            return limiter.usage("free")[0] >= 27

        await wait_until(bulk_is_waiting)
        assert application.state.knowledge_supervisor.stats()["active"] == 1
        started = time.monotonic()
        await runtime_of(application).embeddings.embed_query("검색 질의", EmbeddingConfig())
        elapsed = time.monotonic() - started
        progress = (await client.get(f"{BASE}/{kb['id']}/documents/{document['id']}")).json()

    assert elapsed < 2.0
    assert progress["indexing_status"] == "indexing"


async def test_preview_splits_without_storing(logged_in: AsyncClient, app: FastAPI) -> None:
    kb = await create_kb(logged_in)
    tree = await create_kb(logged_in, name="부모 자식", doc_form="hierarchical_model")
    economy = await create_kb(logged_in, name="경제", indexing_technique="economy")
    body = paragraphs(80, 120)

    general = await logged_in.post(f"{BASE}/{kb['id']}/documents/preview", json={"text": body})
    small = await logged_in.post(
        f"{BASE}/{kb['id']}/documents/preview",
        json={"text": body, "process_rule": {"general": {"max_tokens": 200, "overlap_tokens": 0}}},
    )
    nested = await logged_in.post(f"{BASE}/{tree['id']}/documents/preview", json={"text": body})
    cheap = await logged_in.post(f"{BASE}/{economy['id']}/documents/preview", json={"text": body})
    uploaded = await logged_in.post(
        f"{BASE}/{kb['id']}/documents/preview",
        files={"file": ("메모.txt", "첫째\n\n둘째".encode(), "text/plain")},
        headers=FETCH,
    )

    assert general.status_code == 200, general.text
    result = general.json()
    assert result["total_chunks"] > 1 and len(result["chunks"]) == min(50, result["total_chunks"])
    assert (
        result["tokens"] == sum(chunk["tokens"] for chunk in result["chunks"])
        or result["total_chunks"] > 50
    )
    # 분당 인제스트 몫(90 - 10 = 80)으로 나눈 값이다.
    assert result["estimated_minutes"] == -(-result["total_chunks"] // 80)
    assert small.json()["total_chunks"] > result["total_chunks"]
    tree_result = nested.json()
    assert len(tree_result["chunks"]) <= 10 and tree_result["total_child_chunks"] > 0
    assert all(chunk["children"] for chunk in tree_result["chunks"])
    assert cheap.json()["estimated_minutes"] == 0
    assert uploaded.status_code == 200, uploaded.text
    assert [chunk["content"] for chunk in uploaded.json()["chunks"]] == ["첫째\n\n둘째"]
    listed = (await logged_in.get(f"{BASE}/{kb['id']}/documents")).json()
    assert listed["total"] == 0


async def test_a_long_document_finishes_within_the_limiter(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    # K2의 축소판: 청크 1,500개 문서가 분당 몫(가짜라 상한 없음) 안에서 끝까지 간다.
    kb = await create_kb(
        logged_in, process_rule={"general": {"max_tokens": 40, "overlap_tokens": 0}}
    )
    document = await add_text(logged_in, kb["id"], paragraphs(1500, 8))

    done = await wait_document(logged_in, kb["id"], document["id"], ("completed",), timeout=120)

    assert done["total_segments"] >= 1500
    assert done["completed_segments"] == done["total_segments"]
    assert embedder.embedded_items == done["total_segments"]
    assert max(len(call.inputs) for call in embedder.calls) <= 100
    kb_id = uuid.UUID(kb["id"])
    assert await runtime_of(app).vectors.count(kb_id) == done["total_segments"]


def document_service(app: FastAPI) -> Any:
    from app.services.knowledge_documents import KnowledgeDocumentService

    return KnowledgeDocumentService(
        app.state.db,
        app.state.settings,
        app.state.storage,
        lane=app.state.knowledge_lane,
        sandbox=app.state.kb_sandbox,
        limiter=runtime_of(app).limiter,
    )


async def workspace_of(db: Database, kb_id: str) -> uuid.UUID:
    async with db.read() as session:
        value = (
            await session.execute(
                text("SELECT workspace_id FROM knowledge_base WHERE id = :id"), {"id": kb_id}
            )
        ).scalar_one()
    return uuid.UUID(str(value))


async def test_replacing_the_text_keeps_the_document_and_reindexes(
    logged_in: AsyncClient, app: FastAPI, db: Database
) -> None:
    from app.services.errors import ServiceError

    kb = await create_kb(logged_in)
    document, old_items = await completed(logged_in, kb["id"], paragraphs(3, 60), name="규정")
    other, _ = await completed(logged_in, kb["id"], "다른 문서의 글", name="다른")
    field = await logged_in.post(
        f"{BASE}/{kb['id']}/metadata", json={"name": "owner", "type": "string"}
    )
    await logged_in.post(
        f"{BASE}/{kb['id']}/documents/metadata",
        json={
            "document_ids": [document["id"]],
            "values": [{"field_id": field.json()["id"], "value": "총무팀"}],
            "apply_to_all": True,
        },
    )
    service = document_service(app)
    workspace_id = await workspace_of(db, kb["id"])
    kb_id, document_id = uuid.UUID(kb["id"]), uuid.UUID(document["id"])
    storage = app.state.storage
    async with db.read() as session:
        old_key = (
            await session.execute(
                text("SELECT storage_key FROM kb_document WHERE id = :id"), {"id": document["id"]}
            )
        ).scalar_one()

    with pytest.raises(ServiceError) as duplicate:
        await service.replace_text(workspace_id, kb_id, document_id, "다른 문서의 글")
    replaced = await service.replace_text(
        workspace_id, kb_id, document_id, "바뀐 규정 본문 쿼크젤리"
    )

    assert (duplicate.value.status, duplicate.value.code) == (409, "duplicate_document")
    assert replaced.id == document_id and replaced.name == "규정"
    assert replaced.indexing_status == "waiting" and replaced.splitting_completed_at is None
    assert [item.value for item in replaced.doc_metadata] == ["총무팀"]
    done = await wait_document(logged_in, kb["id"], document["id"], ("completed",))
    items = await segments(logged_in, kb["id"], document["id"])
    assert [item["content"] for item in items] == ["바뀐 규정 본문 쿼크젤리"]
    assert done["sha256"] != document["sha256"]
    assert {item["unit_no"] for item in items}.isdisjoint({item["unit_no"] for item in old_items})
    hits = await runtime_of(app).fts.search(kb_id, "쿼크젤리", 5)
    assert [hit.unit_no for hit in hits] == [items[0]["unit_no"]]
    # 옛 원본은 지우고 새 키에 둔다. 같은 KB의 다른 문서는 그대로다.
    assert not storage.local_path(old_key).exists()
    async with db.read() as session:
        new_key = (
            await session.execute(
                text("SELECT storage_key FROM kb_document WHERE id = :id"), {"id": document["id"]}
            )
        ).scalar_one()
    assert new_key != old_key and storage.local_path(new_key).is_file()
    assert (await logged_in.get(f"{BASE}/{kb['id']}/documents/{other['id']}")).json()[
        "indexing_status"
    ] == "completed"
    # 행이 가리키는 원본은 매시 정리가 지우지 않는다.
    assert await app.state.knowledge_maintenance.purge_loose_files() == 0


async def test_replacing_follows_the_reindex_rules(
    logged_in: AsyncClient, app: FastAPI, db: Database
) -> None:
    from app.services.errors import ServiceError
    from app.services.knowledge_documents import UploadSource

    kb = await create_kb(logged_in)
    document, _ = await completed(logged_in, kb["id"], paragraphs(2, 60))
    service = document_service(app)
    workspace_id = await workspace_of(db, kb["id"])
    kb_id, document_id = uuid.UUID(kb["id"]), uuid.UUID(document["id"])

    async def chunks(data: bytes) -> AsyncIterator[bytes]:
        yield data

    async def refused(**changes: Any) -> tuple[int, str]:
        async with db.write() as session:
            assignments = ", ".join(f"{name} = :{name}" for name in changes)
            await session.execute(
                text(f"UPDATE kb_document SET {assignments} WHERE id = :id"),  # noqa: S608
                {"id": document["id"], **changes},
            )
        with pytest.raises(ServiceError) as raised:
            await service.replace_text(workspace_id, kb_id, document_id, "새 글")
        return raised.value.status, raised.value.code

    image = await asyncio.gather(
        service.replace_file(
            workspace_id,
            kb_id,
            document_id,
            UploadSource("그림.png", "image/png", chunks(b"png")),
        ),
        return_exceptions=True,
    )
    model = await asyncio.gather(
        service.replace_file(
            workspace_id,
            kb_id,
            document_id,
            UploadSource("메모.txt", "text/plain", chunks(b"memo")),
            extract_mode="model",
        ),
        return_exceptions=True,
    )
    indexing = await refused(indexing_status="indexing")
    archived = await refused(indexing_status="completed", archived=True)
    async with db.write() as session:
        await session.execute(
            text("UPDATE kb_document SET archived = 0, indexing_status = 'paused' WHERE id = :id"),
            {"id": document["id"]},
        )
    # 멈춘 문서는 재색인처럼 바꿀 수 있다.
    resumed = await service.replace_text(workspace_id, kb_id, document_id, "멈춘 뒤 바꾼 글")
    paused = await wait_document(logged_in, kb["id"], document["id"], ("completed",))
    from_file = await service.replace_file(
        workspace_id,
        kb_id,
        document_id,
        UploadSource("새 메모.txt", "text/plain", chunks("파일에서 온 새 글".encode())),
        name="새 이름",
    )

    assert isinstance(image[0], ServiceError) and image[0].code == "unsupported_file_type"
    assert isinstance(model[0], ServiceError) and model[0].code == "model_read_pdf_only"
    assert indexing == (400, "document_indexing")
    assert archived == (400, "document_archived")
    assert resumed.indexing_status == "waiting"
    assert paused["indexing_status"] == "completed"
    assert (from_file.name, from_file.filename) == ("새 이름", "새 메모.txt")
    done = await wait_document(logged_in, kb["id"], document["id"], ("completed",))
    items = await segments(logged_in, kb["id"], document["id"])
    assert [item["content"] for item in items] == ["파일에서 온 새 글"]
    assert done["name"] == "새 이름"


async def test_changing_the_embedding_mid_ingest_finishes_with_the_new_setting(
    logged_in: AsyncClient,
    app: FastAPI,
    embedder: FakeEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 작업이 옛 설정으로 끝까지 가 completed를 적으면 새 작업이 건너뛰어진다. 옛 작업은 설정이
    # 바뀐 것을 보고 빠지고 새 작업이 새 차원으로 다시 채운다.
    small_batches(app, monkeypatch)
    embedder.delay_sec = 0.05
    kb = await create_kb(logged_in)
    document = await add_text(logged_in, kb["id"], paragraphs(30, 60))
    url = f"{BASE}/{kb['id']}/documents/{document['id']}"

    async def some_done() -> bool:
        return (await logged_in.get(url)).json()["completed_segments"] >= 5

    await wait_until(some_done)
    changed = await logged_in.patch(
        f"{BASE}/{kb['id']}", json={"embedding": {"dimensions": 1536}, "confirm_reembed": True}
    )
    assert changed.status_code == 200, changed.text

    done = await wait_document(logged_in, kb["id"], document["id"], ("completed",), timeout=60)

    runtime = runtime_of(app)
    kb_id = uuid.UUID(kb["id"])
    items = await segments(logged_in, kb["id"], document["id"])
    assert done["completed_segments"] == len(items)
    assert all(item["index_state"]["embedding_version"] == 2 for item in items)
    meta = await runtime.vectors.describe(kb_id)
    assert meta is not None and meta.dims == 1536
    assert await runtime.vectors.count(kb_id) == len(items)


async def test_a_failed_edit_leaves_no_old_vector_for_search(
    logged_in: AsyncClient,
    app: FastAPI,
    embedder: FakeEmbedder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 검색은 index.db 행으로 무엇을 찾을지 정한다. 새 글의 임베딩이 실패해도 그동안 옛 글의
    # 벡터로 잡히면 안 된다. 전문 색인은 새 글로 바로 바뀐다(레인이 돌기 전 상태를 본다).
    kb = await create_kb(logged_in)
    document, items = await completed(logged_in, kb["id"], paragraphs(3, 60))
    monkeypatch.setattr(app.state.knowledge_lane, "index", lambda workspace_id, document_id: False)
    runtime = runtime_of(app)
    kb_id = uuid.UUID(kb["id"])
    target = items[0]
    old_query = pack_vector(fake_embedding(DOCUMENT_PREFIX + target["content"]))
    before = await runtime.vectors.search(kb_id, old_query, 1)
    assert before[0].unit_no == target["unit_no"]
    embedder.push(fake_server_error(503), fake_server_error(503))
    url = f"{BASE}/{kb['id']}/documents/{document['id']}/segments/{target['id']}"

    edited = await logged_in.patch(url, json={"content": "바꾼 글 쿼크젤리"})

    assert edited.status_code == 200, edited.text
    state = edited.json()["index_state"]
    assert (state["fts"], state["vector"]) == ("ok", "pending")
    assert await runtime.vectors.get(kb_id, target["unit_no"]) is None
    hits = await runtime.vectors.search(kb_id, old_query, len(items))
    assert target["unit_no"] not in {hit.unit_no for hit in hits}
    assert [hit.unit_no for hit in await runtime.fts.search(kb_id, "쿼크젤리", 5)] == [
        target["unit_no"]
    ]
    old_words = await runtime.fts.search(kb_id, target["content"][:12], len(items))
    assert old_words and target["unit_no"] not in {hit.unit_no for hit in old_words}
    # 옛 벡터는 캐시에 있어 편집을 되돌리면 API를 부르지 않는다.
    cached = await runtime.cache.lookup(EmbeddingConfig().key(), [content_hash(target["content"])])
    assert list(cached) == [content_hash(target["content"])]
