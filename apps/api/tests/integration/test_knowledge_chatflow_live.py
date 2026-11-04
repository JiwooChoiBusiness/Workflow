"""지식 검색 챗플로우의 실제 Gemini 한 턴(3단계 설계 M24).

start → knowledge_retrieval → llm(context) → answer 챗플로우로 한 번 묻는다. 임베딩은 청크 하나와
질의 하나로 2건, 채팅 호출은 1건이다(기본 검색은 하이브리드·가중치라 재순위 호출이 없다). 키는 앱
설정(.env의 GEMINI_API_KEYS)으로만 받고 없으면 건너뛴다. 데이터는 임시 폴더에 둔다.
`uv run pytest -m live`로만 돈다.
"""

import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app import sysinfo
from app.config import REPO_ROOT, Settings, get_settings
from app.logsetup import configure_logging
from app.main import create_app

pytestmark = pytest.mark.live

BASE = "/api/console/knowledge"
TEXT = (
    "환불은 구매일로부터 7일 안에 고객센터에 신청한다. "
    "7일이 지나면 환불 대신 교환만 할 수 있다.\n\n"
    "교환은 구매일로부터 30일 안에 한 번만 할 수 있다."
)
LOW = {"params": {"max_output_tokens": 2048, "reasoning_effort": "low"}}


@pytest.fixture
def live_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    # 상위 conftest가 테스트마다 .env 읽기를 끊어 두므로 이 모듈에서만 되돌린다.
    monkeypatch.setitem(Settings.model_config, "env_file", REPO_ROOT / ".env")
    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()
    if not settings.gemini_api_keys:
        pytest.skip("GEMINI_API_KEYS가 설정에 없어 라이브 테스트를 건너뛴다")
    return settings


def node(node_id: str, node_type: str, config: dict[str, Any], x: float) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "title": node_id,
        "position": {"x": x, "y": 0},
        "config": config,
    }


def rag_graph(kb_id: str) -> dict[str, Any]:
    llm = {
        "model": LOW,
        "prompt": [
            {"role": "system", "text": "참고 자료만 근거로 한국어 한두 문장으로 답한다."},
            {"role": "user", "text": "{{ sys.query }}"},
        ],
        "context": {"var": "knowledge_1.result"},
    }
    ids = ["start", "knowledge_1", "llm_1", "answer_1"]
    return {
        "nodes": [
            node("start", "start", {"fields": []}, 0),
            node("knowledge_1", "knowledge_retrieval", {"kb_ids": [kb_id]}, 200),
            node("llm_1", "llm", llm, 400),
            node("answer_1", "answer", {"text": "{{ llm_1.text }}"}, 600),
        ],
        "edges": [
            {"id": f"e{index}", "source": source, "target": target}
            for index, (source, target) in enumerate(zip(ids, ids[1:], strict=False), start=1)
        ],
    }


async def indexed(client: AsyncClient, kb_id: str, document_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5 * 60
    while True:
        document = (await client.get(f"{BASE}/{kb_id}/documents/{document_id}")).json()
        if document["indexing_status"] in ("completed", "error"):
            return document
        assert time.monotonic() < deadline, document
        await asyncio.sleep(1)


async def chatflow(client: AsyncClient, graph: dict[str, Any]) -> str:
    created = await client.post(
        "/api/console/workflows", json={"name": "환불 챗", "kind": "chatflow"}
    )
    workflow_id = created.json()["id"]
    current = (await client.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/console/workflows/{workflow_id}/draft",
        json={
            "graph": graph,
            "env_vars": [],
            "conversation_vars": [],
            "settings": current["settings"],
            "updated_at": current["updated_at"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert [issue for issue in saved.json()["issues"] if issue["level"] == "error"] == []
    return workflow_id


async def test_a_rag_chatflow_answers_from_real_embeddings_with_citations(
    live_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    root = Path(tempfile.mkdtemp(prefix="wfl")).resolve()
    settings = live_settings.model_copy(
        update={"data_dir": root / "data", "web_dist_dir": root / "no-dist", "llm_fake": False}
    )
    application = create_app(settings)
    try:
        async with (
            application.router.lifespan_context(application),
            AsyncClient(
                transport=ASGITransport(app=application), base_url="http://127.0.0.1:8500"
            ) as client,
        ):
            account = {"email": "owner@example.com", "password": "correct-horse-battery"}
            await client.post("/api/console/setup", json=account)
            await client.post("/api/console/auth/login", json=account)
            kb = (
                await client.post(
                    BASE, json={"name": "환불 규정", "data_policy": {"acknowledged": True}}
                )
            ).json()
            created = await client.post(
                f"{BASE}/{kb['id']}/documents", json={"name": "환불 규정", "text": TEXT}
            )
            assert created.status_code == 201, created.text
            document = await indexed(client, kb["id"], created.json()["documents"][0]["id"])
            workflow_id = await chatflow(client, rag_graph(kb["id"]))
            conversation = (
                await client.post(
                    f"/api/console/workflows/{workflow_id}/preview/conversations", json={}
                )
            ).json()
            sent = await client.post(
                f"/api/console/wf-conversations/{conversation['id']}/messages",
                json={"text": "환불은 며칠 안에 신청해야 해?", "response_mode": "blocking"},
            )
            events = (
                await client.get(f"/api/console/wf-runs/{sent.json()['run']['id']}/event-log")
            ).json()
    finally:
        configure_logging("INFO")
        shutil.rmtree(root, ignore_errors=True)

    assert document["indexing_status"] == "completed", document.get("error")
    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["run"]["status"] == "succeeded", body["run"]["error"]
    cited = body["retriever_resources"]
    assert cited, body
    assert cited[0]["document_name"] == "환불 규정" and cited[0]["score"] > 0
    assert any("7일" in item["content"] for item in cited)
    assert "7" in body["answer"], body["answer"]
    [completed] = [item for item in events["items"] if item["type"] == "workflow.completed"]
    assert [item["segment_id"] for item in completed["data"]["retriever_resources"]] == [
        item["segment_id"] for item in cited
    ]
