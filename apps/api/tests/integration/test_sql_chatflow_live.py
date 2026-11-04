"""text_to_sql 템플릿 챗플로우의 실제 Gemini 한 턴(3단계 설계 M27).

sqlite 데이터 소스(주문 4건)에 지역별 합계를 묻는다. 채팅 호출은 SQL 만들기와 답변 두 번이고, SQL이
거부되거나 실패하면 고치기 갈래로 한 번 더 돈다. 키는 앱 설정(.env의 GEMINI_API_KEYS)으로만 받고
없으면 건너뛴다. 데이터는 임시 폴더에 둔다. `uv run pytest -m live`로만 돈다.
"""

import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import sysinfo
from app.config import REPO_ROOT, Settings, get_settings
from app.logsetup import configure_logging
from app.main import create_app

pytestmark = pytest.mark.live

BASE = "/api/console/sql-sources"
FETCH = {"X-Requested-With": "fetch"}


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


def shop_db(root: Path) -> bytes:
    path = root / "shop.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, region TEXT);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL);
            INSERT INTO customers VALUES (1, '김하나', '서울'), (2, '이두리', '부산'),
                (3, '박세찬', '서울');
            INSERT INTO orders VALUES (1, 1, 120.5), (2, 1, 30.0), (3, 2, 99.0), (4, 3, 250.0);
            """
        )
        conn.commit()
    return path.read_bytes()


async def test_text_to_sql_template_answers_one_turn_with_gemini(
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
            source = (
                await client.post(
                    BASE,
                    json={
                        "name": "주문 DB",
                        "kind": "sqlite",
                        "description": "쇼핑몰 고객과 주문",
                        "data_policy": {"acknowledged": True},
                    },
                )
            ).json()
            uploaded = await client.post(
                f"{BASE}/{source['id']}/files",
                files=[("files", ("shop.db", shop_db(root), "application/octet-stream"))],
                headers=FETCH,
            )
            assert uploaded.status_code == 200, uploaded.text
            prepared = await client.post(f"{BASE}/{source['id']}/prepare", json={})
            assert prepared.status_code == 200, prepared.text
            created = await client.post(
                "/api/console/workflows",
                json={
                    "name": "주문 분석",
                    "kind": "chatflow",
                    "template": "text_to_sql",
                    "sql_source_id": source["id"],
                },
            )
            assert created.status_code == 201, created.text
            workflow_id = created.json()["id"]
            conversation = (
                await client.post(
                    f"/api/console/workflows/{workflow_id}/preview/conversations", json={}
                )
            ).json()
            sent = await client.post(
                f"/api/console/wf-conversations/{conversation['id']}/messages",
                json={
                    "text": "지역별 주문 금액 합계를 표로 보여 줘",
                    "response_mode": "blocking",
                },
            )
            detail = (await client.get(f"/api/console/wf-runs/{sent.json()['run']['id']}")).json()
            logs = (await client.get(f"{BASE}/{source['id']}/query-logs")).json()["items"]
    finally:
        configure_logging("INFO")
        shutil.rmtree(root, ignore_errors=True)

    assert sent.status_code == 200, sent.text
    body = sent.json()
    assert body["run"]["status"] == "succeeded", body["run"]["error"]
    statuses = {item["node_id"]: item["status"] for item in detail["node_runs"]}
    assert "succeeded" in (statuses.get("run_sql"), statuses.get("rerun_sql")), statuses
    executed = [item["sql"] for item in logs if item["status"] == "ok" and item["origin"] == "node"]
    assert executed, logs
    # 서울 합계 400.5, 부산 99.
    assert "400.5" in body["answer"] and "99" in body["answer"], body["answer"]
    print(f"\n[live] SQL: {executed[-1]}\n[live] 답: {body['answer']}")
