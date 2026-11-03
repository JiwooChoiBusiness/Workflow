"""실제 임베딩으로 합성 평가셋을 돈다(3단계 설계 K3, M23). `uv run pytest -m live`로만 돈다.

문단 48개를 문서 48개로 올려 지식 레인이 색인하고(문서 임베딩 48건), 평가 실행이 질문 53개를
하이브리드 기본값으로 검색한다(질의 임베딩 53건). 무료 등급 리미터 안이라 몇 분 걸릴 수 있다. 키는
앱 설정(.env의 GEMINI_API_KEYS)으로만 받고 없으면 건너뛴다. 데이터는 임시 폴더에 둔다.
"""

import asyncio
import csv
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import sysinfo
from app.config import REPO_ROOT, Settings, get_settings
from app.logsetup import configure_logging
from app.main import create_app

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "knowledge"
BASE = "/api/console/knowledge"


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


async def test_the_synthetic_set_with_real_embeddings(
    live_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sysinfo, "startup_checks", lambda _settings: [])
    with (FIXTURES / "ko_eval_docs.csv").open(encoding="utf-8", newline="") as file:
        paragraphs = {row["name"]: row["text"] for row in csv.DictReader(file)}
    root = Path(tempfile.mkdtemp(prefix="wfe")).resolve()
    settings = live_settings.model_copy(
        update={"data_dir": root / "data", "web_dist_dir": root / "no-dist", "llm_fake": False}
    )
    application = create_app(settings)
    try:
        async with (
            application.router.lifespan_context(application),
            # 평가 실행 하나가 질의 53개를 실제로 임베딩한다(분당 한도 대기 포함).
            AsyncClient(
                transport=ASGITransport(app=application),
                base_url="http://127.0.0.1:8500",
                timeout=600,
            ) as client,
        ):
            account = {"email": "owner@example.com", "password": "correct-horse-battery"}
            await client.post("/api/console/setup", json=account)
            await client.post("/api/console/auth/login", json=account)
            created = await client.post(
                BASE, json={"name": "합성 평가셋", "data_policy": {"acknowledged": True}}
            )
            assert created.status_code == 201, created.text
            kb_id = created.json()["id"]
            for name, text in paragraphs.items():
                response = await client.post(
                    f"{BASE}/{kb_id}/documents", json={"name": name, "text": text}
                )
                assert response.status_code == 201, response.text
            deadline = time.monotonic() + 15 * 60
            while True:
                listed = await client.get(f"{BASE}/{kb_id}/documents", params={"limit": 100})
                statuses = [item["indexing_status"] for item in listed.json()["items"]]
                if all(status in ("completed", "error") for status in statuses):
                    break
                assert time.monotonic() < deadline, statuses
                await asyncio.sleep(2)
            imported = await client.post(
                f"{BASE}/{kb_id}/eval/cases/import",
                json={"csv": (FIXTURES / "ko_eval.csv").read_text(encoding="utf-8")},
            )
            run = await client.post(f"{BASE}/{kb_id}/eval/run", json={})
    finally:
        configure_logging("INFO")
        shutil.rmtree(root, ignore_errors=True)

    assert statuses == ["completed"] * len(paragraphs)
    assert imported.json()["created"] == 53, imported.text
    assert run.status_code == 200, run.text
    body = run.json()
    misses = [(case["question"], case["rank"]) for case in body["cases"] if case["rank"] != 1]
    print("live eval", body["metrics"], "misses", misses, "warnings", body["warnings"])
    assert body["warnings"] == []
    # K3: 하이브리드 기본값(후보 50 안 min-max 가중합 a=0.7). 저장된 벡터 재실측은 R@1 1.000이었다.
    assert body["metrics"]["r1"] >= 0.96
    assert body["metrics"]["r3"] >= 0.96
