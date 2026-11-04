"""지식 베이스·문서·청크·메타데이터 콘솔 API(3단계 설계 4장, 12.1, 14.4, M21a).

app.db 쪽만 본다. 지식 레인이 문서를 집지 않게 하고 청크 편집은 index.db 없이(색인 파일을 열지 못한
경우와 같게) 돌린다. 그래서 문서는 waiting에 머물고 청크는 pending으로 남는다. 청크 편집 경로를
보려고 문서 상태는 DB에서 직접 바꾼다. 레인과 요청 안 색인은 test_knowledge_ingest.py가 본다.
"""

import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import text

from app import sysinfo
from app.config import Settings
from app.db.engine import Database
from app.db.models import ApiKey
from app.db.models.enums import KbDataSourceType
from app.db.repositories.api_keys import ApiKeyRepository
from app.db.repositories.knowledge import KbDocumentRepository, KnowledgeBaseRepository
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id
from app.llm.fake_adapter import FakeAdapter
from app.logsetup import configure_logging
from app.main import create_app
from app.schemas.knowledge import KbDocumentUploadOptions
from app.security.tokens import hash_token
from app.services.knowledge_documents import KnowledgeDocumentService, UploadSource
from app.services.knowledge_index_executor import KnowledgeLane
from app.services.knowledge_runtime import optional_knowledge_runtime

FETCH = {"X-Requested-With": "fetch"}
BASE = "/api/console/knowledge"


@pytest.fixture
async def app(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    monkeypatch.setattr(KnowledgeLane, "index", lambda self, workspace_id, document_id: False)
    monkeypatch.setattr(KnowledgeLane, "delete", lambda self, workspace_id, document_id: None)
    application = create_app(settings)
    application.state.llm = FakeAdapter()
    application.dependency_overrides[optional_knowledge_runtime] = lambda: None
    try:
        async with application.router.lifespan_context(application):
            yield application
    finally:
        configure_logging("INFO")


def problem_types(response: Response) -> dict[str, str]:
    assert response.status_code == 422, response.text
    return {
        ".".join(str(part) for part in item["loc"][1:]): item["type"]
        for item in response.json()["error"]["details"]
    }


def error_code(response: Response, status: int) -> str:
    assert response.status_code == status, response.text
    return response.json()["error"]["code"]


async def create_kb(client: AsyncClient, **body: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "사내 매뉴얼", "data_policy": {"acknowledged": True}}
    payload.update(body)
    response = await client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def add_text(
    client: AsyncClient,
    kb_id: str,
    *,
    name: str = "회의록",
    body: str = "첫 회의 내용",
    **extra: Any,
) -> dict[str, Any]:
    response = await client.post(
        f"{BASE}/{kb_id}/documents", json={"name": name, "text": body, **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def set_document(db: Database, document_id: str, **values: Any) -> None:
    assignments = ", ".join(f"{name} = :{name}" for name in values)
    async with db.write() as session:
        await session.execute(
            text(f"UPDATE kb_document SET {assignments} WHERE id = :id"),  # noqa: S608
            {"id": document_id, **values},
        )


async def set_segment_status(db: Database, segment_id: str, status: str) -> None:
    async with db.write() as session:
        await session.execute(
            text("UPDATE kb_segment SET status = :status WHERE id = :id"),
            {"id": segment_id, "status": status},
        )


async def completed_document(
    client: AsyncClient, db: Database, kb_id: str, **text_args: Any
) -> dict[str, Any]:
    created = await add_text(client, kb_id, **text_args)
    document = created["documents"][0]
    await set_document(db, document["id"], indexing_status="completed")
    return document


def segments_url(kb_id: str, document_id: str) -> str:
    return f"{BASE}/{kb_id}/documents/{document_id}/segments"


async def test_create_needs_the_data_policy_and_fills_defaults(logged_in: AsyncClient) -> None:
    refused = await logged_in.post(BASE, json={"name": "문서", "data_policy": {}})
    paid = await logged_in.post(
        BASE, json={"name": "문서", "data_policy": {"key_tier": "paid", "acknowledged": True}}
    )

    kb = await create_kb(logged_in, description="설명")

    assert problem_types(refused) == {"data_policy.acknowledged": "data_policy_not_acknowledged"}
    assert error_code(paid, 422) == "paid_key_missing"
    assert kb["slug"] == "knowledge"
    assert (kb["doc_form"], kb["indexing_technique"]) == ("text_model", "high_quality")
    assert kb["embedding"] == {
        "model": "gemini-embedding-2",
        "dimensions": 768,
        "prefix": "gemini2",
        "task_type": None,
        "version": 1,
    }
    assert kb["retrieval_model"]["search_method"] == "hybrid_search"
    assert kb["retrieval_model"]["weights"] == {"vector_weight": 0.7, "keyword_weight": 0.3}
    assert kb["process_rule"]["general"] == {
        "separators": ["\n\n", "\n", ". ", " "],
        "max_tokens": 1024,
        "overlap_tokens": 50,
    }
    assert kb["data_policy"]["key_tier"] == "free"
    assert kb["data_policy"]["acknowledged_at"] is not None
    assert (kb["document_count"], kb["segment_count"], kb["indexing_document_count"]) == (0, 0, 0)
    listed = (await logged_in.get(BASE)).json()["items"]
    assert [item["id"] for item in listed] == [kb["id"]]
    assert (await logged_in.get(f"{BASE}/{kb['id']}")).json() == kb
    again = await create_kb(logged_in)
    assert again["slug"] == "knowledge-2"


async def test_paid_tier_needs_the_paid_key(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    app.state.settings = settings.model_copy(
        update={"gemini_paid_api_key": SecretStr("paid-" + "9" * 30)}
    )

    kb = await create_kb(logged_in, data_policy={"key_tier": "paid", "acknowledged": True})

    assert kb["data_policy"]["key_tier"] == "paid"


async def test_economy_settings_are_checked(logged_in: AsyncClient) -> None:
    with_embedding = await logged_in.post(
        BASE,
        json={
            "name": "e",
            "indexing_technique": "economy",
            "embedding": {"dimensions": 768},
            "data_policy": {"acknowledged": True},
        },
    )
    vector_search = await logged_in.post(
        BASE,
        json={
            "name": "e",
            "indexing_technique": "economy",
            "retrieval_model": {"search_method": "semantic_search", "top_k": 50},
            "data_policy": {"acknowledged": True},
        },
    )

    economy = await create_kb(logged_in, indexing_technique="economy", doc_form="qa_model")

    assert problem_types(with_embedding) == {"embedding": "embedding_not_allowed"}
    assert problem_types(vector_search) == {
        "retrieval_model.search_method": "search_method_unavailable",
        "retrieval_model.top_k": "top_k_too_large",
    }
    assert economy["embedding"] is None
    assert economy["retrieval_model"]["search_method"] == "full_text_search"


async def test_patch_rules_and_slug(logged_in: AsyncClient) -> None:
    kb = await create_kb(logged_in, slug="manual")
    other = await create_kb(logged_in, slug="other")
    url = f"{BASE}/{kb['id']}"

    renamed = await logged_in.patch(
        url,
        json={
            "name": "새 이름",
            "icon": "📘",
            "retrieval_model": {
                "search_method": "hybrid_search",
                "top_k": 6,
                "weights": {"vector_weight": 0.6, "keyword_weight": 0.2},
            },
            "process_rule": {"general": {"max_tokens": 512, "overlap_tokens": 20}},
        },
    )
    taken = await logged_in.patch(url, json={"slug": other["slug"]})
    form = await logged_in.patch(url, json={"doc_form": "qa_model"})
    downgrade = await logged_in.patch(url, json={"indexing_technique": "economy"})
    bad_rule = await logged_in.patch(
        url, json={"process_rule": {"general": {"max_tokens": 100, "overlap_tokens": 100}}}
    )

    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert (body["name"], body["icon"], body["slug"]) == ("새 이름", "📘", "manual")
    assert body["retrieval_model"]["weights"] == {"vector_weight": 0.75, "keyword_weight": 0.25}
    assert body["process_rule"]["general"]["max_tokens"] == 512
    assert error_code(taken, 409) == "slug_taken"
    assert form.status_code == 422
    assert error_code(downgrade, 422) == "downgrade_not_allowed"
    assert bad_rule.status_code == 422
    cleared = await logged_in.patch(url, json={"icon": None})
    assert cleared.json()["icon"] is None


async def test_changing_the_embedding_requeues_every_document(
    logged_in: AsyncClient, db: Database
) -> None:
    kb = await create_kb(logged_in)
    document = await completed_document(logged_in, db, kb["id"])
    segment = await logged_in.post(
        segments_url(kb["id"], document["id"]), json={"content": "벡터를 다시 만들 청크"}
    )
    assert segment.status_code == 201, segment.text
    await set_segment_status(db, segment.json()["id"], "completed")
    async with db.write() as session:
        await session.execute(
            text("UPDATE kb_segment SET index_state = :state WHERE id = :id"),
            {
                "id": segment.json()["id"],
                "state": '{"fts": "ok", "vector": "ok", "embedding_version": 1,'
                ' "embedded_at": "2026-11-02T00:00:00Z"}',
            },
        )
    url = f"{BASE}/{kb['id']}"

    unconfirmed = await logged_in.patch(url, json={"embedding": {"dimensions": 1536}})
    same = await logged_in.patch(url, json={"embedding": {"dimensions": 768}})
    confirmed = await logged_in.patch(
        url, json={"embedding": {"dimensions": 1536}, "confirm_reembed": True}
    )

    assert error_code(unconfirmed, 409) == "reembed_confirmation_required"
    assert same.json()["embedding"]["version"] == 1
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["embedding"]["dimensions"] == 1536
    assert confirmed.json()["embedding"]["version"] == 2
    doc = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{document['id']}")).json()
    assert doc["indexing_status"] == "waiting"
    seg = (
        await logged_in.get(f"{segments_url(kb['id'], document['id'])}/{segment.json()['id']}")
    ).json()
    # 전문 색인은 그대로이고 벡터만 다시 만든다.
    assert seg["index_state"] == {
        "fts": "ok",
        "vector": "pending",
        "embedding_version": None,
        "embedded_at": None,
    }
    assert seg["status"] == "waiting"
    assert confirmed.json()["indexing_document_count"] == 1


async def test_economy_upgrades_to_high_quality(logged_in: AsyncClient, db: Database) -> None:
    kb = await create_kb(logged_in, indexing_technique="economy")
    document = await completed_document(logged_in, db, kb["id"])
    created = await logged_in.post(
        segments_url(kb["id"], document["id"]), json={"content": "전문 색인만 있는 청크"}
    )
    assert created.json()["index_state"]["vector"] == "skip"

    upgraded = await logged_in.patch(
        f"{BASE}/{kb['id']}", json={"indexing_technique": "high_quality"}
    )

    assert upgraded.status_code == 200, upgraded.text
    body = upgraded.json()
    assert body["indexing_technique"] == "high_quality"
    assert body["embedding"]["version"] == 1
    assert body["retrieval_model"]["search_method"] == "full_text_search"
    seg = (
        await logged_in.get(f"{segments_url(kb['id'], document['id'])}/{created.json()['id']}")
    ).json()
    assert seg["index_state"]["vector"] == "pending"


async def test_delete_hides_the_kb_and_revokes_its_keys(
    logged_in: AsyncClient, db: Database
) -> None:
    kb = await create_kb(logged_in)
    document = await add_text(logged_in, kb["id"])
    async with db.write() as session:
        workspace = await WorkspaceRepository(session).get_default()
        assert workspace is not None
        workspace_id = workspace.id
        keys = ApiKeyRepository(session, workspace_id)
        key_id = new_id()
        keys.add(
            ApiKey(
                id=key_id,
                knowledge_base_id=uuid.UUID(kb["id"]),
                name="kb",
                key_prefix="sk-wf-kb",
                key_hash="f" * 64,
                scopes=["knowledge"],
            )
        )

    deleted = await logged_in.delete(f"{BASE}/{kb['id']}", headers=FETCH)

    assert deleted.status_code == 204
    assert (await logged_in.get(BASE)).json()["items"] == []
    assert error_code(await logged_in.get(f"{BASE}/{kb['id']}"), 404) == "not_found"
    gone = await logged_in.get(f"{BASE}/{kb['id']}/documents/{document['documents'][0]['id']}")
    assert gone.status_code == 404
    async with db.read() as session:
        key = await ApiKeyRepository(session, workspace_id).get(key_id)
    assert key is not None and key.revoked_at is not None
    # 지운 KB의 slug는 다시 쓸 수 있다.
    assert (await create_kb(logged_in))["slug"] == kb["slug"]


async def test_text_documents_are_stored_and_wait_for_the_lane(
    logged_in: AsyncClient, app: FastAPI
) -> None:
    kb = await create_kb(logged_in)

    created = await add_text(logged_in, kb["id"], name="주간 회의", body="안건 1\n\n안건 2")
    duplicate = await logged_in.post(
        f"{BASE}/{kb['id']}/documents", json={"name": "복사본", "text": "안건 1\n\n안건 2"}
    )
    allowed = await add_text(
        logged_in, kb["id"], name="복사본", body="안건 1\n\n안건 2", allow_duplicate=True
    )

    document = created["documents"][0]
    assert (document["name"], document["filename"], document["mime"]) == (
        "주간 회의",
        "주간 회의.txt",
        "text/plain",
    )
    assert (document["indexing_status"], document["display_status"]) == ("waiting", "queuing")
    assert (document["data_source_type"], document["batch"]) == ("paste_text", created["batch"])
    assert document["process_rule"] == kb["process_rule"]
    stored = app.state.storage.local_path(
        f"kb-files/{(await logged_in.get('/api/console/workspace')).json()['id']}/{document['id']}"
    )
    assert stored.read_text(encoding="utf-8") == "안건 1\n\n안건 2"
    assert error_code(duplicate, 409) == "duplicate_document"
    assert duplicate.json()["error"]["details"]["duplicates"][0]["document_id"] == document["id"]
    assert allowed["batch"] != created["batch"]
    refreshed = (await logged_in.get(f"{BASE}/{kb['id']}")).json()
    assert (refreshed["document_count"], refreshed["indexing_document_count"]) == (2, 2)


async def test_uploads_make_one_batch_and_reject_bad_files(
    logged_in: AsyncClient, app: FastAPI, settings: Settings
) -> None:
    kb = await create_kb(logged_in)
    url = f"{BASE}/{kb['id']}/documents"
    rule = '{"process_rule": {"general": {"max_tokens": 300, "overlap_tokens": 0}}}'

    uploaded = await logged_in.post(
        url,
        files=[
            ("files", ("메모.txt", "메모 본문".encode(), "text/plain")),
            ("files", ("notes.md", b"# title\n\nbody", "text/markdown")),
        ],
        data={"data": rule},
        headers=FETCH,
    )
    unsupported = await logged_in.post(
        url, files=[("files", ("tool.exe", b"MZ", "application/octet-stream"))], headers=FETCH
    )
    too_many = await logged_in.post(
        url,
        files=[
            ("files", (f"{index}.txt", f"{index}".encode(), "text/plain")) for index in range(21)
        ],
        headers=FETCH,
    )
    no_files = await logged_in.post(url, files={"data": (None, "{}")}, headers=FETCH)
    app.state.settings = settings.model_copy(update={"kb_upload_max_mb": 1})
    too_large = await logged_in.post(
        url,
        files=[
            ("files", ("small.txt", b"ok", "text/plain")),
            ("files", ("big.txt", b"x" * (1024 * 1024 + 1), "text/plain")),
        ],
        headers=FETCH,
    )

    assert uploaded.status_code == 201, uploaded.text
    documents = uploaded.json()["documents"]
    assert [doc["name"] for doc in documents] == ["메모.txt", "notes.md"]
    assert {doc["batch"] for doc in documents} == {uploaded.json()["batch"]}
    assert [doc["position"] for doc in documents] == [1, 2]
    assert {doc["data_source_type"] for doc in documents} == {"upload_file"}
    assert documents[0]["process_rule"]["general"]["max_tokens"] == 300
    assert error_code(unsupported, 415) == "unsupported_file_type"
    assert error_code(too_many, 422) == "too_many_files"
    assert no_files.status_code == 422
    assert error_code(too_large, 413) == "file_too_large"
    # 실패한 요청은 문서도 원본도 남기지 않는다.
    listed = (await logged_in.get(url)).json()
    assert listed["total"] == 2


async def test_uploads_record_where_the_files_came_from(
    logged_in: AsyncClient, app: FastAPI, db: Database
) -> None:
    # knowledge_index 노드는 같은 경로로 파일 문서를 만들고 출처를 workflow로 남긴다(M24 연동).
    kb = await create_kb(logged_in)
    me = (await logged_in.get("/api/console/auth/me")).json()
    workspace_id = uuid.UUID(me["workspace"]["id"])
    service = KnowledgeDocumentService(db, app.state.settings, app.state.storage)

    async def one(data: bytes) -> AsyncIterator[bytes]:
        yield data

    by_node = await service.create_from_uploads(
        workspace_id,
        uuid.UUID(kb["id"]),
        [UploadSource("노드.txt", "text/plain", one("노드가 만든 글".encode()))],
        KbDocumentUploadOptions(),
        data_source_type=KbDataSourceType.WORKFLOW,
    )
    by_default = await service.create_from_uploads(
        workspace_id,
        uuid.UUID(kb["id"]),
        [UploadSource("사람.txt", "text/plain", one("사람이 올린 글".encode()))],
        KbDocumentUploadOptions(),
    )

    assert by_node.documents[0].data_source_type == "workflow"
    assert by_default.documents[0].data_source_type == "upload_file"
    stored = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{by_node.documents[0].id}")).json()
    assert (stored["data_source_type"], stored["filename"]) == ("workflow", "노드.txt")
    kb_files = app.state.storage.local_path("kb-files")
    assert len([path for path in kb_files.rglob("*") if path.is_file()]) == 2


async def test_document_list_filters_and_pages(logged_in: AsyncClient, db: Database) -> None:
    kb = await create_kb(logged_in)
    first = (await add_text(logged_in, kb["id"], name="알파 회의", body="a"))["documents"][0]
    second = (await add_text(logged_in, kb["id"], name="베타", body="b"))["documents"][0]
    third = (await add_text(logged_in, kb["id"], name="감마 회의", body="c"))["documents"][0]
    await set_document(db, first["id"], indexing_status="completed")
    await set_document(db, second["id"], indexing_status="completed", enabled=False)
    await set_document(db, third["id"], indexing_status="indexing")
    url = f"{BASE}/{kb['id']}/documents"

    async def names(**params: Any) -> list[str]:
        response = await logged_in.get(url, params=params)
        assert response.status_code == 200, response.text
        return [item["name"] for item in response.json()["items"]]

    assert await names() == ["감마 회의", "베타", "알파 회의"]
    assert await names(status="available") == ["알파 회의"]
    assert await names(status="disabled") == ["베타"]
    assert await names(status="indexing") == ["감마 회의"]
    assert await names(status="queuing") == []
    assert await names(keyword="회의") == ["감마 회의", "알파 회의"]
    assert await names(keyword="%") == []
    page = (await logged_in.get(url, params={"page": 2, "limit": 2})).json()
    assert ([item["name"] for item in page["items"]], page["total"], page["has_more"]) == (
        ["알파 회의"],
        3,
        False,
    )
    assert (await logged_in.get(url, params={"status": "bogus"})).status_code == 422


async def test_rename_and_delete_documents(logged_in: AsyncClient, db: Database) -> None:
    kb = await create_kb(logged_in)
    document = (await add_text(logged_in, kb["id"]))["documents"][0]
    url = f"{BASE}/{kb['id']}/documents/{document['id']}"

    renamed = await logged_in.patch(url, json={"name": "새 이름"})
    await set_document(db, document["id"], archived=True, indexing_status="completed")
    archived = await logged_in.patch(url, json={"name": "또 바꿈"})
    deleted = await logged_in.delete(url, headers=FETCH)

    assert renamed.json()["name"] == "새 이름"
    assert error_code(archived, 400) == "document_archived"
    assert deleted.status_code == 204
    assert (await logged_in.get(url)).status_code == 404
    assert (await logged_in.delete(url, headers=FETCH)).status_code == 404
    kb_now = (await logged_in.get(f"{BASE}/{kb['id']}")).json()
    assert kb_now["document_count"] == 0
    # 행은 지식 레인의 delete 작업이 지울 때까지 deleted_at만 찍혀 남는다.
    async with db.read() as session:
        row = (
            await session.execute(
                text("SELECT deleted_at FROM kb_document WHERE id = :id"), {"id": document["id"]}
            )
        ).scalar_one()
    assert row is not None


async def test_segments_are_units_waiting_for_the_index(
    logged_in: AsyncClient, db: Database
) -> None:
    kb = await create_kb(logged_in)
    waiting = (await add_text(logged_in, kb["id"], name="대기", body="x"))["documents"][0]
    document = await completed_document(logged_in, db, kb["id"], body="본문")
    url = segments_url(kb["id"], document["id"])

    blocked = await logged_in.post(segments_url(kb["id"], waiting["id"]), json={"content": "a"})
    first = await logged_in.post(url, json={"content": "첫 청크"})
    second = await logged_in.post(url, json={"content": "두 번째 청크"})
    answer = await logged_in.post(url, json={"content": "q", "answer": "a"})
    blank = await logged_in.post(url, json={"content": "   "})
    long = await logged_in.post(url, json={"content": "가" * 2000})

    assert error_code(blocked, 400) == "document_indexing"
    assert first.status_code == 201, first.text
    one, two = first.json(), second.json()
    assert (one["position"], two["position"]) == (1, 2)
    assert two["unit_no"] == one["unit_no"] + 1
    assert (one["status"], one["created_by"], one["edited"]) == ("waiting", "customized", False)
    assert one["index_state"] == {
        "fts": "pending",
        "vector": "pending",
        "embedding_version": None,
        "embedded_at": None,
    }
    assert one["tokens"] > 0 and one["word_count"] == len("첫 청크")
    assert problem_types(answer) == {"answer": "answer_not_allowed"}
    assert problem_types(blank) == {"content": "content_blank"}
    assert problem_types(long) == {"content": "content_too_long"}
    listed = (await logged_in.get(url)).json()
    assert [item["content"] for item in listed["items"]] == ["첫 청크", "두 번째 청크"]
    assert (await logged_in.get(url, params={"keyword": "두 번째"})).json()["total"] == 1
    doc = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{document['id']}")).json()
    assert (doc["total_segments"], doc["completed_segments"]) == (2, 0)
    assert doc["word_count"] == len("첫 청크") + len("두 번째 청크")
    assert (await logged_in.get(f"{BASE}/{kb['id']}")).json()["segment_count"] == 2


async def test_editing_a_segment_marks_its_unit_pending(
    logged_in: AsyncClient, db: Database
) -> None:
    kb = await create_kb(logged_in)
    document = await completed_document(logged_in, db, kb["id"])
    url = segments_url(kb["id"], document["id"])
    created = (await logged_in.post(url, json={"content": "고치기 전"})).json()
    await set_segment_status(db, created["id"], "completed")
    seg_url = f"{url}/{created['id']}"

    toggled = await logged_in.patch(seg_url, json={"enabled": False})
    edited = await logged_in.patch(seg_url, json={"content": "고친 뒤"})
    toggle_waiting = await logged_in.patch(seg_url, json={"enabled": True})

    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["enabled"] is False
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["content"], body["edited"], body["status"]) == ("고친 뒤", True, "waiting")
    assert body["index_state"]["fts"] == "pending" and body["index_state"]["vector"] == "pending"
    assert body["unit_no"] == created["unit_no"]
    # 색인이 끝나지 않은 청크는 켜고 끄지 못한다.
    assert error_code(toggle_waiting, 400) == "segment_not_indexed"
    async with db.read() as session:
        hashes = (
            await session.execute(
                text("SELECT content_hash FROM kb_segment WHERE id = :id"), {"id": created["id"]}
            )
        ).scalar_one()
    assert hashes == hashlib.sha256("고친 뒤".encode()).hexdigest()
    deleted = await logged_in.delete(seg_url, headers=FETCH)
    assert deleted.status_code == 204
    assert (await logged_in.get(seg_url)).status_code == 404
    doc = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{document['id']}")).json()
    assert doc["total_segments"] == 0


async def test_qa_segments_need_an_answer(logged_in: AsyncClient, db: Database) -> None:
    kb = await create_kb(logged_in, doc_form="qa_model", indexing_technique="economy")
    document = await completed_document(logged_in, db, kb["id"])
    url = segments_url(kb["id"], document["id"])

    missing = await logged_in.post(url, json={"content": "질문"})
    created = await logged_in.post(url, json={"content": "환불 기간은?", "answer": "7일"})
    answered = await logged_in.patch(f"{url}/{created.json()['id']}", json={"answer": "14일"})

    assert problem_types(missing) == {"answer": "answer_required"}
    body = created.json()
    assert (body["answer"], body["word_count"]) == ("7일", len("환불 기간은?") + len("7일"))
    assert body["index_state"]["vector"] == "skip"
    assert answered.json()["answer"] == "14일"
    assert answered.json()["index_state"]["fts"] == "pending"


async def test_parent_child_segments(logged_in: AsyncClient, db: Database) -> None:
    kb = await create_kb(logged_in, doc_form="hierarchical_model")
    plain = await create_kb(logged_in)
    document = await completed_document(logged_in, db, kb["id"])
    plain_doc = await completed_document(logged_in, db, plain["id"])
    url = segments_url(kb["id"], document["id"])

    parent = (await logged_in.post(url, json={"content": "부모 청크 전체 글"})).json()
    children_url = f"{url}/{parent['id']}/children"
    child = await logged_in.post(children_url, json={"content": "자식 하나"})
    second = await logged_in.post(children_url, json={"content": "자식 둘"})
    too_long = await logged_in.post(children_url, json={"content": "나" * 400})
    edited = await logged_in.patch(
        f"{children_url}/{child.json()['id']}", json={"content": "고친 자식"}
    )
    plain_parent = (
        await logged_in.post(segments_url(plain["id"], plain_doc["id"]), json={"content": "x"})
    ).json()
    not_tree = await logged_in.get(
        f"{segments_url(plain['id'], plain_doc['id'])}/{plain_parent['id']}/children"
    )

    assert parent["unit_no"] is None
    assert parent["index_state"]["fts"] == "skip" and parent["index_state"]["vector"] == "skip"
    # 손으로 넣은 부모도 문서의 자식 규칙으로 자식을 나눠 붙인다(부모만으로는 검색에 안 잡힌다).
    assert [c["content"] for c in parent["child_chunks"]] == ["부모 청크 전체 글"]
    assert child.status_code == 201, child.text
    assert second.json()["unit_no"] == child.json()["unit_no"] + 1
    assert (child.json()["position"], second.json()["position"]) == (2, 3)
    assert problem_types(too_long) == {"content": "content_too_long"}
    assert edited.json()["content"] == "고친 자식"
    assert edited.json()["created_by"] == "customized"
    assert error_code(not_tree, 400) == "not_hierarchical"
    listed = (await logged_in.get(url)).json()["items"]
    assert [c["content"] for c in listed[0]["child_chunks"]] == [
        "부모 청크 전체 글",
        "고친 자식",
        "자식 둘",
    ]
    # 부모 글을 고쳐도 자식은 그대로다.
    await logged_in.patch(f"{url}/{parent['id']}", json={"content": "고친 부모"})
    assert len((await logged_in.get(children_url)).json()["items"]) == 3
    removed = await logged_in.delete(f"{children_url}/{second.json()['id']}", headers=FETCH)
    assert removed.status_code == 204
    await logged_in.delete(f"{url}/{parent['id']}", headers=FETCH)
    async with db.read() as session:
        left = (await session.execute(text("SELECT count(*) FROM kb_child_chunk"))).scalar_one()
    assert left == 0


async def test_paths_must_match_their_parents(logged_in: AsyncClient, db: Database) -> None:
    first = await create_kb(logged_in)
    second = await create_kb(logged_in)
    document = await completed_document(logged_in, db, first["id"])
    other_doc = await completed_document(logged_in, db, first["id"], body="다른 문서")
    segment = (
        await logged_in.post(segments_url(first["id"], document["id"]), json={"content": "x"})
    ).json()

    wrong_kb = await logged_in.get(f"{BASE}/{second['id']}/documents/{document['id']}")
    wrong_doc = await logged_in.get(f"{segments_url(first['id'], other_doc['id'])}/{segment['id']}")
    unknown = await logged_in.get(f"{BASE}/{uuid.uuid4()}/documents")

    assert wrong_kb.status_code == 404
    assert wrong_doc.status_code == 404
    assert unknown.status_code == 404


async def test_repositories_stay_in_their_workspace(logged_in: AsyncClient, db: Database) -> None:
    kb = await create_kb(logged_in)
    document = (await add_text(logged_in, kb["id"]))["documents"][0]
    other = uuid.uuid4()

    async with db.read() as session:
        assert await KnowledgeBaseRepository(session, other).get_active(uuid.UUID(kb["id"])) is None
        assert (
            await KbDocumentRepository(session, other).get_live(
                uuid.UUID(kb["id"]), uuid.UUID(document["id"])
            )
            is None
        )


async def test_metadata_fields_and_values(logged_in: AsyncClient, db: Database) -> None:
    kb = await create_kb(logged_in)
    one = (await add_text(logged_in, kb["id"], name="하나", body="1"))["documents"][0]
    two = (await add_text(logged_in, kb["id"], name="둘", body="2"))["documents"][0]
    meta = f"{BASE}/{kb['id']}/metadata"

    team = (await logged_in.post(meta, json={"name": "team", "type": "string"})).json()
    year = (await logged_in.post(meta, json={"name": "year", "type": "number"})).json()
    due = (await logged_in.post(meta, json={"name": "due", "type": "time"})).json()
    reserved = await logged_in.post(meta, json={"name": "uploader", "type": "string"})
    duplicate = await logged_in.post(meta, json={"name": "team", "type": "number"})
    bad_name = await logged_in.post(meta, json={"name": "Team", "type": "string"})

    assert error_code(reserved, 422) == "metadata_name_reserved"
    assert error_code(duplicate, 409) == "metadata_field_exists"
    assert bad_name.status_code == 422
    values_url = f"{BASE}/{kb['id']}/documents/metadata"
    written = await logged_in.post(
        values_url,
        json={
            "document_ids": [one["id"]],
            "values": [
                {"field_id": team["id"], "value": "기획"},
                {"field_id": year["id"], "value": 2026},
                {"field_id": due["id"], "value": "2026-11-30T09:00:00+09:00"},
            ],
            "apply_to_all": True,
        },
    )
    assert written.status_code == 200, written.text
    values = {item["name"]: item["value"] for item in written.json()["items"][0]["doc_metadata"]}
    assert values == {"team": "기획", "year": 2026.0, "due": "2026-11-30T00:00:00Z"}
    # apply_to_all을 끄면 값이 있는 문서(one)만 바뀐다.
    only_existing = await logged_in.post(
        values_url,
        json={
            "document_ids": [one["id"], two["id"]],
            "values": [{"field_id": team["id"], "value": "개발"}],
        },
    )
    items = {item["document_id"]: item["doc_metadata"] for item in only_existing.json()["items"]}
    assert [v["value"] for v in items[one["id"]] if v["name"] == "team"] == ["개발"]
    assert items[two["id"]] == []
    invalid = await logged_in.post(
        values_url,
        json={
            "document_ids": [one["id"]],
            "values": [
                {"field_id": year["id"], "value": "많이"},
                {"field_id": due["id"], "value": "2026-11-30"},
                {"field_id": team["id"], "value": True},
            ],
        },
    )
    assert invalid.status_code == 422
    cleared = await logged_in.post(
        values_url,
        json={"document_ids": [one["id"]], "values": [{"field_id": year["id"], "value": None}]},
    )
    assert "year" not in {v["name"] for v in cleared.json()["items"][0]["doc_metadata"]}
    fields = (await logged_in.get(meta)).json()
    assert [(f["name"], f["use_count"]) for f in fields["items"]] == [
        ("team", 1),
        ("year", 0),
        ("due", 1),
    ]
    renamed = await logged_in.patch(f"{meta}/{team['id']}", json={"name": "department"})
    assert renamed.json()["name"] == "department"
    assert (await logged_in.delete(f"{meta}/{due['id']}", headers=FETCH)).status_code == 204
    doc = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{one['id']}")).json()
    assert [v["name"] for v in doc["doc_metadata"]] == ["department"]
    missing_doc = await logged_in.post(
        values_url,
        json={
            "document_ids": [str(uuid.uuid4())],
            "values": [{"field_id": team["id"], "value": "x"}],
        },
    )
    assert missing_doc.status_code == 404


async def test_built_in_fields_are_filled_and_read_only(logged_in: AsyncClient) -> None:
    kb = await create_kb(logged_in)
    document = (await add_text(logged_in, kb["id"], name="처음 이름"))["documents"][0]
    meta = f"{BASE}/{kb['id']}/metadata"

    enabled = await logged_in.post(f"{meta}/built-in/enable", headers=FETCH)
    later = (await add_text(logged_in, kb["id"], name="나중 문서", body="later"))["documents"][0]
    await logged_in.patch(
        f"{BASE}/{kb['id']}/documents/{document['id']}", json={"name": "바꾼 이름"}
    )

    assert enabled.status_code == 200, enabled.text
    fields = {item["name"]: item for item in enabled.json()["items"]}
    assert set(fields) == {"document_name", "uploader", "upload_date", "last_update_date", "source"}
    assert enabled.json()["built_in_fields_enabled"] is True
    first = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{document['id']}")).json()
    values = {item["name"]: item["value"] for item in first["doc_metadata"]}
    assert values["document_name"] == "바꾼 이름"
    assert values["source"] == "paste_text"
    assert values["uploader"]
    later_values = {item["name"]: item["value"] for item in later["doc_metadata"]}
    assert later_values["document_name"] == "나중 문서"
    field_id = fields["source"]["id"]
    assert (
        error_code(await logged_in.patch(f"{meta}/{field_id}", json={"name": "origin"}), 400)
        == "metadata_built_in"
    )
    assert (
        error_code(await logged_in.delete(f"{meta}/{field_id}", headers=FETCH), 400)
        == "metadata_built_in"
    )
    edit = await logged_in.post(
        f"{BASE}/{kb['id']}/documents/metadata",
        json={"document_ids": [document["id"]], "values": [{"field_id": field_id, "value": "x"}]},
    )
    assert error_code(edit, 400) == "metadata_built_in"

    disabled = await logged_in.post(f"{meta}/built-in/disable", headers=FETCH)

    assert disabled.json() == {"items": [], "built_in_fields_enabled": False}
    after = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{document['id']}")).json()
    assert after["doc_metadata"] == []


@pytest.mark.parametrize("path", ["", "/documents"])
async def test_knowledge_routes_need_login(client: AsyncClient, path: str) -> None:
    response = await client.get(f"{BASE}/{uuid.uuid4()}{path}" if path else BASE)

    assert response.status_code == 401


async def test_a_knowledge_base_key_is_refused_by_agent_and_flow_routes(
    logged_in: AsyncClient, db: Database
) -> None:
    # 지식 베이스 키의 datasets 경로는 M23이다. 그 전에도 인증은 되고 다른 대상의 경로는 400이다.
    kb = await create_kb(logged_in)
    raw = "sk-wf-" + "k" * 43
    async with db.write() as session:
        workspace = await WorkspaceRepository(session).get_default()
        assert workspace is not None
        ApiKeyRepository(session, workspace.id).add(
            ApiKey(
                id=new_id(),
                knowledge_base_id=uuid.UUID(kb["id"]),
                name="kb",
                key_prefix=raw[:12],
                key_hash=hash_token(raw),
                scopes=["knowledge"],
            )
        )
    headers = {"Authorization": f"Bearer {raw}"}

    chat = await logged_in.post(
        "/api/v1/chat-messages",
        headers=headers,
        json={"query": "안녕", "user": "u1", "response_mode": "blocking"},
    )
    run = await logged_in.post(
        "/api/v1/workflows/run",
        headers=headers,
        json={"inputs": {}, "user": "u1", "response_mode": "blocking"},
    )

    for response in (chat, run):
        assert error_code(response, 400) == "wrong_target"
        assert response.json()["error"]["details"]["key_target"] == "knowledge"
