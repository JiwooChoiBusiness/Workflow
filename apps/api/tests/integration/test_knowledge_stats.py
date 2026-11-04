"""KB 통계, 지식 기본값, 콘솔 indexing-status(3단계 설계 12.1, M25).

가짜 임베더로 문서를 끝까지 넣어 본다.
"""

import asyncio
import math
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text

from app import sysinfo
from app.config import Settings
from app.db.engine import Database
from app.db.models import Workspace
from app.db.types import new_id
from app.llm.base import ModelAdapter
from app.llm.fake_adapter import FakeAdapter, FakeEmbedder, fake_rate_limit_error
from app.logsetup import configure_logging
from app.main import create_app
from app.schemas.knowledge import ProcessRule, RetrievalSettings
from app.services.knowledge_documents import KnowledgeDocumentService
from app.services.knowledge_index_executor import KnowledgeLane
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
    settings: Settings, monkeypatch: pytest.MonkeyPatch, llm: ModelAdapter | None = None
) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    application = create_app(settings)
    if llm is not None:
        application.state.llm = llm
    try:
        async with application.router.lifespan_context(application):
            yield application
    finally:
        configure_logging("INFO")


@pytest.fixture
async def app(
    fast_settings: Settings, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FastAPI]:
    async with serve(fast_settings, monkeypatch, FakeAdapter(embedder=embedder)) as application:
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
    client: AsyncClient, kb_id: str, document_id: str, timeout: float = 20.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        document = (await client.get(f"{BASE}/{kb_id}/documents/{document_id}")).json()
        if document["indexing_status"] in DONE:
            return document
        if time.monotonic() > deadline:
            raise AssertionError(f"문서가 끝나지 않았다: {document['indexing_status']}")
        await asyncio.sleep(0.05)


def paragraphs(count: int, words: int = 30) -> str:
    return "\n\n".join(
        " ".join(f"문단{index}의 낱말{word}" for word in range(words)) for index in range(count)
    )


async def stats_of(client: AsyncClient, kb_id: str) -> dict[str, Any]:
    response = await client.get(f"{BASE}/{kb_id}/stats")
    assert response.status_code == 200, response.text
    return response.json()


async def test_stats_follow_a_document_to_completion(logged_in: AsyncClient, app: FastAPI) -> None:
    kb = await create_kb(logged_in)
    document = await add_text(logged_in, kb["id"], paragraphs(6, 60))
    done = await wait_document(logged_in, kb["id"], document["id"])
    assert done["indexing_status"] == "completed", done

    stats = await stats_of(logged_in, kb["id"])

    units = done["total_segments"]
    assert units > 1
    assert stats["documents"] == {
        "total": 1,
        "queuing": 0,
        "indexing": 0,
        "paused": 0,
        "error": 0,
        "available": 1,
        "disabled": 0,
        "archived": 0,
    }
    assert (stats["segment_count"], stats["unit_count"], stats["vector_count"]) == (
        units,
        units,
        units,
    )
    assert (stats["vector_ratio"], stats["vector_state"]) == (1.0, "ready")
    assert (stats["pending_vector_units"], stats["estimated_minutes"]) == (0, 0)
    assert stats["index_bytes"] > 0
    assert stats["index_bytes"] == runtime_of(app).index_db.disk_bytes()
    kb_now = (await logged_in.get(f"{BASE}/{kb['id']}")).json()
    assert stats["embedded_tokens_estimated"] == kb_now["embedded_tokens_estimated"] > 0
    assert (stats["daily_limited_documents"], stats["daily_retry_after"]) == (0, None)
    assert (stats["index_available"], stats["index_error"]) == (True, None)
    assert (stats["indexing_technique"], stats["key_tier"]) == ("high_quality", "free")


async def test_status_counts_follow_the_display_status(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    # 상태별 수는 (indexing_status, enabled, archived) 묶음 한 번으로 센다. 목록 필터와 같은
    # 규칙이다.
    kb = await create_kb(logged_in)
    first = await add_text(logged_in, kb["id"], "첫째 " + paragraphs(2, 20), name="첫째")
    second = await add_text(logged_in, kb["id"], "둘째 " + paragraphs(2, 20), name="둘째")
    third = await add_text(logged_in, kb["id"], "셋째 " + paragraphs(2, 20), name="셋째")
    for document in (first, second, third):
        await wait_document(logged_in, kb["id"], document["id"])
    url = f"{BASE}/{kb['id']}/documents/status"
    disabled = await logged_in.post(f"{url}/disable", json={"document_ids": [first["id"]]})
    archived = await logged_in.post(f"{url}/archive", json={"document_ids": [second["id"]]})
    assert disabled.is_success and archived.is_success

    stats = await stats_of(logged_in, kb["id"])

    assert stats["documents"] == {
        "total": 3,
        "queuing": 0,
        "indexing": 0,
        "paused": 0,
        "error": 0,
        "available": 1,
        "disabled": 1,
        "archived": 1,
    }
    for status, count in stats["documents"].items():
        if status == "total" or not count:
            continue
        listed = await logged_in.get(f"{BASE}/{kb['id']}/documents", params={"status": status})
        assert listed.json()["total"] == count, status


async def test_daily_limit_shows_in_stats_and_defaults(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder
) -> None:
    embedder.push(fake_rate_limit_error("day", retry_after=None))
    kb = await create_kb(logged_in)
    document = await add_text(logged_in, kb["id"], paragraphs(4, 60), name="한도에 걸림")
    failed = await wait_document(logged_in, kb["id"], document["id"])
    assert failed["error"]["code"] == "rate_limited_daily"

    stats = await stats_of(logged_in, kb["id"])

    assert (stats["documents"]["error"], stats["daily_limited_documents"]) == (1, 1)
    retry_after = datetime.fromisoformat(stats["daily_retry_after"])
    assert retry_after == datetime.fromisoformat(failed["error"]["retry_after"])
    assert retry_after > datetime.now(UTC)
    # 전문 색인은 먼저 만들었고 벡터는 하나도 없다. 남은 유닛을 분당 인제스트 몫으로 나눈다.
    units = stats["unit_count"]
    assert units > 0 and stats["vector_count"] == 0
    assert stats["vector_ratio"] == 0.0
    assert stats["pending_vector_units"] == units
    limits = runtime_of(app).limiter.limits("free")
    assert stats["estimated_minutes"] == math.ceil(units / (limits.rpm - limits.reserve))
    defaults = (await logged_in.get("/api/console/knowledge-defaults")).json()
    free = next(item for item in defaults["daily"] if item["tier"] == "free")
    assert free["blocked_until"] is not None
    assert datetime.fromisoformat(free["blocked_until"]) > datetime.now(UTC)


async def test_economy_stats_have_no_vectors(logged_in: AsyncClient) -> None:
    kb = await create_kb(logged_in, indexing_technique="economy")
    document = await add_text(logged_in, kb["id"], paragraphs(3, 40))
    assert (await wait_document(logged_in, kb["id"], document["id"]))["indexing_status"] == (
        "completed"
    )

    stats = await stats_of(logged_in, kb["id"])

    assert stats["unit_count"] > 0
    assert (stats["vector_count"], stats["vector_ratio"], stats["vector_state"]) == (
        None,
        None,
        None,
    )
    assert (stats["pending_vector_units"], stats["estimated_minutes"]) == (0, 0)
    assert stats["embedded_tokens_estimated"] == 0


async def test_stats_without_the_index_file_keep_document_counts(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    document = await add_text(logged_in, kb["id"], paragraphs(2, 40))
    await wait_document(logged_in, kb["id"], document["id"])
    index_db = runtime_of(app).index_db
    index_db.available = False
    index_db.unavailable_reason = "OSError: 시험으로 막음"
    try:
        stats = await stats_of(logged_in, kb["id"])
        defaults = (await logged_in.get("/api/console/knowledge-defaults")).json()
    finally:
        index_db.available = True
        index_db.unavailable_reason = None

    assert stats["documents"]["total"] == 1
    assert (stats["index_available"], stats["index_error"]) == (False, "OSError: 시험으로 막음")
    for field in (
        "unit_count",
        "vector_count",
        "vector_ratio",
        "vector_state",
        "pending_vector_units",
        "estimated_minutes",
        "index_bytes",
    ):
        assert stats[field] is None, field
    assert (defaults["index_available"], defaults["fts_mode"]) == (False, None)
    assert defaults["index_error"] == "OSError: 시험으로 막음"


async def test_stats_stay_in_the_workspace(
    logged_in: AsyncClient, make_client: Callable[[], AsyncClient]
) -> None:
    kb = await create_kb(logged_in)

    missing = await logged_in.get(f"{BASE}/{uuid.uuid4()}/stats")
    anon = make_client()
    anonymous = [
        (await anon.get(path)).status_code
        for path in (
            f"{BASE}/{kb['id']}/stats",
            "/api/console/knowledge-defaults",
            f"{BASE}/{kb['id']}/documents/indexing-status?batch=x",
        )
    ]

    assert missing.status_code == 404
    assert anonymous == [401, 401, 401]


async def test_defaults_describe_models_rules_and_the_running_limiter(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    response = await logged_in.get("/api/console/knowledge-defaults")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["embedding_models"] == [
        {
            "id": "gemini-embedding-2",
            "dimensions": [768, 1536, 3072],
            "default_dimensions": 768,
            "prefixes": ["none", "gemini2"],
            "default_prefix": "gemini2",
            "input_token_limit": 8192,
        }
    ]
    assert body["embedding"] == {
        "model": "gemini-embedding-2",
        "dimensions": 768,
        "prefix": "gemini2",
    }
    assert body["process_rule"] == ProcessRule().model_dump(mode="json")
    assert body["retrieval_model"] == RetrievalSettings().model_dump(mode="json")
    # 가짜 임베더라 무료 등급 상한과 하루 몫을 걸지 않는다(설정값 그대로).
    runtime = runtime_of(app)
    assert body["limiter"]["capped"] is False
    for tier in ("free", "paid"):
        limits = runtime.limiter.limits(tier)
        assert body["limiter"][tier] == {
            "rpm": limits.rpm,
            "reserve": limits.reserve,
            "tpm": limits.tpm,
            "rpd": limits.rpd,
        }
    assert body["limiter"]["free"]["rpm"] == 60_000
    assert body["daily"] == [
        {
            "tier": tier,
            "limit": 0,
            "used": 0,
            "bulk_remaining": None,
            "reset_at": None,
            "blocked_until": None,
        }
        for tier in ("free", "paid")
    ]
    assert body["paid_key"] is False
    assert body["sqlite_version"] == sqlite3.sqlite_version
    expected_mode = "contentless" if runtime.index_db.fts_contentless else "stored"
    assert (body["fts_mode"], body["index_available"]) == (expected_mode, True)
    assert body["index_error"] is None
    assert (body["top_k_max"], body["upload_max_files"], body["upload_max_mb"]) == (20, 20, 50)
    assert (body["doc_max_text_mb"], body["query_log_days"]) == (8, 90)


async def test_real_adapter_caps_the_free_tier(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 실제 어댑터(키는 가짜, 호출하지 않는다)면 무료 등급 상한(분당 100)과 하루 몫을 건다. 설정
    # 화면(LLM)도 이 값(지금 돌고 있는 리미터)을 보여 준다.
    paid = "paid-" + "7" * 26 + "zz99"
    capped = settings.model_copy(
        update={"kb_embed_rpm": 500, "gemini_paid_api_key": SecretStr(paid)}
    )
    async with (
        serve(capped, monkeypatch) as application,
        AsyncClient(
            transport=ASGITransport(app=application), base_url="http://127.0.0.1:8500"
        ) as c,
    ):
        assert (
            await c.post("/api/console/setup", json={"email": EMAIL, "password": PASSWORD})
        ).status_code == 201
        assert (
            await c.post("/api/console/auth/login", json={"email": EMAIL, "password": PASSWORD})
        ).status_code == 200

        defaults = (await c.get("/api/console/knowledge-defaults")).json()

    assert defaults["limiter"] == {
        "model": "gemini-embedding-2",
        "capped": True,
        "free": {"rpm": 100, "reserve": 10, "tpm": 0, "rpd": 1000},
        "paid": {"rpm": 90, "reserve": 10, "tpm": 0, "rpd": 0},
    }
    free = next(item for item in defaults["daily"] if item["tier"] == "free")
    # 하루 1,000건 중 사람이 기다리는 요청 몫(1000 × 10 / 100)을 뺀 것이 인제스트 몫이다.
    assert (free["limit"], free["used"], free["bulk_remaining"]) == (1000, 0, 900)
    assert free["reset_at"] is not None and free["blocked_until"] is None
    paid_day = next(item for item in defaults["daily"] if item["tier"] == "paid")
    assert (paid_day["limit"], paid_day["bulk_remaining"]) == (0, None)
    assert defaults["paid_key"] is True
    assert paid not in str(defaults)


async def test_console_indexing_status_reports_one_upload_batch(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)
    response = await logged_in.post(
        f"{BASE}/{kb['id']}/documents",
        files=[
            ("files", ("a.txt", paragraphs(3, 40).encode(), "text/plain")),
            ("files", ("b.md", paragraphs(2, 40).encode(), "text/markdown")),
        ],
        headers=FETCH,
    )
    assert response.status_code == 201, response.text
    created = response.json()
    batch = created["batch"]
    ids = {document["id"] for document in created["documents"]}
    for document_id in ids:
        await wait_document(logged_in, kb["id"], document_id)

    found = await logged_in.get(
        f"{BASE}/{kb['id']}/documents/indexing-status", params={"batch": batch}
    )
    other = await add_text(logged_in, kb["id"], paragraphs(1, 20), name="다른 묶음")
    again = await logged_in.get(
        f"{BASE}/{kb['id']}/documents/indexing-status", params={"batch": batch}
    )
    unknown = await logged_in.get(
        f"{BASE}/{kb['id']}/documents/indexing-status", params={"batch": "no-such-batch"}
    )
    missing = await logged_in.get(f"{BASE}/{kb['id']}/documents/indexing-status")
    wrong_kb = await logged_in.get(
        f"{BASE}/{uuid.uuid4()}/documents/indexing-status", params={"batch": batch}
    )

    assert found.status_code == 200, found.text
    items = found.json()["data"]
    assert {item["id"] for item in items} == ids
    for item in items:
        assert item["indexing_status"] == "completed"
        assert item["completed_segments"] == item["total_segments"] > 0
        assert item["completed_at"] is not None and item["error"] is None
    assert other["id"] not in {item["id"] for item in again.json()["data"]}
    assert unknown.json() == {"data": []}
    assert missing.status_code == 422
    assert wrong_kb.status_code == 404


async def test_stats_right_after_changing_the_embedding_are_stale(
    logged_in: AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 색인 설정을 바꾸면 레인이 테이블을 다시 만들기 전까지 옛 벡터는 쓰지 않는다. 그동안 벡터는
    # 0개로 세고 유닛이 모두 대기다. 레인이 집지 않게 해서 바로 다음 순간을 본다.
    kb = await create_kb(logged_in)
    document = await add_text(logged_in, kb["id"], paragraphs(4, 60))
    done = await wait_document(logged_in, kb["id"], document["id"])
    assert done["indexing_status"] == "completed"
    monkeypatch.setattr(KnowledgeLane, "index", lambda self, workspace_id, document_id: False)

    changed = await logged_in.patch(
        f"{BASE}/{kb['id']}",
        json={
            "embedding": {"model": "gemini-embedding-2", "dimensions": 1536, "prefix": "gemini2"},
            "confirm_reembed": True,
        },
    )
    assert changed.status_code == 200, changed.text
    stats = await stats_of(logged_in, kb["id"])

    units = stats["unit_count"]
    assert units == done["total_segments"] > 0
    assert (stats["vector_state"], stats["vector_count"], stats["vector_ratio"]) == (
        "stale",
        0,
        0.0,
    )
    assert stats["pending_vector_units"] == units
    limits = runtime_of(app).limiter.limits("free")
    assert stats["estimated_minutes"] == math.ceil(units / (limits.rpm - limits.reserve))
    assert stats["documents"]["queuing"] == 1


async def test_stats_in_the_middle_of_an_ingest(
    logged_in: AsyncClient, app: FastAPI, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 묶음을 작게 하고 임베딩을 늦춰 색인 도중의 통계를 본다. 남은 유닛은 유닛 - 벡터이고 예상 분은
    # 그것을 분당 인제스트 몫으로 나눈 값이다.
    monkeypatch.setattr(runtime_of(app).limiter, "batch_items", lambda tier, priority: 4)
    embedder.delay_sec = 0.05
    kb = await create_kb(logged_in)
    rule = {"general": {"separators": ["\n\n"], "max_tokens": 40, "overlap_tokens": 0}}
    response = await logged_in.post(
        f"{BASE}/{kb['id']}/documents",
        json={"name": "긴 글", "text": paragraphs(60, 4), "process_rule": rule},
    )
    assert response.status_code == 201, response.text
    document = response.json()["documents"][0]

    middle: dict[str, Any] | None = None
    deadline = time.monotonic() + 20
    while middle is None and time.monotonic() < deadline:
        stats = await stats_of(logged_in, kb["id"])
        units, vectors = stats["unit_count"], stats["vector_count"]
        if units and vectors and 0 < vectors < units:
            middle = stats
        else:
            await asyncio.sleep(0.02)
    assert middle is not None, "색인 도중의 통계를 보지 못했다"

    units, vectors = middle["unit_count"], middle["vector_count"]
    assert middle["vector_state"] == "ready"
    assert middle["pending_vector_units"] == units - vectors
    limits = runtime_of(app).limiter.limits("free")
    expected = math.ceil((units - vectors) / (limits.rpm - limits.reserve))
    assert middle["estimated_minutes"] == expected
    assert 0 < middle["vector_ratio"] < 1
    assert middle["documents"]["indexing"] == 1
    await wait_document(logged_in, kb["id"], document["id"])


async def test_eval_cases_carry_the_names_of_live_expected_documents(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    # 평가 탭이 문서 목록을 따로 읽지 않게 정답 문서 이름을 싣는다. 지운 문서는 빠진다.
    kb = await create_kb(logged_in)
    kept = await add_text(logged_in, kb["id"], "남는 " + paragraphs(2, 20), name="남는 문서")
    gone = await add_text(logged_in, kb["id"], "지울 " + paragraphs(2, 20), name="지울 문서")
    for document in (kept, gone):
        await wait_document(logged_in, kb["id"], document["id"])
    cases = f"{BASE}/{kb['id']}/eval/cases"
    for question, document_id in (("남는 문서 질문", kept["id"]), ("지울 문서 질문", gone["id"])):
        created = await logged_in.post(
            cases, json={"question": question, "expected": {"document_ids": [document_id]}}
        )
        assert created.status_code == 201, created.text
    removed = await logged_in.delete(f"{BASE}/{kb['id']}/documents/{gone['id']}", headers=FETCH)
    assert removed.status_code == 204

    listed = (await logged_in.get(cases)).json()

    assert listed["total"] == 2
    assert listed["document_names"] == {kept["id"]: "남는 문서"}


async def test_original_file_downloads_as_an_attachment(
    logged_in: AsyncClient, app: FastAPI, db: Database
) -> None:
    kb = await create_kb(logged_in)
    uploaded = await logged_in.post(
        f"{BASE}/{kb['id']}/documents",
        files=[
            ("files", ("메모.txt", "원본 내용".encode(), "text/plain")),
            ("files", ("page.html", b"<script>alert(1)</script><p>hi</p>", "text/html")),
        ],
        headers=FETCH,
    )
    assert uploaded.status_code == 201, uploaded.text
    memo, page = uploaded.json()["documents"]
    pasted = await add_text(logged_in, kb["id"], "붙여 넣은 글", name="회의")
    for document in (memo, page, pasted):
        await wait_document(logged_in, kb["id"], document["id"])

    def file_url(document: dict[str, Any]) -> str:
        return f"{BASE}/{kb['id']}/documents/{document['id']}/file"

    memo_file = await logged_in.get(file_url(memo))
    page_file = await logged_in.get(file_url(page))
    pasted_file = await logged_in.get(file_url(pasted))

    assert memo_file.status_code == 200
    assert memo_file.content == "원본 내용".encode()
    disposition = memo_file.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''%EB%A9%94%EB%AA%A8.txt" in disposition
    assert memo_file.headers["x-content-type-options"] == "nosniff"
    assert memo_file.headers["content-type"].startswith("text/plain")
    # 브라우저가 열면 스크립트가 도는 원본은 내려받기로만 준다.
    assert page_file.headers["content-type"] == "application/octet-stream"
    assert page_file.headers["content-disposition"].startswith("attachment;")
    assert page_file.headers["x-content-type-options"] == "nosniff"
    assert pasted_file.content == "붙여 넣은 글".encode()
    assert "filename*=UTF-8''%ED%9A%8C%EC%9D%98.txt" in pasted_file.headers["content-disposition"]

    # 원본을 바꾼 문서는 지금 원본을 준다.
    me = (await logged_in.get("/api/console/auth/me")).json()
    workspace_id = uuid.UUID(me["workspace"]["id"])
    service = KnowledgeDocumentService(db, app.state.settings, app.state.storage)
    await service.replace_text(
        workspace_id, uuid.UUID(kb["id"]), uuid.UUID(pasted["id"]), "새 원본"
    )
    assert (await logged_in.get(file_url(pasted))).content == "새 원본".encode()

    # 지운 문서와 다른 워크스페이스의 KB는 404다.
    removed = await logged_in.delete(f"{BASE}/{kb['id']}/documents/{memo['id']}", headers=FETCH)
    assert removed.status_code == 204
    assert (await logged_in.get(file_url(memo))).status_code == 404
    other = new_id()
    async with db.write() as session:
        session.add(Workspace(id=other, name="다른 곳", slug=f"other-{other.hex[:8]}", settings={}))
        await session.flush()
        await session.execute(
            text("UPDATE knowledge_base SET workspace_id = :ws WHERE id = :kb"),
            {"ws": str(other), "kb": kb["id"]},
        )
    assert (await logged_in.get(file_url(page))).status_code == 404
