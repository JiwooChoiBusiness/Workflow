"""CSV 데이터 소스로 끝까지 돌리는 text_to_sql 템플릿 챗플로우(3단계 설계 10.3, 10.6, K11).

통합 시험(integration/test_sql_chatflow.py)은 SQLite 소스로 템플릿을 돌린다. 여기서는
"런타임 준비"로 받은 샌드박스 DuckDB가 csv를 질의 파일로 옮기고, 가짜 어댑터 각본이 그 표에
맞는 SQL을 ```sql 블록으로 낸 뒤 답한다. 답변 프롬프트에는 데이터 표식으로 감싼 결과 표가
실리고, 데이터 소스의 max_rows를 넘는 질의는 잘림 표시와 결과 CSV(wf_file)를 남긴다.

런타임은 test_sql_runner_duckdb.py와 같이 받는다. 환경변수 KB_TEST_SQL_RUNTIME에 폴더를
주면 거기 받아 둔 런타임을 복사해 쓰고, 없으면 이 모듈이 받은 것을 그 폴더에 남긴다. PyPI에
닿지 않으면 건너뛴다. 기본 실행에서 빠지고 `uv run pytest -m windows`로 돈다.
"""

import asyncio
import datetime
import os
import shutil
import socket
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app import sysinfo
from app.config import Settings
from app.knowledge.sql.runner import EXTENSIONS_MARKER, extensions_dir, runtime_dir
from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text
from app.logsetup import configure_logging
from app.main import create_app
from app.workflow.extract_runner import READY_MARKER

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows 샌드박스 파이썬"),
]

BASE = "/api/console/sql-sources"
FETCH = {"X-Requested-With": "fetch"}
REGIONS = ["서울", "부산", "대구", "광주"]
MAX_ROWS = 50
TOTALS = (
    "```sql\nSELECT region, COUNT(*) AS n, SUM(amount) AS total FROM sales "
    "GROUP BY region ORDER BY region\n```"
)
TABLE_ANSWER = (
    "| 지역 | 건수 | 합계 |\n| --- | --- | --- |\n| 광주 | 250 | 1315125.0 |\n"
    "| 대구 | 250 | 1312500.0 |\n| 부산 | 250 | 1309875.0 |\n| 서울 | 250 | 1317750.0 |"
)
LISTING = "```sql\nSELECT id, region, amount FROM sales ORDER BY id\n```"
LISTING_ANSWER = "앞의 50건만 표로 정리했습니다. 결과가 잘려서 받은 행은 CSV 파일로 남겼습니다."


def pypi_reachable() -> bool:
    try:
        with socket.create_connection(("pypi.org", 443), timeout=5):
            return True
    except OSError:
        return False


def runtime_complete(kb_sql: Path) -> bool:
    return (runtime_dir(kb_sql) / READY_MARKER).is_file() and (
        extensions_dir(kb_sql) / EXTENSIONS_MARKER
    ).is_file()


def copy_runtime(source: Path, target: Path) -> None:
    if not runtime_complete(source) or runtime_complete(target):
        return
    for folder in (runtime_dir(source), extensions_dir(source)):
        shutil.copytree(folder, target / folder.name, dirs_exist_ok=True)


@pytest.fixture(scope="module")
def root() -> Iterator[Path]:
    # pip 임시 폴더가 경로 길이 260자를 넘지 않게 실제 DATA_DIR처럼 짧은 경로를 쓴다.
    path = Path(tempfile.mkdtemp(prefix="wfc")).resolve()
    kb_sql = path / "data" / "cache" / "kb-sql"
    cache = os.environ.get("KB_TEST_SQL_RUNTIME")
    if cache:
        copy_runtime(Path(cache), kb_sql)
    yield path
    if cache:
        copy_runtime(kb_sql, Path(cache))
    shutil.rmtree(path, ignore_errors=True)


@asynccontextmanager
async def running(root: Path, adapter: FakeAdapter) -> AsyncIterator[AsyncClient]:
    if not pypi_reachable():
        pytest.skip("PyPI에 닿지 않아 DuckDB를 받을 수 없다")
    settings = Settings.model_validate({"data_dir": root / "data", "web_dist_dir": root / "x"})
    checks = sysinfo.startup_checks
    sysinfo.startup_checks = lambda _settings: []  # type: ignore[assignment]
    application = create_app(settings)
    application.state.llm = adapter
    account = {"email": "owner@example.com", "password": "correct-horse-battery"}
    try:
        async with (
            application.router.lifespan_context(application),
            AsyncClient(
                transport=ASGITransport(app=application), base_url="http://127.0.0.1:8500"
            ) as client,
        ):
            await client.post("/api/console/setup", json=account)
            login = await client.post("/api/console/auth/login", json=account)
            assert login.status_code == 200, login.text
            yield client
    finally:
        sysinfo.startup_checks = checks  # type: ignore[assignment]
        configure_logging("INFO")


async def ensure_runtime(client: AsyncClient) -> None:
    status = (await client.get("/api/console/sql-runtime")).json()
    if status["state"] == "ready":
        return
    started = time.monotonic()
    accepted = await client.post("/api/console/sql-runtime/prepare", json={})
    assert accepted.status_code == 202, accepted.text
    while status["state"] != "ready":
        assert time.monotonic() - started < 900, status
        await asyncio.sleep(0.5)
        status = (await client.get("/api/console/sql-runtime")).json()
        assert status["state"] != "failed", status


def sales_csv(rows: int) -> bytes:
    lines = ["id,region,amount,sold_on"]
    for index in range(1, rows + 1):
        day = datetime.date(2024, 1, 1) + datetime.timedelta(days=index % 365)
        lines.append(f"{index},{REGIONS[index % 4]},{index * 10.5},{day}")
    return ("\n".join(lines) + "\n").encode("utf-8")


async def csv_source(client: AsyncClient) -> dict[str, Any]:
    created = await client.post(
        BASE,
        json={
            "name": "매출 파일",
            "slug": "sales",
            "kind": "files",
            "description": "지점별 매출 1,000건",
            "limits": {"max_rows": MAX_ROWS},
            "data_policy": {"acknowledged": True},
        },
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    uploaded = await client.post(
        f"{BASE}/{source_id}/files",
        files=[("files", ("sales.csv", sales_csv(1000), "text/csv"))],
        headers=FETCH,
    )
    assert uploaded.status_code == 200, uploaded.text
    prepared = await client.post(f"{BASE}/{source_id}/prepare", json={})
    assert prepared.status_code == 200, prepared.text
    source = prepared.json()
    assert source["status"]["state"] == "ready", source["status"]
    assert source["dialect"] == "duckdb"
    return source


async def chat(client: AsyncClient, workflow_id: str, text: str) -> dict[str, Any]:
    conversation = (
        await client.post(f"/api/console/workflows/{workflow_id}/preview/conversations", json={})
    ).json()
    sent = await client.post(
        f"/api/console/wf-conversations/{conversation['id']}/messages",
        json={"text": text, "response_mode": "blocking"},
    )
    assert sent.status_code == 200, sent.text
    return sent.json()


async def node_run(client: AsyncClient, run_id: str, node_id: str) -> dict[str, Any]:
    """노드 런 상세(outputs·warnings 전체)."""
    detail = (await client.get(f"/api/console/wf-runs/{run_id}")).json()
    found = next(item for item in detail["node_runs"] if item["node_id"] == node_id)
    response = await client.get(f"/api/console/wf-runs/{run_id}/node-runs/{found['id']}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_text_to_sql_template_answers_from_a_csv_source(root: Path) -> None:
    adapter = FakeAdapter(
        [
            [*fake_text(TOTALS), fake_end()],
            [*fake_text(TABLE_ANSWER), fake_end()],
            [*fake_text(LISTING), fake_end()],
            [*fake_text(LISTING_ANSWER), fake_end()],
        ]
    )
    async with running(root, adapter) as client:
        await ensure_runtime(client)
        source = await csv_source(client)
        created = await client.post(
            "/api/console/workflows",
            json={
                "name": "매출 분석",
                "kind": "chatflow",
                "template": "text_to_sql",
                "sql_source_id": source["id"],
            },
        )
        assert created.status_code == 201, created.text
        workflow_id = created.json()["id"]

        totals = await chat(client, workflow_id, "지역별 매출 건수와 합계를 표로 보여 줘")
        listing = await chat(client, workflow_id, "매출을 번호순으로 전부 보여 줘")

        assert totals["run"]["status"] == "succeeded", totals["run"]
        assert totals["answer"] == TABLE_ANSWER
        write_sql, answer_llm, _, listing_llm = adapter.requests
        assert "-- 데이터 소스: 매출 파일 (DuckDB). SQL은 DuckDB 문법의 SELECT 하나로 쓴다." in (
            write_sql.system
        )
        assert "CREATE TABLE sales (" in write_sql.system
        # 답변 프롬프트에는 실행한 SQL과 데이터 표식으로 감싼 결과 표가 실린다.
        assert '<document name="질의 결과" source="매출 파일">' in answer_llm.system
        assert "| 서울 | 250 | 1317750.0 |" in answer_llm.system
        assert "| 광주 | 250 | 1315125.0 |" in answer_llm.system
        assert f"LIMIT {MAX_ROWS + 1}" in answer_llm.system
        first = await node_run(client, totals["run"]["id"], "run_sql")
        assert first["outputs"]["truncated"] is False and first["outputs"].get("file") is None

        assert listing["run"]["status"] == "succeeded", listing["run"]
        assert listing["answer"] == LISTING_ANSWER
        assert f"결과가 {MAX_ROWS}행을 넘어 잘렸다. 처음 {MAX_ROWS}행만 싣는다." in (
            listing_llm.system
        )
        run_sql = await node_run(client, listing["run"]["id"], "run_sql")
        outputs = run_sql["outputs"]
        assert outputs["truncated"] is True and outputs["row_count"] == MAX_ROWS
        assert outputs["sql_executed"].endswith(f"LIMIT {MAX_ROWS + 1}")
        assert f"truncated: 결과가 {MAX_ROWS}행을 넘어 잘렸다" in run_sql["warnings"]
        file = outputs["file"]
        assert file["source"] == "wf_file" and file["filename"] == "run_sql-result.csv"
        downloaded = await client.get(
            f"/api/console/wf-runs/{listing['run']['id']}/files/{file['file_id']}/download"
        )
        assert downloaded.status_code == 200, downloaded.text
        lines = downloaded.content.decode("utf-8-sig").splitlines()
        assert lines[0] == "id,region,amount" and len(lines) == MAX_ROWS + 1
        assert lines[1] == "1,부산,10.5"
