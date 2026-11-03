"""기본 시험 앱(가짜 키를 든 GeminiAdapter)에서 지식 기능이 실제 Gemini를 부르지 않는다.

지식 실행 객체는 채팅 어댑터의 전송을 그대로 받으므로, 시험에서는 tests/conftest.py가 실제 HTTP
전송 대신 가짜 임베더를 준다. 그래도 실제 네트워크로 나가려는 호출이 있으면 그 conftest가 시험을
실패시킨다.
"""

import asyncio
import time
import uuid

from fastapi import FastAPI
from httpx import AsyncClient

from app.llm.embeddings import EmbeddingConfig
from app.llm.gemini_adapter import GeminiAdapter

BASE = "/api/console/knowledge"


async def test_ingest_and_chunk_edits_stay_offline(logged_in: AsyncClient, app: FastAPI) -> None:
    assert isinstance(app.state.llm, GeminiAdapter)
    kb = (
        await logged_in.post(BASE, json={"name": "오프라인", "data_policy": {"acknowledged": True}})
    ).json()
    created = await logged_in.post(
        f"{BASE}/{kb['id']}/documents", json={"name": "글", "text": "회의록 정리 규칙"}
    )
    document_id = created.json()["documents"][0]["id"]
    deadline = time.monotonic() + 20
    while True:
        document = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{document_id}")).json()
        if document["indexing_status"] in ("completed", "error"):
            break
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    segment = (await logged_in.get(f"{BASE}/{kb['id']}/documents/{document_id}/segments")).json()[
        "items"
    ][0]
    edited = await logged_in.patch(
        f"{BASE}/{kb['id']}/documents/{document_id}/segments/{segment['id']}",
        json={"content": "고친 회의록 규칙"},
    )
    runtime = app.state.knowledge
    vectors = await runtime.vectors.count(uuid.UUID(kb["id"]))
    query = await runtime.embeddings.embed_query("회의록", EmbeddingConfig())

    assert document["indexing_status"] == "completed", document["error"]
    assert edited.json()["index_state"]["vector"] == "ok"
    assert vectors == 1 and len(query) == 768 * 4
