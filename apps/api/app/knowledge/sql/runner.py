"""SQL 실행과 DuckDB 런타임 설치(3단계 설계 10.2, 10.5). 실제 실행은 샌드박스 하위 프로세스가 한다.

서버는 DuckDB를 import하지 않고 sqlite3로 대상 파일을 열지도 않는다. 스크립트 템플릿을 만들어 세
번째 로컬 백엔드(DATA_DIR/kb-work)의 작업 폴더에서 `python -I`로 돌리고(Job Object 메모리·프로세스
4개·시간 timeout_sec + 5), 결과 JSON과 잘렸을 때의 CSV를 읽는다. 설정 값(SQL, 경로, 허용 목록)은
JSON 글 하나로 스크립트에 넣어 코드와 섞이지 않게 한다.

DuckDB는 준비 단계가 만든 질의 파일(보이는 열만 담은 것)을 read_only로 열고 DUCKDB_LOCK 순서로
잠근다. 순서가 바뀌면 잠금이 무의미하다. temp_directory는 enable_external_access를 끈 뒤에는 바꿀 수
없어(DuckDB 2.0.0 실측) 연결을 열 때 config로 준다. 시간 상한이 지나면 타이머가 interrupt한다.

SQLite는 `mode=ro&immutable=1` URI, query_only, ATTACH 0개, authorizer, 진행 핸들러로
연다. 숨긴 표·열이 있는 소스는 준비가 만든 사본(보이는 표·열과 그것만 읽는 뷰만 담은
cache/kb-sql/<id>.sqlite)을 열므로 숨긴 값이 파일에 아예 없고, authorizer는 두 번째 겹이다.
authorizer는 SELECT·READ·허용 함수·RECURSIVE만 받는다. READ는 소스의 표·뷰와 json_each·json_tree만
받고(pragma_table_info 같은 표 값 함수는 PRAGMA가 아니라 READ로 온다), 숨긴 표는 직접이든 뷰를
거치든 DENY, 숨긴 열은 SQLITE_IGNORE로 NULL을 읽게 한다. 이 쿼리의 CTE 이름은 열 없이 읽을 때만
받는다. SQLite 3.45는 재귀 CTE를 COUNT(*)처럼 열 없이 읽을 때만 CTE 이름으로 READ를 내고, 열을
읽을 때나 일반 CTE는 밑의 표 이름으로 낸다(실측).
"""

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.knowledge.sql.results import ROWS_MAX_BYTES
from app.knowledge.sql.sources import RESERVED_PREFIXES, SQLITE_FUNCTIONS
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.workflow.extract_runner import (
    ExtractError,
    ensure_packages,
    package_dir,
    run_script,
)

Engine = Literal["duckdb", "sqlite"]
# 시험한 버전으로 고정한다. 바꾸면 해시가 바뀌어 새 폴더(packages-<해시>)에 다시 받는다. pytz는
# DuckDB가 TIMESTAMPTZ 값을 파이썬 datetime으로 바꿀 때 필요하다(없으면 그 열을 읽지 못한다).
RUNTIME_SPECS: tuple[str, ...] = ("duckdb==2.0.0", "pytz==2026.6")
RUNTIME_PREFIX = "packages"
EXTENSIONS: tuple[str, ...] = ("excel", "encodings")
EXTENSIONS_DIR = "duckdb-ext"
EXTENSIONS_MARKER = ".installed"
EXTENSIONS_TIMEOUT_SEC = 600
EXTENSIONS_MEMORY_MB = 1024
# 시간 상한에 더해 프로세스를 끝내기까지 기다리는 몫(10.5).
KILL_GRACE_SEC = 5
# 셀을 자르지 않으므로 행 수 상한(max_rows ≤ SQL_MAX_ROWS 10,000)만으로는 크기가 정해지지 않는다. 긴
# 글 열(본문·JSON)이 있으면 1,001행으로도 결과 JSON(ROWS_MAX_BYTES 1MB, 넘으면 rows_cut)과 CSV에
# 닿는다. CSV 상한은 셀 하나 5KB 안팎의 10,000행을 담는 크기다.
CSV_MAX_BYTES = 50 * 1024 * 1024
# 결과 JSON 파일을 읽는 상한은 행 1MB에 열 이름·숫자 필드 몫을 더한 것이다.
RESULT_SLACK_BYTES = 512 * 1024
OUTPUT_NAME = "result.json"
CSV_NAME = "result.csv"
# DuckDB 질의 연결의 잠금 순서(10.5). 앞의 둘이 확장 자동 설치·로드를 끄고, 그다음이 파일 접근을
# 막고, 자원 상한을 건 뒤 마지막에 설정을 잠근다. 잠근 뒤에는 COPY·ATTACH·INSTALL·설정 변경이 모두
# 거부된다.
DUCKDB_LOCK: tuple[str, ...] = (
    "SET autoload_known_extensions=false",
    "SET autoinstall_known_extensions=false",
    "SET allowed_paths=[]",
    "SET enable_external_access=false",
    "SET memory_limit='{memory}MB'",
    "SET threads=2",
    "SET max_temp_directory_size='1GB'",
    "SET lock_configuration=true",
)


def duckdb_lock(memory_mb: int) -> list[str]:
    return [statement.format(memory=memory_mb) for statement in DUCKDB_LOCK]


def duckdb_memory_mb(process_memory_mb: int) -> int:
    # Job Object 상한의 절반을 DuckDB에 준다(512MB면 256MB, 10.8). 나머지는 파이썬과 결과 몫이다.
    return max(64, process_memory_mb // 2)


_COMMON = r"""# 플랫폼이 만든 SQL 실행 스크립트다. 결과를 JSON(잘렸으면 CSV도)으로 남긴다.
import csv
import datetime
import decimal
import json
import math
import os
import time
import uuid

_CONFIG = json.loads(__CONFIG__)
_HERE = os.path.dirname(os.path.abspath(__file__))
_BIG = 2 ** 53


def _value(value):
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if -_BIG <= value <= _BIG else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<%d bytes>" % len(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    return str(value)


def _write(payload):
    with open(os.path.join(_HERE, _CONFIG["output"]), "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False)


def _fail(kind, message):
    _write({"ok": False, "kind": kind, "error": str(message)[:2000]})


# 표 프로그램은 이 글자로 시작하는 칸을 수식으로 읽는다(CSV 수식 주입).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value):
    # DB에서 꺼낸 값 그대로 받는다. 글 값만 앞에 '를 붙여 수식이 되지 않게 하고, 수·날짜처럼 글이
    # 아닌 값은 바꾸지 않는다(음수가 글이 되지 않게).
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return "'" + value if value.startswith(_FORMULA_START) else value
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(_value(value), ensure_ascii=False)
    return _value(value)


def _write_csv(columns, rows):
    path = os.path.join(_HERE, _CONFIG["csv_name"])
    written = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        # 열 이름도 모델이 쓴 SQL의 별칭이라 같은 규칙으로 중화한다.
        writer.writerow([_csv_cell(str(name)) for name in columns])
        for row in rows:
            cells = [_csv_cell(item) for item in row]
            writer.writerow(cells)
            written += sum(len(str(cell).encode("utf-8")) + 1 for cell in cells)
            if written > _CONFIG["csv_bytes"]:
                return True
    return False


def _finish(columns, rows, started, spilled=0):
    limit = _CONFIG["max_rows"]
    truncated = len(rows) > limit
    # CSV는 바꾸기 전 값으로 쓴다. _value가 날짜·큰 수를 글로 바꾸므로 그 뒤에는 글 값인지 모른다.
    raw = rows[:limit]
    rows = [[_value(item) for item in row] for row in raw]
    kept, size, cut = [], 2, False
    for row in rows:
        size += len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
        if size > _CONFIG["rows_bytes"]:
            cut = True
            break
        kept.append(row)
    csv_name, csv_cut = None, False
    if _CONFIG["csv"] and rows and (truncated or cut):
        csv_cut = _write_csv(columns, raw)
        csv_name = _CONFIG["csv_name"]
    _write(
        {
            "ok": True,
            "columns": columns,
            "rows": kept,
            "row_count": len(rows),
            "truncated": truncated,
            "rows_cut": cut,
            "csv": csv_name,
            "csv_cut": csv_cut,
            "spilled_bytes": spilled,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    )
"""

_DUCKDB = r"""

def _spilled(folder):
    # 메모리 상한을 넘은 정렬·집계가 작업 폴더의 temp_directory에 쓴 크기. 연결을 닫으면 지워지므로
    # 결과를 받은 직후에 잰다.
    total = 0
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                total += os.path.getsize(path)
    return total


def _main():
    import site
    import threading

    site.addsitedir(_CONFIG["lib"])
    try:
        import duckdb
    except ImportError as exc:
        _fail("runtime_missing", "%s: %s" % (type(exc).__name__, exc))
        return
    # 잠근 뒤에는 temp_directory를 바꿀 수 없어 연결을 열 때 준다.
    connection = duckdb.connect(
        _CONFIG["db"], read_only=True, config={"temp_directory": _CONFIG["temp"]}
    )
    for statement in _CONFIG["lock"]:
        connection.execute(statement)
    timer = threading.Timer(_CONFIG["timeout"], connection.interrupt)
    timer.daemon = True
    started = time.monotonic()
    timer.start()
    try:
        cursor = connection.execute(_CONFIG["sql"])
        columns = [item[0] for item in (cursor.description or [])]
        rows = cursor.fetchmany(_CONFIG["max_rows"] + 1) if columns else []
        spilled = _spilled(_CONFIG["temp"])
    except duckdb.InterruptException:
        _fail("timeout", "%d초 안에 끝나지 않아 멈췄다" % _CONFIG["timeout"])
        return
    except duckdb.OutOfMemoryException as exc:
        _fail("memory_limit", str(exc).splitlines()[0] if str(exc) else "메모리 상한을 넘었다")
        return
    except duckdb.Error as exc:
        _fail("sql_error", "%s: %s" % (type(exc).__name__, exc))
        return
    finally:
        timer.cancel()
    _finish(columns, rows, started, spilled)


try:
    _main()
except MemoryError:
    _fail("memory_limit", "메모리 상한을 넘었다")
except Exception as exc:
    _fail("failed", "%s: %s" % (type(exc).__name__, exc))
"""

_SQLITE = r"""
import sqlite3
from urllib.parse import quote

_HIDDEN = {table: set(columns) for table, columns in _CONFIG["hidden"].items()}
_HIDDEN_TABLES = set(_CONFIG["hidden_tables"])
_READABLE = (set(_CONFIG["tables"]) - _HIDDEN_TABLES) | {"json_each", "json_tree"}
_RESERVED = tuple(_CONFIG["reserved"])
# 표·카탈로그 이름과 겹치는 CTE 이름은 받지 않는다(검증도 cte_name_conflict로 거부한다).
_CTES = set(
    name for name in _CONFIG["ctes"]
    if name not in _READABLE and name not in _HIDDEN_TABLES and not name.startswith(_RESERVED)
)
_FUNCTIONS = set(_CONFIG["functions"])


def _authorize(action, first, second, database, source):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        table = (first or "").lower()
        column = (second or "").lower()
        if table in _HIDDEN_TABLES:
            return sqlite3.SQLITE_DENY
        if column and column in _HIDDEN.get(table, ()):
            return sqlite3.SQLITE_IGNORE
        if table in _READABLE:
            return sqlite3.SQLITE_OK
        if not column and table in _CTES:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if (second or "").lower() in _FUNCTIONS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


def _main():
    path = _CONFIG["db"].replace("\\", "/")
    uri = "file:" + quote(path, safe="/:") + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.execute("PRAGMA query_only=ON")
    connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    connection.set_authorizer(_authorize)
    deadline = time.monotonic() + _CONFIG["timeout"]
    connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    started = time.monotonic()
    try:
        cursor = connection.execute(_CONFIG["sql"])
        columns = [item[0] for item in (cursor.description or [])]
        rows = cursor.fetchmany(_CONFIG["max_rows"] + 1) if columns else []
    except sqlite3.Error as exc:
        if "interrupted" in str(exc):
            _fail("timeout", "%d초 안에 끝나지 않아 멈췄다" % _CONFIG["timeout"])
        else:
            _fail("sql_error", "%s: %s" % (type(exc).__name__, exc))
        return
    _finish(columns, rows, started)


try:
    _main()
except MemoryError:
    _fail("memory_limit", "메모리 상한을 넘었다")
except Exception as exc:
    _fail("failed", "%s: %s" % (type(exc).__name__, exc))
"""

_EXTENSIONS = r"""# 플랫폼이 만든 DuckDB 확장 설치 스크립트다.
import json
import os
import site

_CONFIG = json.loads(__CONFIG__)
_HERE = os.path.dirname(os.path.abspath(__file__))


def _write(payload):
    with open(os.path.join(_HERE, _CONFIG["output"]), "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False)


try:
    site.addsitedir(_CONFIG["lib"])
    import duckdb

    connection = duckdb.connect()
    connection.execute(
        "SET extension_directory = '%s'" % _CONFIG["directory"].replace("'", "''")
    )
    for name in _CONFIG["names"]:
        connection.execute("INSTALL " + name)
        try:
            connection.execute("LOAD " + name)
        except duckdb.Error:
            # 앞선 설치가 끊겨 깨진 파일이 남았으면 다시 받는다.
            connection.execute("FORCE INSTALL " + name)
            connection.execute("LOAD " + name)
    _write({"ok": True, "version": duckdb.__version__})
except Exception as exc:
    _write({"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:1000])})
"""


def render(template: str, config: Mapping[str, Any]) -> str:
    # 설정은 JSON 글 하나로 넣는다. repr이 파이썬 문자열 리터럴이라 SQL이나 경로가 코드가 되지
    # 않는다.
    return template.replace("__CONFIG__", repr(json.dumps(config, ensure_ascii=False)))


class SqlRunFailed(Exception):
    """실행 실패. code는 sql_error, sql_timeout, sql_memory_limit, sql_runtime_missing,
    sql_failed다."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


_FAILURE_CODES = {
    "timeout": "sql_timeout",
    "memory_limit": "sql_memory_limit",
    "runtime_missing": "sql_runtime_missing",
    "sql_error": "sql_error",
}


@dataclass(frozen=True)
class QueryRequest:
    engine: Engine
    database: Path
    sql: str
    max_rows: int
    timeout_sec: int
    # 하위 프로세스(Job Object) 상한. DuckDB memory_limit은 그 절반이다.
    memory_mb: int
    export_csv: bool = False
    # DuckDB 패키지 폴더(packages-<해시>).
    lib: Path | None = None
    # SQLite authorizer가 읽기를 허용할 표·뷰(소문자)와 이 쿼리의 CTE(열 없는 읽기만).
    tables: Sequence[str] = ()
    ctes: Sequence[str] = ()
    # 숨긴 열(표 소문자 → 열 소문자)과 숨긴 표. SQLite만 쓴다(DuckDB 질의 파일에는 아예 없다).
    hidden: Mapping[str, Sequence[str]] = field(default_factory=dict[str, Sequence[str]])
    hidden_tables: Sequence[str] = ()


@dataclass(frozen=True)
class QueryOutput:
    columns: list[str]
    # 값은 JSON으로 바꾼 행 목록. 1MB 상한 안에서 잘렸으면 rows_cut이다.
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    rows_cut: bool
    elapsed_ms: int
    # 잘렸을 때 작업 폴더에 쓴 CSV(전체 max_rows행). csv_cut이면 50MB에서 멈췄다.
    csv_path: Path | None = None
    csv_cut: bool = False
    # DuckDB가 메모리 상한을 넘어 작업 폴더(.tmp/duckdb)에 내려 쓴 크기.
    spilled_bytes: int = 0


def query_config(request: QueryRequest, handle: WorkspaceHandle) -> dict[str, Any]:
    config: dict[str, Any] = {
        "output": OUTPUT_NAME,
        "csv_name": CSV_NAME,
        "csv": request.export_csv,
        "csv_bytes": CSV_MAX_BYTES,
        "rows_bytes": ROWS_MAX_BYTES,
        "db": str(request.database),
        "sql": request.sql,
        "max_rows": request.max_rows,
        "timeout": request.timeout_sec,
    }
    if request.engine == "duckdb":
        config["lib"] = str(request.lib) if request.lib is not None else ""
        config["temp"] = str(handle.root / ".tmp" / "duckdb")
        config["lock"] = duckdb_lock(duckdb_memory_mb(request.memory_mb))
    else:
        config["tables"] = [name.lower() for name in request.tables]
        config["ctes"] = [name.lower() for name in request.ctes]
        config["hidden"] = {
            table.lower(): [column.lower() for column in columns]
            for table, columns in request.hidden.items()
        }
        config["hidden_tables"] = [name.lower() for name in request.hidden_tables]
        config["reserved"] = list(RESERVED_PREFIXES)
        config["functions"] = sorted(SQLITE_FUNCTIONS)
    return config


def query_script(request: QueryRequest, handle: WorkspaceHandle) -> str:
    body = _DUCKDB if request.engine == "duckdb" else _SQLITE
    return render(_COMMON + body, query_config(request, handle))


async def run_query(
    sandbox: SandboxBackend, handle: WorkspaceHandle, request: QueryRequest, *, exec_id: str
) -> QueryOutput:
    """작업 폴더에서 질의를 돌린다. 실패는 SqlRunFailed다."""
    script = query_script(request, handle)
    try:
        result = await run_script(
            sandbox,
            handle,
            exec_id=exec_id,
            script=script,
            output=OUTPUT_NAME,
            output_limit=ROWS_MAX_BYTES + RESULT_SLACK_BYTES,
            timeout_sec=request.timeout_sec + KILL_GRACE_SEC,
            memory_mb=request.memory_mb,
        )
    except SandboxError as exc:
        raise SqlRunFailed("sql_failed", exc.message, retryable=True) from None
    if result.timed_out:
        raise SqlRunFailed(
            "sql_timeout", f"{request.timeout_sec}초 안에 끝나지 않아 프로세스를 끝냈다"
        )
    payload = result.payload
    if payload is None:
        raise SqlRunFailed(
            "sql_failed",
            f"질의 프로세스가 결과 없이 끝났다(메모리 상한일 수 있다): {result.detail}",
        )
    if payload.get("ok") is not True:
        kind = str(payload.get("kind") or "failed")
        message = str(payload.get("error") or "질의를 실행하지 못했다")
        code = _FAILURE_CODES.get(kind, "sql_failed")
        raise SqlRunFailed(code, message, retryable=code == "sql_runtime_missing")
    columns = [str(name) for name in payload.get("columns") or []]
    raw_rows = payload.get("rows") or []
    rows = [list(row) for row in raw_rows if isinstance(row, list)]
    csv_name = payload.get("csv")
    return QueryOutput(
        columns=columns,
        rows=rows,
        row_count=int(payload.get("row_count") or 0),
        truncated=payload.get("truncated") is True,
        rows_cut=payload.get("rows_cut") is True,
        elapsed_ms=int(payload.get("elapsed_ms") or 0),
        csv_path=handle.root / ".run" / CSV_NAME if isinstance(csv_name, str) else None,
        csv_cut=payload.get("csv_cut") is True,
        spilled_bytes=int(payload.get("spilled_bytes") or 0),
    )


def runtime_dir(cache_dir: Path) -> Path:
    return package_dir(cache_dir, RUNTIME_SPECS, RUNTIME_PREFIX)


def extensions_dir(cache_dir: Path) -> Path:
    return cache_dir / EXTENSIONS_DIR


async def install_runtime(
    sandbox: SandboxBackend, handle: WorkspaceHandle, cache_dir: Path, *, exec_prefix: str
) -> tuple[Path, str]:
    """DuckDB 패키지와 확장을 받는다. (패키지 폴더, DuckDB 버전). 실패는 SqlRunFailed다.

    패키지는 extract_runner의 설치 잠금·표시 파일 함수로 받고, 확장은 잠그기 전에만 설치할 수 있어
    여기서 extension_directory에 미리 받는다(10.2).
    """
    try:
        lib = await ensure_packages(
            sandbox,
            handle,
            RUNTIME_SPECS,
            cache_dir,
            exec_id=f"{exec_prefix}-pip",
            label="DuckDB",
            prefix=RUNTIME_PREFIX,
        )
    except ExtractError as exc:
        raise SqlRunFailed("sql_runtime_install_failed", exc.message, retryable=True) from None
    directory = extensions_dir(cache_dir)
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    output = f"{uuid.uuid4().hex[:8]}.extensions.json"
    script = render(
        _EXTENSIONS,
        {
            "output": output,
            "lib": str(lib),
            "directory": str(directory),
            "names": list(EXTENSIONS),
        },
    )
    try:
        result = await run_script(
            sandbox,
            handle,
            exec_id=f"{exec_prefix}-ext",
            script=script,
            output=output,
            output_limit=64 * 1024,
            timeout_sec=EXTENSIONS_TIMEOUT_SEC,
            memory_mb=EXTENSIONS_MEMORY_MB,
        )
    except SandboxError as exc:
        raise SqlRunFailed("sql_runtime_install_failed", exc.message, retryable=True) from None
    payload = result.payload
    if result.timed_out or payload is None or payload.get("ok") is not True:
        reason = (
            f"{EXTENSIONS_TIMEOUT_SEC}초 안에 끝나지 않았다"
            if result.timed_out
            else str((payload or {}).get("error") or result.detail)
        )
        raise SqlRunFailed(
            "sql_runtime_install_failed",
            f"DuckDB 확장({', '.join(EXTENSIONS)})을 받지 못했다. 인터넷 연결을 확인한다: {reason}",
            retryable=True,
        )
    version = str(payload.get("version") or "")
    marker = directory / EXTENSIONS_MARKER
    await asyncio.to_thread(marker.write_text, version + "\n", encoding="utf-8")
    return lib, version
