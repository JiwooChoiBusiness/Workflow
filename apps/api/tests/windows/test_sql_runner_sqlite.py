"""sqlite 데이터 소스의 실제 러너와 숨김을 뺀 사본(3단계 설계 10.5, KS3·KS4·KS5, 17.3-8).

원본을 mode=ro&immutable=1로 열면 authorizer가 숨긴 표 읽기는 직접이든 뷰를 거치든 DENY, 숨긴 열은
SQLITE_IGNORE(NULL)로 바꾼다. 다만 USING·NATURAL 조인 조건은 SQLite가 안에서 만들어 authorizer를
거치지 않는다(3.45 실측). 그래서 숨김이 있는 소스는 준비가 만든 사본(보이는 표·열과 그것만 읽는
뷰)을 열고, 검증을 건너뛴 SQL도 사본에는 숨긴 값이 없어 엔진에서 막힌다. 검증을 건너뛴 SQL의
쓰기·ATTACH·확장 로드도 실행 단계에서 막히고, 폭주 질의는 진행 핸들러가 timeout_sec에 끊는다.
런타임 설치 없이 샌드박스 파이썬의 sqlite3로 돈다. 기본 실행에서 빠지고 `uv run pytest -m windows`로
돈다.
"""

import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI

from app import sysinfo
from app.config import Settings
from app.knowledge.sql.prepare import CopyTable, build_sqlite_copy
from app.knowledge.sql.runner import QueryOutput, QueryRequest, SqlRunFailed, run_query
from app.knowledge.sql.sources import SourceView, source_view
from app.knowledge.sql.validate import SqlRejected, validate_sql
from app.logsetup import configure_logging
from app.main import create_app
from app.schemas.sql_source import (
    ColumnNote,
    SqlAnnotations,
    SqlColumn,
    SqlSchemaSnapshot,
    SqlTable,
    TableNote,
)
from app.services.sql_runtime import SqlRuntime

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows 샌드박스 파이썬"),
]

TABLES = ["customers", "orders", "numbers", "v_public", "v_contacts"]
HIDDEN = {"customers": ["email", "profile"]}
HIDDEN_TABLES = ["secret", "v_secret"]
COLUMNS = {
    "customers": ["id", "name", "email", "profile"],
    "orders": ["id", "customer_id", "amount"],
    "numbers": ["n"],
    "secret": ["code"],
    "v_public": ["id", "name"],
    "v_contacts": ["name", "contact"],
    "v_secret": ["code"],
}


@pytest.fixture(scope="module")
def root() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="wfs")).resolve()
    database = path / "shop.db"
    with closing(sqlite3.connect(database)) as conn:
        conn.executescript(
            """
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, email TEXT, profile TEXT);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL);
            CREATE TABLE numbers(n INTEGER);
            CREATE TABLE secret(code TEXT);
            CREATE VIEW v_public AS SELECT id, name FROM customers;
            CREATE VIEW v_contacts AS SELECT name, email AS contact FROM customers;
            CREATE VIEW v_secret AS SELECT code FROM secret;
            INSERT INTO secret VALUES ('s3cr3t');
            """
        )
        conn.executemany(
            "INSERT INTO customers VALUES (?, ?, ?, ?)",
            [
                (1, "김하나", "kim@example.com", json.dumps({"tier": "gold", "city": "서울"})),
                (2, "이두리", "lee@example.com", json.dumps({"tier": "silver"})),
            ],
        )
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)", [(1, 1, 120.5), (2, 1, 30.0), (3, 2, 99.0)]
        )
        conn.executemany("INSERT INTO numbers VALUES (?)", [(index,) for index in range(1000)])
        conn.commit()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@asynccontextmanager
async def running(root: Path) -> AsyncIterator[FastAPI]:
    settings = Settings.model_validate({"data_dir": root / "data", "web_dist_dir": root / "x"})
    checks = sysinfo.startup_checks
    sysinfo.startup_checks = lambda _settings: []  # type: ignore[assignment]
    application = create_app(settings)
    try:
        async with application.router.lifespan_context(application):
            yield application
    finally:
        sysinfo.startup_checks = checks  # type: ignore[assignment]
        configure_logging("INFO")


async def raw(
    application: FastAPI,
    database: Path,
    sql: str,
    *,
    hidden: Mapping[str, Sequence[str]] = HIDDEN,
    hidden_tables: Sequence[str] = HIDDEN_TABLES,
    ctes: Sequence[str] = (),
    max_rows: int = 200,
    timeout_sec: int = 20,
    export_csv: bool = False,
) -> tuple[QueryOutput | SqlRunFailed, str | None, str]:
    """검증을 건너뛰고 파일에 바로 돌린다. (결과나 실패, CSV 글, 작업 폴더 이름)."""
    runtime: SqlRuntime = application.state.sql_runtime
    request = QueryRequest(
        engine="sqlite",
        database=database,
        sql=sql,
        max_rows=max_rows,
        timeout_sec=timeout_sec,
        memory_mb=512,
        export_csv=export_csv,
        tables=TABLES,
        ctes=ctes,
        hidden=hidden,
        hidden_tables=hidden_tables,
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


async def rows_of(application: FastAPI, database: Path, sql: str, **options: object) -> QueryOutput:
    output, _, _ = await raw(application, database, sql, **options)  # type: ignore[arg-type]
    assert isinstance(output, QueryOutput), output
    return output


async def failure_of(application: FastAPI, database: Path, sql: str, **options: object) -> str:
    failed, _, _ = await raw(application, database, sql, **options)  # type: ignore[arg-type]
    assert isinstance(failed, SqlRunFailed), (sql, failed)
    assert failed.code == "sql_error", (sql, failed.message)
    return failed.message


def denied(message: str) -> bool:
    # SQLite는 열 읽기 거부를 "access to t.c is prohibited", 그 밖을 "not authorized"로 알린다.
    return "prohibited" in message or "not authorized" in message


def shop_view() -> SourceView:
    snapshot = SqlSchemaSnapshot(
        tables=[
            SqlTable(
                name=name,
                kind="view" if name.startswith("v_") else "table",
                columns=[SqlColumn(name=item, type="") for item in names],
            )
            for name, names in COLUMNS.items()
        ],
        captured_at=datetime.now(UTC),
        source_hash="h",
    )
    notes = SqlAnnotations(
        tables={
            "customers": TableNote(
                columns={name: ColumnNote(hidden=True) for name in HIDDEN["customers"]}
            ),
            **{name: TableNote(hidden=True) for name in HIDDEN_TABLES},
        }
    )
    return source_view("sqlite", snapshot, notes)


def rejected(sql: str) -> str:
    try:
        validate_sql(sql, shop_view(), max_rows=10)
    except SqlRejected as exc:
        return exc.code
    return "allowed"


def processes_with(marker: str) -> list[str]:
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


async def make_copy(application: FastAPI, root: Path) -> Path:
    """준비가 만드는 것과 같은 사본. 숨긴 열·표와 그것을 읽는 뷰를 뺀다."""
    target = root / "copy.sqlite"
    if target.exists():
        return target
    runtime: SqlRuntime = application.state.sql_runtime
    async with runtime.work() as handle:
        copied = await build_sqlite_copy(
            runtime.backend(),
            handle,
            database=root / "shop.db",
            tables=[
                CopyTable("customers", ["id", "name"]),
                CopyTable("orders", COLUMNS["orders"]),
                CopyTable("numbers", ["n"]),
            ],
            views=[("v_public", "CREATE VIEW v_public AS SELECT id, name FROM customers")],
            memory_mb=512,
            exec_id="copy",
        )
        await asyncio.to_thread(shutil.move, str(copied.path), str(target))
    # 두 표의 INTEGER PRIMARY KEY는 rowid 별칭으로 남고 따로 옮길 인덱스는 없다.
    assert copied.skipped == {} and copied.indexes == 0
    return target


async def test_hidden_columns_read_as_null_including_through_views(root: Path) -> None:
    database = root / "shop.db"
    async with running(root) as application:
        star = await rows_of(application, database, "SELECT * FROM customers ORDER BY id")
        alias_star = await rows_of(application, database, "SELECT t.* FROM customers t")
        nested = await rows_of(application, database, "SELECT * FROM (SELECT * FROM customers)")
        through_view = await rows_of(application, database, "SELECT * FROM v_contacts")
        using = "SELECT name FROM customers JOIN (SELECT 'kim@example.com' AS email) USING (email)"
        joined = await rows_of(application, database, using)
        joined_open = await rows_of(application, database, using, hidden={})
        natural = "SELECT name FROM customers NATURAL JOIN (SELECT 'kim@example.com' AS email)"
        joined_natural = await rows_of(application, database, natural)
        on = (
            "SELECT name FROM customers c JOIN (SELECT 'kim@example.com' AS email) g "
            "ON c.email = g.email"
        )
        joined_on = await rows_of(application, database, on)
        each = "SELECT j.key, j.value FROM customers, json_each(customers.profile) AS j"
        unpacked = await rows_of(application, database, each)
        unpacked_open = await rows_of(application, database, each, hidden={})

    # 원본에서는 숨긴 열이 NULL로 읽힌다(사본이 없는 경로의 두 번째 겹).
    assert star.columns == ["id", "name", "email", "profile"]
    assert star.rows == [[1, "김하나", None, None], [2, "이두리", None, None]]
    assert alias_star.rows == star.rows and nested.rows == star.rows
    assert through_view.rows == [["김하나", None], ["이두리", None]]
    assert joined_on.row_count == 0 and joined_open.rows == [["김하나"]]
    assert unpacked.row_count == 0 and unpacked_open.row_count == 3
    # USING·NATURAL 조건은 authorizer를 거치지 않아 실제 값으로 비교된다. 검증이 막고, 실제 질의는
    # 숨김을 뺀 사본에서 돈다(test_the_copy_closes_every_bypass).
    assert rejected(using) == "column_hidden" and rejected(natural) == "column_unresolved"
    print(
        "\n[17.3-8] SQLite SQLITE_IGNORE: SELECT *·t.*·서브쿼리·뷰·ON·json_each는 NULL, "
        f"USING {joined.row_count}행·NATURAL {joined_natural.row_count}행"
        "(조건이 실제 값으로 비교됨)"
    )


async def test_hidden_tables_are_denied_directly_and_through_views(root: Path) -> None:
    database = root / "shop.db"
    async with running(root) as application:
        direct = await failure_of(application, database, "SELECT * FROM secret")
        counted = await failure_of(application, database, "SELECT COUNT(*) FROM secret")
        viewed = await failure_of(application, database, "SELECT * FROM v_secret")
        # 숨기지 않은 뷰라도 숨긴 표를 읽으면 막는다.
        unhidden_view = await failure_of(
            application, database, "SELECT * FROM v_secret", hidden_tables=["secret"]
        )

    for message in (direct, counted, viewed, unhidden_view):
        assert denied(message) and "s3cr3t" not in message
    assert rejected("SELECT * FROM secret") == "table_not_allowed"


async def test_cte_names_cannot_open_catalog_or_hidden_tables(root: Path) -> None:
    database = root / "shop.db"
    master = (
        "SELECT name, sql FROM sqlite_master WHERE 1 IN "
        "(WITH sqlite_master AS (SELECT 1 AS a) SELECT a FROM sqlite_master)"
    )
    shadow = (
        "SELECT COUNT(*) FROM secret WHERE 1 IN "
        "(WITH secret AS (SELECT 1 AS a) SELECT a FROM secret)"
    )
    recursive = (
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 5) "
        "SELECT COUNT(*) FROM r"
    )
    async with running(root) as application:
        # 검증을 건너뛰고 CTE 이름을 넘겨도 러너는 카탈로그·숨긴 표 이름을 CTE로 받지 않는다.
        master_error = await failure_of(application, database, master, ctes=["sqlite_master"])
        secret_error = await failure_of(application, database, shadow, ctes=["secret"])
        counted = await rows_of(application, database, recursive, ctes=["r"])

    assert denied(master_error) and denied(secret_error)
    assert "CREATE TABLE" not in master_error
    # 재귀 CTE는 열 없이 읽을 때 CTE 이름으로 READ가 온다(그것만 받는다).
    assert counted.rows == [[5]]
    assert rejected(master) == "cte_name_conflict" and rejected(shadow) == "cte_name_conflict"


async def test_the_copy_closes_every_bypass(root: Path) -> None:
    async with running(root) as application:
        copy = await make_copy(application, root)
        star = await rows_of(application, copy, "SELECT * FROM customers ORDER BY id", hidden={})
        public = await rows_of(application, copy, "SELECT * FROM v_public", hidden={})
        bypasses = [
            "SELECT email FROM customers",
            "SELECT name FROM customers JOIN (SELECT 'kim@example.com' AS email) USING (email)",
            "SELECT name FROM customers NATURAL JOIN (SELECT 'kim@example.com' AS email)",
            "SELECT j.value FROM customers, json_each(customers.profile) AS j",
            "SELECT * FROM secret",
            "SELECT * FROM v_contacts",
            "SELECT * FROM v_secret",
        ]
        # 사본에는 숨긴 열·표·뷰가 없다. 검증과 authorizer 설정을 모두 비워도 값이 나오지 않는다.
        results = [
            (await raw(application, copy, sql, hidden={}, hidden_tables=[]))[0] for sql in bypasses
        ]
        natural = (await raw(application, copy, bypasses[2], hidden={}, hidden_tables=[]))[0]

    assert star.columns == ["id", "name"] and public.columns == ["id", "name"]
    for sql, result in zip(bypasses, results, strict=True):
        if sql == bypasses[2]:
            continue
        assert isinstance(result, SqlRunFailed) and result.code == "sql_error", (sql, result)
    # NATURAL은 같은 이름 열이 없어 교차 조인이 되지만 숨긴 값은 비교하지 못한다.
    assert isinstance(natural, QueryOutput) and natural.columns == ["name"]
    raw_bytes = copy.read_bytes()
    assert b"kim@example.com" not in raw_bytes and b"s3cr3t" not in raw_bytes
    # 검증부터 러너까지: USING(숨긴 열)은 검증이 거부하고, 사본에서도 실행되지 않는다.
    assert rejected(bypasses[1]) == "column_hidden"


async def test_bypasses_and_writes_fail_in_sqlite(root: Path) -> None:
    database = root / "shop.db"
    out = root / "copy.db"
    statements = [
        "SELECT * FROM read_csv('C:/Windows/win.ini')",
        "SELECT * FROM '<path>'".replace("<path>", database.as_posix()),
        "INSERT INTO orders VALUES (9, 1, 1.0)",
        "DROP TABLE orders",
        f"ATTACH '{out.as_posix()}' AS other",
        f"VACUUM INTO '{out.as_posix()}'",
        "PRAGMA writable_schema = ON",
        "SELECT load_extension('C:/Windows/System32/kernel32.dll')",
        f"COPY orders TO '{out.as_posix()}'",
        "WITH a AS (SELECT 1) INSERT INTO orders SELECT 9, 1, 1.0 FROM a",
        "SELECT readfile('C:/Windows/win.ini')",
    ]
    async with running(root) as application:
        results = [(await raw(application, database, sql, ctes=["a"]))[0] for sql in statements]
        count = await rows_of(application, database, "SELECT COUNT(*) AS n FROM orders")

    for sql, failed in zip(statements, results, strict=True):
        assert isinstance(failed, SqlRunFailed), (sql, failed)
        assert failed.code == "sql_error", (sql, failed.message)
    assert not out.exists()
    assert count.rows == [[3]]


async def test_runaway_queries_stop_at_the_timeout_and_leave_no_process(root: Path) -> None:
    database = root / "shop.db"
    runaway = [
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r",
        "SELECT COUNT(*) FROM numbers a, numbers b, numbers c, numbers d",
    ]
    async with running(root) as application:
        for sql in runaway:
            started = time.monotonic()
            failed, _, marker = await raw(application, database, sql, ctes=["r"], timeout_sec=2)
            elapsed = time.monotonic() - started

            assert isinstance(failed, SqlRunFailed) and failed.code == "sql_timeout", failed
            assert elapsed < 12, elapsed
            assert processes_with(marker) == []
            print(f"\n[KS4] SQLite {sql[:30]}… {elapsed:.2f}초에 끊김")


async def test_rows_over_max_rows_are_truncated_into_a_csv(root: Path) -> None:
    database = root / "shop.db"
    async with running(root) as application:
        copy = await make_copy(application, root)
        numbers, numbers_csv, _ = await raw(
            application,
            database,
            "SELECT n FROM numbers ORDER BY n LIMIT 51",
            max_rows=50,
            export_csv=True,
        )
        people, people_csv, _ = await raw(
            application,
            copy,
            "SELECT * FROM customers ORDER BY id LIMIT 2",
            hidden={},
            max_rows=1,
            export_csv=True,
        )

    assert isinstance(numbers, QueryOutput) and numbers.truncated and numbers.row_count == 50
    assert numbers_csv is not None
    lines = numbers_csv.splitlines()
    assert lines[0] == "n" and len(lines) == 51 and lines[-1] == "49"
    assert isinstance(people, QueryOutput) and people.truncated
    assert people_csv is not None
    # 사본에는 숨긴 열이 없어 CSV에도 없다.
    assert people_csv.splitlines() == ["id,name", "1,김하나"]
