"""files 데이터 소스의 실제 DuckDB(3단계 설계 10.2, 10.5, 보안 확인표 KS3·KS4·KS5, 17.3-8).

"런타임 준비"가 샌드박스 파이썬으로 duckdb 휠과 excel·encodings 확장을 cache/kb-sql에
받고, csv·cp949 csv·xlsx(한국어 시트·날짜)를 질의 파일로 옮긴다. 질의 파일은 읽기 전용으로
열고 외부 접근을 끈 뒤 설정을 잠그므로, 검증을 건너뛴 SQL(러너 직접 호출)도 쓰기·파일
읽기·확장 로드가 실행 단계에서 막힌다. 숨긴 열은 질의 파일에 없다. 폭주 질의는
timeout_sec에 끊기고 프로세스가 남지 않는다.

런타임은 모듈이 한 번만 받도록 데이터 폴더를 모듈 안에서 같이 쓴다. PyPI에 닿지 않으면
건너뛴다. 기본 실행에서 빠지고 `uv run pytest -m windows`로 돈다. 환경변수 KB_TEST_SQL_RUNTIME에
폴더를 주면 거기 받아 둔 런타임을 복사해 쓰고, 없으면 이 모듈이 받은 것을 그 폴더에 남긴다(CI의
windows 잡이 actions/cache로 이 폴더를 보존한다).
"""

import asyncio
import datetime
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import sysinfo
from app.config import Settings
from app.knowledge.sql.runner import (
    EXTENSIONS_MARKER,
    QueryOutput,
    QueryRequest,
    SqlRunFailed,
    extensions_dir,
    run_query,
    runtime_dir,
)
from app.logsetup import configure_logging
from app.main import create_app
from app.services.sql_runtime import SqlRuntime
from app.workflow.extract_runner import READY_MARKER

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows 샌드박스 파이썬"),
]

BASE = "/api/console/sql-sources"
FETCH = {"X-Requested-With": "fetch"}
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REGIONS = ["서울", "부산", "대구", "광주"]
# 데이터 폴더에 공백·괄호·한글을 넣어 allowed_paths와 read_csv가 그런 경로를 받는지 본다(사용자 폴더
# 이름 ElonChoi(최지우)와 같은 모양).
DATA_NAME = "데이터 (시험)"


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
    """다 받은 런타임(패키지와 확장 폴더)을 복사한다. 원본이 덜 받았거나 대상에 있으면 그만둔다."""
    if not runtime_complete(source) or runtime_complete(target):
        return
    for folder in (runtime_dir(source), extensions_dir(source)):
        shutil.copytree(folder, target / folder.name, dirs_exist_ok=True)


@pytest.fixture(scope="module")
def root() -> Iterator[Path]:
    # pip 임시 폴더가 경로 길이 260자를 넘지 않게 실제 DATA_DIR처럼 짧은 경로를 쓴다.
    path = Path(tempfile.mkdtemp(prefix="wfq")).resolve()
    kb_sql = path / DATA_NAME / "cache" / "kb-sql"
    cache = os.environ.get("KB_TEST_SQL_RUNTIME")
    if cache:
        copy_runtime(Path(cache), kb_sql)
    yield path
    if cache:
        copy_runtime(kb_sql, Path(cache))
    shutil.rmtree(path, ignore_errors=True)


@asynccontextmanager
async def running(root: Path) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    if not pypi_reachable():
        pytest.skip("PyPI에 닿지 않아 DuckDB를 받을 수 없다")
    settings = Settings.model_validate({"data_dir": root / DATA_NAME, "web_dist_dir": root / "x"})
    checks = sysinfo.startup_checks
    sysinfo.startup_checks = lambda _settings: []  # type: ignore[assignment]
    application = create_app(settings)
    account = {"email": "owner@example.com", "password": "correct-horse-battery"}
    try:
        async with (
            application.router.lifespan_context(application),
            AsyncClient(
                transport=ASGITransport(app=application), base_url="http://127.0.0.1:8500"
            ) as client,
        ):
            # 모듈의 앞 시험이 이미 계정을 만들었으면 409다.
            await client.post("/api/console/setup", json=account)
            login = await client.post("/api/console/auth/login", json=account)
            assert login.status_code == 200, login.text
            yield application, client
    finally:
        sysinfo.startup_checks = checks  # type: ignore[assignment]
        configure_logging("INFO")


async def ensure_runtime(client: AsyncClient) -> dict[str, Any]:
    status = (await client.get("/api/console/sql-runtime")).json()
    if status["state"] == "ready":
        return status
    started = time.monotonic()
    accepted = await client.post("/api/console/sql-runtime/prepare", json={})
    assert accepted.status_code == 202, accepted.text
    while status["state"] != "ready":
        assert time.monotonic() - started < 900, status
        await asyncio.sleep(0.5)
        status = (await client.get("/api/console/sql-runtime")).json()
        assert status["state"] != "failed", status
    print(
        f"\n[17.3-8] 런타임 준비 {time.monotonic() - started:.1f}초, "
        f"패키지+확장 {status['bytes'] / 2**20:.1f}MB"
    )
    return status


def column(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def xlsx_cell(ref: str, value: object) -> str:
    if isinstance(value, datetime.date):
        serial = (value - datetime.date(1899, 12, 30)).days
        # 스타일 1은 날짜 서식(numFmtId 14)이다.
        return f'<c r="{ref}" s="1"><v>{serial}</v></c>'
    if isinstance(value, int | float):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def make_xlsx(sheet: str, rows: list[list[object]]) -> bytes:
    body = "".join(
        f'<row r="{number}">'
        + "".join(xlsx_cell(f"{column(index)}{number}", value) for index, value in enumerate(row))
        + "</row>"
        for number, row in enumerate(rows, start=1)
    )
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    package = "http://schemas.openxmlformats.org/package/2006"
    sheet_type = "application/vnd.openxmlformats-officedocument.spreadsheetml"
    parts = {
        "[Content_Types].xml": (
            f'{head}<Types xmlns="{package}/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{sheet_type}.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            f'ContentType="{sheet_type}.worksheet+xml"/>'
            f'<Override PartName="/xl/styles.xml" ContentType="{sheet_type}.styles+xml"/></Types>'
        ),
        "_rels/.rels": (
            f'{head}<Relationships xmlns="{package}/relationships">'
            f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>"
        ),
        "xl/workbook.xml": (
            f'{head}<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>'
            f'<sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            f'{head}<Relationships xmlns="{package}/relationships">'
            f'<Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{REL}/styles" Target="styles.xml"/></Relationships>'
        ),
        "xl/styles.xml": (
            f'{head}<styleSheet xmlns="{MAIN}"><fonts count="1"><font/></fonts>'
            '<fills count="1"><fill/></fills><borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0"/>'
            '<xf numFmtId="14" applyNumberFormat="1"/></cellXfs></styleSheet>'
        ),
        "xl/worksheets/sheet1.xml": (
            f'{head}<worksheet xmlns="{MAIN}"><sheetData>{body}</sheetData></worksheet>'
        ),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def sales_csv(rows: int) -> bytes:
    lines = ["id,region,amount,card,sold_on"]
    for index in range(1, rows + 1):
        day = datetime.date(2024, 1, 1) + datetime.timedelta(days=index % 365)
        lines.append(
            f"{index},{REGIONS[index % 4]},{index * 10.5},4111-0000-0000-{index:04d},{day}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


async def ready_source(client: AsyncClient) -> dict[str, Any]:
    """매출(csv 1,000행)·지점(cp949 csv)·장부(xlsx) 세 표의 files 소스. 모듈에서 한 번 만든다."""
    listed = (await client.get(BASE)).json()["items"]
    found = next((item for item in listed if item["slug"] == "sales"), None)
    if found is not None:
        return (await client.get(f"{BASE}/{found['id']}")).json()
    created = await client.post(
        BASE,
        json={
            "name": "매출 파일",
            "slug": "sales",
            "kind": "files",
            "data_policy": {"acknowledged": True},
        },
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    branches = "지점,담당자\n강남,김하나\n해운대,이두리\n".encode("cp949")
    book = make_xlsx(
        "매출 2024",
        [
            ["지역", "금액", "판매일"],
            ["서울", 100, datetime.date(2024, 1, 2)],
            ["부산", 250.5, datetime.date(2024, 3, 4)],
        ],
    )
    uploaded = await client.post(
        f"{BASE}/{source_id}/files",
        files=[
            ("files", ("sales.csv", sales_csv(1000), "text/csv")),
            ("files", ("branches.csv", branches, "text/csv")),
            ("files", ("book.xlsx", book, "application/octet-stream")),
        ],
        headers=FETCH,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert [item["table"] for item in uploaded.json()["files"]] == ["sales", "branches", "book"]
    prepared = await client.post(f"{BASE}/{source_id}/prepare", json={})
    assert prepared.status_code == 200, prepared.text
    return prepared.json()


async def query(client: AsyncClient, source_id: str, sql: str, **body: Any) -> dict[str, Any]:
    response = await client.post(f"{BASE}/{source_id}/query", json={"sql": sql, **body})
    assert response.status_code == 200, response.text
    return response.json()


async def raw(
    application: FastAPI,
    source_id: str,
    sql: str,
    *,
    max_rows: int = 200,
    timeout_sec: int = 20,
    export_csv: bool = False,
    memory_mb: int | None = None,
) -> tuple[QueryOutput | SqlRunFailed, str | None, str]:
    """검증을 건너뛰고 질의 파일에 바로 돌린다. (결과나 실패, CSV 글, 작업 폴더 이름)."""
    runtime: SqlRuntime = application.state.sql_runtime
    settings: Settings = application.state.settings
    request = QueryRequest(
        engine="duckdb",
        database=runtime.database_path(uuid.UUID(source_id)),
        sql=sql,
        max_rows=max_rows,
        timeout_sec=timeout_sec,
        memory_mb=memory_mb or settings.sql_memory_mb,
        export_csv=export_csv,
        lib=await runtime.ready_lib(),
    )
    async with runtime.work() as handle:
        try:
            output = await run_query(runtime.backend(), handle, request, exec_id="t")
        except SqlRunFailed as exc:
            return exc, None, handle.root.name
        csv = None
        if output.csv_path is not None:
            csv = await asyncio.to_thread(output.csv_path.read_text, "utf-8-sig")
        return output, csv, handle.root.name


def processes_with(marker: str) -> list[str]:
    """명령줄에 marker(작업 폴더 이름)가 든 프로세스 id.

    샌드박스 파이썬은 작업 폴더 안의 스크립트를 돌리므로 명령줄에 그 폴더 이름이 있다.
    """
    powershell = shutil.which("powershell")
    assert powershell is not None
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and "
        f"$_.CommandLine -like '*{marker}*' }} | ForEach-Object {{ $_.ProcessId }}"
    )
    done = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return done.stdout.split()


def original_path(application: FastAPI, source: dict[str, Any], table: str) -> str:
    settings: Settings = application.state.settings
    file_id = next(item["file_id"] for item in source["files"] if item["table"] == table)
    [path] = list((settings.data_dir / "storage" / "sql-sources").rglob(file_id))
    assert "(" in path.as_posix() and "데이터" in path.as_posix()
    return path.as_posix()


async def test_files_are_prepared_into_a_query_file(root: Path) -> None:
    async with running(root) as (application, client):
        status = await ensure_runtime(client)
        assert status["packages_ready"] and status["extensions_ready"]
        source = await ready_source(client)

        assert source["status"]["state"] == "ready", source["status"]
        tables = {table["name"]: table for table in source["schema"]["tables"]}
        assert tables["sales"]["row_count"] == 1000
        assert [c["type"] for c in tables["sales"]["columns"]] == [
            "BIGINT",
            "VARCHAR",
            "DOUBLE",
            "VARCHAR",
            "DATE",
        ]
        # 인코딩 auto는 UTF-8로 못 읽으면 cp949로 다시 읽는다.
        assert tables["branches"]["encoding"] == "cp949"
        assert [(c["name"], c["type"]) for c in tables["book"]["columns"]] == [
            ("지역", "VARCHAR"),
            ("금액", "DOUBLE"),
            ("판매일", "DATE"),
        ]
        materialized = source["materialized"]
        print(
            f"\n[17.3-8] csv 1,000행+cp949+xlsx 준비 {source['schema']['duration_ms']}ms, "
            f"질의 파일 {materialized['bytes'] / 1024:.0f}KB"
        )
        branches = await query(client, source["id"], "SELECT 지점, 담당자 FROM branches ORDER BY 1")
        book = await query(client, source["id"], "SELECT 지역, 판매일 FROM book ORDER BY 판매일")
        totals = await query(
            client,
            source["id"],
            "SELECT region, COUNT(*) AS n FROM sales GROUP BY region ORDER BY region",
        )

        assert branches["rows"] == [
            {"지점": "강남", "담당자": "김하나"},
            {"지점": "해운대", "담당자": "이두리"},
        ]
        assert book["rows"] == [
            {"지역": "서울", "판매일": "2024-01-02"},
            {"지역": "부산", "판매일": "2024-03-04"},
        ]
        assert [row["n"] for row in totals["rows"]] == [250, 250, 250, 250]
        # 런타임이 받은 확장은 오프라인으로 불러 쓰고(외부 접근 끔), 결과 글은 데이터 표식 안이다.
        assert totals["result_text"].startswith('<document name="질의 결과" source="매출 파일">')


async def test_hidden_columns_are_not_in_the_query_file(root: Path) -> None:
    async with running(root) as (application, client):
        await ensure_runtime(client)
        source = await ready_source(client)
        before = source["materialized"]["source_hash"]

        hidden = await client.patch(
            f"{BASE}/{source['id']}/annotations",
            json={"tables": {"sales": {"columns": {"card": {"hidden": True}}}}},
        )

        assert hidden.status_code == 200, hidden.text
        source = hidden.json()
        # 숨김이 바뀌면 질의 파일을 다시 만든다.
        assert source["status"]["state"] == "ready", source["status"]
        assert source["materialized"]["source_hash"] != before
        ddl = (await client.get(f"{BASE}/{source['id']}/schema")).json()["ddl"]
        assert "card" not in ddl and "4111-" not in ddl
        path = original_path(application, source, "sales")
        bypasses = [
            sql.replace("<path>", path)
            for sql in (
                "SELECT * FROM read_csv('<path>')",
                "SELECT * FROM '<path>'",
                "SELECT t.* FROM sales t ORDER BY id",
                "SELECT * FROM (SELECT * FROM sales) ORDER BY id",
            )
        ]
        checked = [await query(client, source["id"], sql) for sql in bypasses]
        direct = await query(client, source["id"], "SELECT card FROM sales")

        assert [(item["ok"], (item["error"] or {}).get("code")) for item in checked] == [
            (False, "function_not_allowed"),
            (False, "table_not_allowed"),
            (True, None),
            (True, None),
        ]
        assert checked[2]["columns"] == ["id", "region", "amount", "sold_on"]
        assert checked[3]["columns"] == ["id", "region", "amount", "sold_on"]
        assert direct["error"]["code"] == "column_hidden"

        # 검증을 건너뛰어도(KS5) 파일 읽기는 막혀 있고 질의 파일에는 열이 없다.
        results = [(await raw(application, source["id"], sql))[0] for sql in bypasses]
        missing = (await raw(application, source["id"], "SELECT card FROM sales"))[0]

        for failed in results[:2]:
            assert isinstance(failed, SqlRunFailed) and failed.code == "sql_error"
            assert "4111-" not in failed.message
        assert isinstance(results[0], SqlRunFailed) and "Permission Error" in results[0].message
        # 저장소 원본에는 확장자가 없어 FROM '<경로>'가 표 이름으로 읽힌다. .csv 사본은 파일 읽기로
        # 해석되고 외부 접근을 꺼 두어 막힌다.
        copy = root / "sales-copy.csv"
        await asyncio.to_thread(shutil.copyfile, path, copy)
        for template in ("SELECT * FROM read_csv('<path>')", "SELECT * FROM '<path>'"):
            sql = template.replace("<path>", copy.as_posix())
            failed = (await raw(application, source["id"], sql))[0]
            assert isinstance(failed, SqlRunFailed) and "Permission Error" in failed.message, sql
        for output in results[2:]:
            assert isinstance(output, QueryOutput)
            assert output.columns == ["id", "region", "amount", "sold_on"]
        assert isinstance(missing, SqlRunFailed) and "card" in missing.message


async def test_write_file_and_extension_statements_fail_in_validation_and_in_duckdb(
    root: Path,
) -> None:
    async with running(root) as (application, client):
        await ensure_runtime(client)
        source = await ready_source(client)
        path = original_path(application, source, "sales")
        out = (root / "out.csv").as_posix()
        other = (root / "other.duckdb").as_posix()
        statements = [
            sql.replace("<path>", path).replace("<out>", out).replace("<other>", other)
            for sql in (
                "INSERT INTO sales SELECT * FROM sales LIMIT 1",
                "DROP TABLE sales",
                "ATTACH '<other>' AS other",
                "VACUUM INTO '<out>'",
                "PRAGMA enable_profiling",
                "LOAD excel",
                "SELECT load_extension('excel')",
                "SELECT * FROM read_csv('<path>')",
                "SELECT * FROM '<path>'",
                "COPY sales TO '<out>'",
                "WITH a AS (SELECT 1) INSERT INTO sales SELECT * FROM sales LIMIT 1",
                "SET threads = 8",
                "INSTALL httpfs",
            )
        ]

        validated = [await query(client, source["id"], sql) for sql in statements]
        executed = [(await raw(application, source["id"], sql))[0] for sql in statements]

        for sql, item in zip(statements, validated, strict=True):
            assert item["ok"] is False and item["error"]["stage"] == "validate", (sql, item)
        for sql, failed in zip(statements, executed, strict=True):
            assert isinstance(failed, SqlRunFailed), sql
            assert failed.code == "sql_error", (sql, failed.message)
        assert not (root / "out.csv").exists() and not (root / "other.duckdb").exists()
        rows = await query(client, source["id"], "SELECT COUNT(*) AS n FROM sales")
        assert rows["rows"] == [{"n": 1000}]


async def test_runaway_queries_stop_at_the_timeout_and_leave_no_process(root: Path) -> None:
    async with running(root) as (application, client):
        await ensure_runtime(client)
        source = await ready_source(client)
        runaway = [
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
            "SELECT COUNT(*) FROM r",
            "SELECT COUNT(*) FROM sales a, sales b, sales c, sales d",
        ]

        for sql in runaway:
            started = time.monotonic()
            failed, _, marker = await raw(application, source["id"], sql, timeout_sec=2)
            elapsed = time.monotonic() - started

            assert isinstance(failed, SqlRunFailed) and failed.code == "sql_timeout", failed
            # 인터럽트로 2초에 끝나고, 응답하지 않으면 5초 뒤 Job Object가 트리를 끝낸다.
            assert elapsed < 12, elapsed
            assert processes_with(marker) == []
            print(f"\n[KS4] DuckDB {sql[:30]}… {elapsed:.2f}초에 끊김")


async def test_rows_over_max_rows_are_truncated_into_a_csv(root: Path) -> None:
    async with running(root) as (application, client):
        await ensure_runtime(client)
        source = await ready_source(client)

        answered = await query(
            client, source["id"], "SELECT id, region FROM sales ORDER BY id", max_rows=50
        )
        output, csv, _ = await raw(
            application,
            source["id"],
            answered["sql_executed"],
            max_rows=50,
            export_csv=True,
        )

        assert answered["sql_executed"].endswith("LIMIT 51")
        assert answered["truncated"] is True and answered["row_count"] == 50
        assert "결과가 50행을 넘어 잘렸다" in answered["result_text"]
        assert isinstance(output, QueryOutput) and output.truncated and output.row_count == 50
        assert csv is not None
        lines = csv.splitlines()
        assert lines[0] == "id,region" and len(lines) == 51
        assert lines[1] == "1,부산"


async def test_large_sorts_spill_into_the_work_folder(root: Path) -> None:
    async with running(root) as (application, client):
        await ensure_runtime(client)
        source = await ready_source(client)
        big = "SELECT i, md5(i::VARCHAR) AS h FROM range(4000000) t(i) ORDER BY h"
        # 128MB 프로세스면 DuckDB는 64MB까지 쓰고 나머지 정렬을 작업 폴더의 .tmp/duckdb에 내린다.
        spilled, _, _ = await raw(application, source["id"], big, max_rows=5, memory_mb=128)
        small, _, _ = await raw(application, source["id"], "SELECT region FROM sales LIMIT 3")

    assert isinstance(spilled, QueryOutput) and spilled.row_count == 5, spilled
    assert spilled.spilled_bytes > 0
    assert isinstance(small, QueryOutput) and small.spilled_bytes == 0
    size = spilled.spilled_bytes / 2**20
    print(f"\n[10.5] DuckDB 정렬 400만 행(64MB 상한) 내려 쓴 크기 {size:.0f}MB")


async def test_quoted_headers_and_sheet_names_become_valid_sql(root: Path) -> None:
    async with running(root) as (application, client):
        await ensure_runtime(client)
        created = await client.post(
            BASE,
            json={"name": "따옴표", "kind": "files", "data_policy": {"acknowledged": True}},
        )
        assert created.status_code == 201, created.text
        source_id = created.json()["id"]
        header = b'id,"a""b","line1\nline2"\n1,x,y\n2,z,w\n'
        book = make_xlsx("O'Brien 시트", [["이름", "값"], ["가", 1], ["나", 2]])
        uploaded = await client.post(
            f"{BASE}/{source_id}/files",
            files=[
                ("files", ("quoted.csv", header, "text/csv")),
                ("files", ("sheet.xlsx", book, "application/octet-stream")),
            ],
            headers=FETCH,
        )
        assert uploaded.status_code == 200, uploaded.text
        sheet = next(item for item in uploaded.json()["files"] if item["format"] == "xlsx")
        patched = await client.patch(
            f"{BASE}/{source_id}",
            json={"files": [{"file_id": sheet["file_id"], "sheet": "O'Brien 시트"}]},
        )
        assert patched.status_code == 200, patched.text
        prepared = await client.post(f"{BASE}/{source_id}/prepare", json={})

        assert prepared.status_code == 200, prepared.text
        tables = {item["name"]: item for item in prepared.json()["schema"]["tables"]}
        assert [c["name"] for c in tables["quoted"]["columns"]] == ["id", 'a"b', "line1\nline2"]
        assert [c["name"] for c in tables["sheet"]["columns"]] == ["이름", "값"]
        quoted = await query(client, source_id, 'SELECT "a""b" FROM quoted ORDER BY id')
        named = await query(client, source_id, "SELECT 이름 FROM sheet ORDER BY 값")
        assert quoted["rows"] == [{'a"b': "x"}, {'a"b': "z"}]
        assert named["rows"] == [{"이름": "가"}, {"이름": "나"}]
