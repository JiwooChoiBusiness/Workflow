"""실제 Gemini 임베딩으로 청크 300개 문서 하나를 넣는다(3단계 설계 M22, 6.6).

무료 등급 리미터(분당 100건 상한, 인제스트 몫 80건, 20초 조각)를 지키며 끝까지 가는지와 429 없이
끝나는지 본다. 하루 한도 1,000건 중 약 300건을 쓴다. 키는 앱 설정(.env의 GEMINI_API_KEYS)으로만
받고 없으면 건너뛴다. 데이터는 임시 폴더에 둔다. `uv run pytest -m live`로만 돈다.
"""

import asyncio
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import sysinfo
from app.config import REPO_ROOT, Settings, get_settings
from app.llm.embeddings import EmbeddingConfig
from app.logsetup import configure_logging
from app.main import create_app

pytestmark = pytest.mark.live

BASE = "/api/console/knowledge"
CHUNKS = 300


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


def document_text() -> str:
    # 문단 하나가 청크 하나가 되게 짧은 문단을 max_tokens 30으로 나눈다.
    return "\n\n".join(
        f"{index}번 안내: 사내 규정 {index}조는 출장비 정산과 회의록 보관 기한을 정한다."
        for index in range(CHUNKS)
    )


async def test_a_300_chunk_document_is_embedded_with_real_keys(
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
                    BASE,
                    json={
                        "name": "라이브",
                        "data_policy": {"acknowledged": True},
                        "process_rule": {"general": {"max_tokens": 30, "overlap_tokens": 0}},
                    },
                )
            ).json()
            created = await client.post(
                f"{BASE}/{kb['id']}/documents", json={"name": "규정", "text": document_text()}
            )
            document_id = created.json()["documents"][0]["id"]
            deadline = time.monotonic() + 15 * 60
            while True:
                document = (await client.get(f"{BASE}/{kb['id']}/documents/{document_id}")).json()
                if document["indexing_status"] in ("completed", "error"):
                    break
                assert time.monotonic() < deadline, document
                await asyncio.sleep(2)
            runtime = application.state.knowledge
            kb_id = uuid.UUID(kb["id"])
            vectors = await runtime.vectors.count(kb_id)
            query = await runtime.embeddings.embed_query("150번 안내", EmbeddingConfig())
            hits = await runtime.vectors.search(kb_id, query, 5)
    finally:
        configure_logging("INFO")
        shutil.rmtree(root, ignore_errors=True)

    assert document["indexing_status"] == "completed", document.get("error")
    assert document["total_segments"] == CHUNKS == document["completed_segments"]
    assert vectors == CHUNKS
    assert hits and hits[0].cosine > 0.5
