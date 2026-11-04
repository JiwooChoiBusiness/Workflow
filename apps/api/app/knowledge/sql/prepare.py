"""데이터 소스 준비(3단계 설계 10.2). 스키마 스냅샷을 뜨고, files 소스는 질의 파일을 만든다.

files 소스(CSV·TSV·xlsx)는 샌드박스 DuckDB가 원본을 한 번 읽어 숨긴 열을 뺀 표를 작업 폴더의
source.duckdb에 `CREATE TABLE <표> AS SELECT <보이는 열…> FROM read_csv(…)`로 적재하고, 열
이름·타입·행 수·열마다 샘플값 3개를 뜬다. 이때만 원본 경로와 excel·encodings 확장이 필요하다. 원본
경로만 allowed_paths에 넣고 외부 접근을 끈 뒤 잠근다. 숨긴 표는 열 이름만 뜨고 적재하지 않는다.
인코딩이 auto인데 UTF-8로 읽지 못하면 cp949로 다시 읽는다.

sqlite 소스는 원본을 `mode=ro&immutable=1`로 열어 sqlite_master(뷰 정의 포함)와 PRAGMA
table_info로 스냅샷을 뜬다. 행 수·샘플은 질의와 같은 숨김 규칙의 authorizer 아래에서 읽어 숨긴 값이
샘플에 들지 않는다. 숨긴 표·열이 있으면 보이는 표·열과 그것만 읽는 뷰, 보이는 열의 인덱스만 담은
사본(build_sqlite_copy)을 따로 만들고 질의는 그 사본을 연다. 서버는 어느 쪽도 직접 열지 않는다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.knowledge.sql.runner import (
    OUTPUT_NAME,
    SqlRunFailed,
    duckdb_memory_mb,
    render,
)
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.schemas.sql_source import SqlColumn, SqlTable
from app.workflow.extract_runner import run_script

PREPARE_TIMEOUT_SEC = 600
OUTPUT_LIMIT = 4 * 1024 * 1024
DATABASE_NAME = "source.duckdb"
# DuckDB가 확장 없이 읽는 인코딩.
_BUILT_IN = frozenset({"utf-8", "utf-16", "latin-1"})

_SAMPLES = r"""

def _samples(names, rows):
    found = {name: [] for name in names}
    seen = {name: set() for name in names}
    for row in rows:
        for name, value in zip(names, row):
            if value is None or len(found[name]) >= 3:
                continue
            value = _value(value)
            key = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if key in seen[name]:
                continue
            seen[name].add(key)
            if isinstance(value, str) and len(value) > 60:
                value = value[:60] + "…"
            found[name].append(value)
    return found


def _value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return str(value)
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<%d bytes>" % len(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    return str(value)


def _ident(name):
    return '"' + str(name).replace('"', '""') + '"'


def _literal(text):
    return "'" + str(text).replace("'", "''") + "'"


def _write(payload):
    with open(os.path.join(_HERE, _CONFIG["output"]), "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False)
"""

_FILES = r"""# 플랫폼이 만든 데이터 소스 준비 스크립트다. 질의 파일과 스냅샷을 만든다.
import json
import os
import site
import time

_CONFIG = json.loads(__CONFIG__)
_HERE = os.path.dirname(os.path.abspath(__file__))
__SAMPLES__

def _reader(item, encoding):
    args = [_literal(item["path"]), "header=" + ("true" if item["header"] else "false")]
    if item["all_varchar"]:
        args.append("all_varchar=true")
    if item["format"] == "xlsx":
        if item.get("sheet"):
            args.append("sheet=" + _literal(item["sheet"]))
        return "read_xlsx(" + ", ".join(args) + ")"
    if item["format"] == "tsv":
        args.append("delim=" + _literal(chr(9)))
    if encoding:
        args.append("encoding=" + _literal(encoding))
    return "read_csv(" + ", ".join(args) + ")"


def _unicode_error(exc):
    return "unicode" in str(exc).lower() or "encoding" in str(exc).lower()


def _load(connection, duckdb, item):
    choices = [item["encoding"]] if item["encoding"] != "auto" else [None, "cp949"]
    if item["format"] == "xlsx":
        choices = [None]
    hidden = set(name.lower() for name in item["hidden"])
    last = None
    for encoding in choices:
        reader = _reader(item, encoding)
        try:
            described = connection.execute("DESCRIBE SELECT * FROM " + reader).fetchall()
            columns = [(str(row[0]), str(row[1])) for row in described]
            visible = [name for name, _ in columns if name.lower() not in hidden]
            if item["table_hidden"] or not visible:
                return columns, None, encoding
            select = ", ".join(_ident(name) for name in visible)
            connection.execute(
                "CREATE TABLE " + _ident(item["table"]) + " AS SELECT " + select + " FROM " + reader
            )
            return columns, visible, encoding
        except duckdb.Error as exc:
            last = exc
            if encoding is None and len(choices) > 1 and _unicode_error(exc):
                continue
            raise
    raise last


def _main():
    site.addsitedir(_CONFIG["lib"])
    import duckdb

    started = time.monotonic()
    connection = duckdb.connect(_CONFIG["database"], config={"temp_directory": _CONFIG["temp"]})
    connection.execute("SET extension_directory = " + _literal(_CONFIG["extensions"]))
    connection.execute("SET autoinstall_known_extensions=false")
    connection.execute("SET autoload_known_extensions=false")
    loaded = []
    for name in _CONFIG["extensions_needed"]:
        try:
            connection.execute("LOAD " + name)
            loaded.append(name)
        except duckdb.Error:
            pass
    paths = ", ".join(_literal(item["path"]) for item in _CONFIG["files"])
    connection.execute("SET allowed_paths=[" + paths + "]")
    connection.execute("SET enable_external_access=false")
    connection.execute("SET memory_limit=" + _literal("%dMB" % _CONFIG["memory_mb"]))
    connection.execute("SET threads=2")
    connection.execute("SET lock_configuration=true")
    tables = []
    for item in _CONFIG["files"]:
        try:
            columns, visible, encoding = _load(connection, duckdb, item)
        except duckdb.Error as exc:
            message = str(exc)
            if "excel" in message.lower() and "excel" not in loaded:
                message = "xlsx를 읽는 excel 확장이 없다. 런타임을 다시 준비한다: " + message
            elif _unicode_error(exc) and "encodings" not in loaded:
                message = (
                    "cp949 등을 읽는 encodings 확장이 없다. 런타임을 다시 준비한다: " + message
                )
            _write({"ok": False, "kind": "file", "file_id": item["file_id"],
                    "error": "%s(%s): %s" % (item["filename"], item["table"], message[:1500])})
            return
        row_count = None
        samples = {}
        if visible:
            table = _ident(item["table"])
            row_count = connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
            select = ", ".join(_ident(name) for name in visible)
            rows = connection.execute(
                "SELECT " + select + " FROM " + table + " LIMIT 2000"
            ).fetchall()
            samples = _samples(visible, rows)
        tables.append(
            {
                "name": item["table"],
                "file_id": item["file_id"],
                "encoding": encoding or ("auto" if item["format"] != "xlsx" else None),
                "row_count": row_count,
                "columns": [
                    {"name": name, "type": kind, "samples": samples.get(name, [])}
                    for name, kind in columns
                ],
            }
        )
    connection.execute("CHECKPOINT")
    connection.close()
    _write({"ok": True, "tables": tables, "version": duckdb.__version__,
            "elapsed_ms": int((time.monotonic() - started) * 1000)})


try:
    _main()
except ImportError as exc:
    _write({"ok": False, "kind": "runtime_missing", "error": "%s: %s" % (type(exc).__name__, exc)})
except MemoryError:
    _write({"ok": False, "kind": "memory_limit", "error": "메모리 상한을 넘었다"})
except Exception as exc:
    _write({"ok": False, "kind": "failed", "error": "%s: %s" % (type(exc).__name__, exc)})
"""

_SQLITE = r"""# 플랫폼이 만든 SQLite 데이터 소스 스냅샷 스크립트다. 원본을 읽기 전용으로 연다.
import json
import os
import sqlite3
import time
from urllib.parse import quote

_CONFIG = json.loads(__CONFIG__)
_HERE = os.path.dirname(os.path.abspath(__file__))
__SAMPLES__
_HIDDEN = {table: set(columns) for table, columns in _CONFIG["hidden"].items()}
_HIDDEN_TABLES = set(_CONFIG["hidden_tables"])


def _authorize(action, first, second, database, source):
    # 행 수·샘플은 질의와 같은 규칙으로 읽는다. 숨긴 표는 뷰를 거쳐도 막고 숨긴 열은 NULL이다.
    if action == sqlite3.SQLITE_READ:
        table = (first or "").lower()
        if table in _HIDDEN_TABLES:
            return sqlite3.SQLITE_DENY
        if second and second.lower() in _HIDDEN.get(table, ()):
            return sqlite3.SQLITE_IGNORE
    return sqlite3.SQLITE_OK


def _main():
    started = time.monotonic()
    path = _CONFIG["database"].replace("\\", "/")
    uri = "file:" + quote(path, safe="/:") + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.execute("PRAGMA query_only=ON")
    connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    deadline = time.monotonic() + _CONFIG["timeout"]
    connection.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    found = connection.execute(
        "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table', 'view') "
        "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY rowid"
    ).fetchall()
    views = [{"name": name, "sql": sql} for name, kind, sql in found if kind == "view" and sql]
    entries = []
    # 열 목록은 authorizer 없이 뜬다(숨긴 표를 읽는 뷰도 열 이름은 남긴다). 정의가 깨진 뷰는 그
    # 뷰만 오류로 남긴다.
    for name, kind, _ in sorted(found, key=lambda item: item[0]):
        entry = {"name": name, "kind": kind, "row_count": None, "columns": []}
        try:
            info = connection.execute("PRAGMA table_info(" + _ident(name) + ")").fetchall()
            entry["columns"] = [(str(row[1]), str(row[2] or "")) for row in info]
        except sqlite3.Error as exc:
            entry["error"] = str(exc)[:300]
        entries.append(entry)
    connection.set_authorizer(_authorize)
    tables = []
    for entry in entries:
        name, kind = entry["name"], entry["kind"]
        concealed = _HIDDEN.get(name.lower(), set())
        visible = [] if name.lower() in _HIDDEN_TABLES or "error" in entry else [
            column for column, _ in entry["columns"] if column.lower() not in concealed
        ]
        samples = {}
        try:
            if kind == "table" and name.lower() not in _HIDDEN_TABLES:
                entry["row_count"] = connection.execute(
                    "SELECT count(*) FROM " + _ident(name)
                ).fetchone()[0]
            if visible:
                select = ", ".join(_ident(column) for column in visible)
                rows = connection.execute(
                    "SELECT " + select + " FROM " + _ident(name) + " LIMIT 2000"
                ).fetchall()
                samples = _samples(visible, rows)
        except sqlite3.Error:
            pass
        entry["columns"] = [
            {"name": column, "type": declared, "samples": samples.get(column, [])}
            for column, declared in entry["columns"]
        ]
        tables.append(entry)
    _write({"ok": True, "tables": tables, "views": views,
            "elapsed_ms": int((time.monotonic() - started) * 1000)})


try:
    _main()
except MemoryError:
    _write({"ok": False, "kind": "memory_limit", "error": "메모리 상한을 넘었다"})
except sqlite3.Error as exc:
    _write({"ok": False, "kind": "file", "error": "SQLite 파일을 읽지 못했다: %s" % exc})
except Exception as exc:
    _write({"ok": False, "kind": "failed", "error": "%s: %s" % (type(exc).__name__, exc)})
"""


_COPY = r"""# 플랫폼이 만든 SQLite 사본 스크립트다. 숨긴 표·열을 뺀 질의용 사본을 만든다.
import json
import os
import re
import sqlite3
import time
from urllib.parse import quote

_CONFIG = json.loads(__CONFIG__)
_HERE = os.path.dirname(os.path.abspath(__file__))
__SAMPLES__
_TYPE = re.compile(r"^[A-Za-z0-9_ (),]{1,64}$")


def _uri(path, mode):
    return "file:" + quote(path.replace("\\", "/"), safe="/:") + "?" + mode


def _main():
    started = time.monotonic()
    target = _CONFIG["target"]
    if os.path.exists(target):
        os.remove(target)
    copy = sqlite3.connect(_uri(target, "mode=rwc"), uri=True, isolation_level=None)
    copy.execute("PRAGMA journal_mode=OFF")
    copy.execute("PRAGMA synchronous=OFF")
    copy.execute("ATTACH DATABASE ? AS src", (_uri(_CONFIG["source"], "mode=ro&immutable=1"),))
    deadline = time.monotonic() + _CONFIG["timeout"]
    copy.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    copy.execute("BEGIN")
    indexes = 0
    for table in _CONFIG["tables"]:
        name = _ident(table["name"])
        keep = set(column.lower() for column in table["columns"])
        info = copy.execute("PRAGMA src.table_info(" + name + ")").fetchall()
        columns = [(str(row[1]), str(row[2] or ""), int(row[5])) for row in info
                   if str(row[1]).lower() in keep]
        if not columns:
            continue
        listed = copy.execute("PRAGMA src.index_list(" + name + ")").fetchall()
        # INTEGER PRIMARY KEY 하나뿐인 rowid 표면 그 열은 rowid 별칭이다. 사본에서도 별칭으로 두어
        # id 조인이 원본처럼 빠르게 한다(WITHOUT ROWID 표는 pk 인덱스가 따로 있어 빠진다).
        keys = [row for row in info if row[5]]
        alias = None
        if (len(keys) == 1 and str(keys[0][2]).upper() == "INTEGER"
                and not any(row[3] == "pk" for row in listed)):
            alias = str(keys[0][1])
        definitions = []
        for column, declared, _ in columns:
            if column == alias:
                definitions.append(_ident(column) + " INTEGER PRIMARY KEY")
                continue
            kind = declared if _TYPE.match(declared) else ""
            definitions.append(_ident(column) + (" " + kind if kind else ""))
        copy.execute("CREATE TABLE main." + name + " (" + ", ".join(definitions) + ")")
        selected = ", ".join(_ident(column) for column, _, _ in columns)
        copy.execute(
            "INSERT INTO main." + name + " (" + selected + ") SELECT " + selected
            + " FROM src." + name
        )
        # 보이는 열만으로 된 인덱스만 옮긴다(숨긴 열의 인덱스에는 그 값이 들어 있다).
        wanted = []
        for _, index_name, unique, _, partial in listed:
            if partial:
                continue
            parts = copy.execute("PRAGMA src.index_info(" + _ident(index_name) + ")").fetchall()
            if not parts or any(part[1] < 0 for part in parts):
                continue
            names = [str(part[2]) for part in sorted(parts)]
            if all(column.lower() in keep for column in names):
                wanted.append((bool(unique), names))
        seen = set()
        for unique, names in wanted:
            key = tuple(column.lower() for column in names)
            if key in seen:
                continue
            seen.add(key)
            indexes += 1
            copy.execute(
                "CREATE " + ("UNIQUE " if unique else "") + "INDEX main."
                + _ident("%s__i%d" % (table["name"], indexes)) + " ON " + name
                + " (" + ", ".join(_ident(column) for column in names) + ")"
            )
    copy.execute("COMMIT")
    skipped = []
    for view in _CONFIG["views"]:
        try:
            copy.execute(view["sql"])
        except sqlite3.Error as exc:
            skipped.append({"name": view["name"], "error": str(exc)[:300]})
    copy.execute("DETACH DATABASE src")
    copy.close()
    _write({"ok": True, "bytes": os.path.getsize(target), "indexes": indexes,
            "skipped": skipped, "elapsed_ms": int((time.monotonic() - started) * 1000)})


try:
    _main()
except MemoryError:
    _write({"ok": False, "kind": "memory_limit", "error": "메모리 상한을 넘었다"})
except sqlite3.Error as exc:
    message = str(exc)
    kind = "timeout" if "interrupted" in message else "file"
    _write({"ok": False, "kind": kind, "error": "사본을 만들지 못했다: %s" % message})
except Exception as exc:
    _write({"ok": False, "kind": "failed", "error": "%s: %s" % (type(exc).__name__, exc)})
"""


def _script(template: str) -> str:
    return template.replace("__SAMPLES__", _SAMPLES)


class PrepareFailed(SqlRunFailed):
    """준비 실패. code는 sql_prepare_failed, sql_runtime_missing, sql_memory_limit 등이다.

    file_id가 있으면 그 파일을 읽다가 실패했다(인코딩, 시트 이름, 깨진 파일).
    """

    def __init__(
        self, code: str, message: str, *, retryable: bool = False, file_id: str | None = None
    ) -> None:
        super().__init__(code, message, retryable=retryable)
        self.file_id = file_id


@dataclass(frozen=True)
class PrepareFile:
    file_id: str
    path: Path
    filename: str
    format: str
    table: str
    sheet: str | None
    header: bool
    encoding: str
    all_varchar: bool
    # 숨긴 열 이름. 질의 파일에 넣지 않고 샘플도 뜨지 않는다.
    hidden: Sequence[str] = ()
    table_hidden: bool = False


@dataclass(frozen=True)
class Prepared:
    tables: list[SqlTable]
    elapsed_ms: int
    # files 소스가 작업 폴더에 만든 질의 파일. sqlite 소스는 None이다.
    database: Path | None = None
    duckdb_version: str | None = None
    # sqlite 소스의 뷰 정의(이름, CREATE VIEW 글). 만든 순서다.
    views: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


@dataclass(frozen=True)
class CopyTable:
    name: str
    # 사본에 담을 보이는 열 이름(원본 대소문자).
    columns: Sequence[str]


@dataclass(frozen=True)
class SqliteCopy:
    path: Path
    bytes: int
    indexes: int
    elapsed_ms: int
    # 사본에 다시 만들지 못한 뷰 이름 → 엔진 오류.
    skipped: dict[str, str] = field(default_factory=dict[str, str])


def _tables(raw: object) -> list[SqlTable]:
    items = raw if isinstance(raw, list) else []
    tables: list[SqlTable] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        data: dict[str, Any] = {str(key): value for key, value in item.items()}
        columns = [
            SqlColumn(
                name=str(column.get("name")),
                type=str(column.get("type") or ""),
                samples=list(column.get("samples") or []),
            )
            for column in data.get("columns") or []
            if isinstance(column, dict)
        ]
        tables.append(
            SqlTable(
                name=str(data.get("name")),
                kind="view" if data.get("kind") == "view" else "table",
                columns=columns,
                row_count=data.get("row_count") if isinstance(data.get("row_count"), int) else None,
                file_id=data.get("file_id") or None,
                encoding=data.get("encoding") or None,
                error=str(data["error"]) if data.get("error") else None,
            )
        )
    return tables


def _failure(payload: Mapping[str, Any] | None, detail: str, timed_out: bool) -> PrepareFailed:
    if timed_out:
        return PrepareFailed(
            "sql_prepare_failed",
            f"준비가 {PREPARE_TIMEOUT_SEC}초 안에 끝나지 않았다",
            retryable=True,
        )
    if payload is None:
        return PrepareFailed(
            "sql_prepare_failed",
            f"준비 프로세스가 결과 없이 끝났다(메모리 상한일 수 있다): {detail}",
        )
    kind = str(payload.get("kind") or "failed")
    message = str(payload.get("error") or "준비하지 못했다")
    if kind == "runtime_missing":
        return PrepareFailed(
            "sql_runtime_missing", "DuckDB 런타임이 없다. 런타임을 먼저 준비한다", retryable=True
        )
    if kind == "memory_limit":
        return PrepareFailed("sql_memory_limit", message)
    file_id = payload.get("file_id")
    return PrepareFailed(
        "sql_prepare_failed", message, file_id=str(file_id) if file_id is not None else None
    )


async def _run(
    sandbox: SandboxBackend,
    handle: WorkspaceHandle,
    *,
    exec_id: str,
    script: str,
    memory_mb: int,
) -> dict[str, Any]:
    try:
        result = await run_script(
            sandbox,
            handle,
            exec_id=exec_id,
            script=script,
            output=OUTPUT_NAME,
            output_limit=OUTPUT_LIMIT,
            timeout_sec=PREPARE_TIMEOUT_SEC,
            memory_mb=memory_mb,
        )
    except SandboxError as exc:
        raise PrepareFailed("sql_prepare_failed", exc.message, retryable=True) from None
    payload = result.payload
    if result.timed_out or payload is None or payload.get("ok") is not True:
        raise _failure(payload, result.detail, result.timed_out)
    return payload


async def prepare_files(
    sandbox: SandboxBackend,
    handle: WorkspaceHandle,
    *,
    lib: Path,
    extensions_dir: Path,
    files: Sequence[PrepareFile],
    memory_mb: int,
    exec_id: str,
) -> Prepared:
    """CSV·TSV·xlsx를 작업 폴더의 source.duckdb로 옮기고 스냅샷을 뜬다."""
    database = handle.root / DATABASE_NAME
    # 확장은 잠그기 전에만 불러올 수 있다. auto는 cp949로 다시 읽을 수 있게 encodings를 미리 둔다.
    needed: list[str] = []
    if any(item.format == "xlsx" for item in files):
        needed.append("excel")
    if any(item.format != "xlsx" and item.encoding not in _BUILT_IN for item in files):
        needed.append("encodings")
    config: dict[str, Any] = {
        "output": OUTPUT_NAME,
        "lib": str(lib),
        "extensions": str(extensions_dir),
        "extensions_needed": needed,
        "database": str(database),
        "temp": str(handle.root / ".tmp" / "duckdb"),
        "memory_mb": duckdb_memory_mb(memory_mb),
        "files": [
            {
                "file_id": item.file_id,
                "path": str(item.path),
                "filename": item.filename,
                "format": item.format,
                "table": item.table,
                "sheet": item.sheet,
                "header": item.header,
                "encoding": item.encoding,
                "all_varchar": item.all_varchar,
                "hidden": list(item.hidden),
                "table_hidden": item.table_hidden,
            }
            for item in files
        ],
    }
    payload = await _run(
        sandbox,
        handle,
        exec_id=exec_id,
        script=render(_script(_FILES), config),
        memory_mb=memory_mb,
    )
    return Prepared(
        tables=_tables(payload.get("tables")),
        elapsed_ms=int(payload.get("elapsed_ms") or 0),
        database=database,
        duckdb_version=str(payload.get("version") or "") or None,
    )


async def snapshot_sqlite(
    sandbox: SandboxBackend,
    handle: WorkspaceHandle,
    *,
    database: Path,
    hidden: Mapping[str, Sequence[str]],
    hidden_tables: Sequence[str],
    memory_mb: int,
    exec_id: str,
) -> Prepared:
    """SQLite 원본의 표·뷰 스냅샷과 뷰 정의. 사본은 build_sqlite_copy가 만든다."""
    config: dict[str, Any] = {
        "output": OUTPUT_NAME,
        "database": str(database),
        "timeout": PREPARE_TIMEOUT_SEC - 30,
        "hidden": {
            table.lower(): [column.lower() for column in columns]
            for table, columns in hidden.items()
        },
        "hidden_tables": [name.lower() for name in hidden_tables],
    }
    payload = await _run(
        sandbox,
        handle,
        exec_id=exec_id,
        script=render(_script(_SQLITE), config),
        memory_mb=memory_mb,
    )
    views = [
        (str(item.get("name")), str(item.get("sql")))
        for item in payload.get("views") or []
        if isinstance(item, dict) and item.get("name") and item.get("sql")
    ]
    return Prepared(
        tables=_tables(payload.get("tables")),
        elapsed_ms=int(payload.get("elapsed_ms") or 0),
        views=views,
    )


COPY_NAME = "source.sqlite"


async def build_sqlite_copy(
    sandbox: SandboxBackend,
    handle: WorkspaceHandle,
    *,
    database: Path,
    tables: Sequence[CopyTable],
    views: Sequence[tuple[str, str]],
    memory_mb: int,
    exec_id: str,
) -> SqliteCopy:
    """숨긴 표·열을 뺀 질의용 사본을 작업 폴더에 만든다. 보이는 표는 보이는 열만, 뷰는 views에
    준 것(보이는 객체만 읽는 정의)만 다시 만들고, 인덱스는 보이는 열만으로 된 것만 옮긴다."""
    target = handle.root / COPY_NAME
    config: dict[str, Any] = {
        "output": OUTPUT_NAME,
        "source": str(database),
        "target": str(target),
        "timeout": PREPARE_TIMEOUT_SEC - 30,
        "tables": [{"name": item.name, "columns": list(item.columns)} for item in tables],
        "views": [{"name": name, "sql": sql} for name, sql in views],
    }
    payload = await _run(
        sandbox,
        handle,
        exec_id=exec_id,
        script=render(_script(_COPY), config),
        memory_mb=memory_mb,
    )
    skipped = {
        str(item.get("name")): str(item.get("error") or "")
        for item in payload.get("skipped") or []
        if isinstance(item, dict)
    }
    return SqliteCopy(
        path=target,
        bytes=int(payload.get("bytes") or 0),
        indexes=int(payload.get("indexes") or 0),
        elapsed_ms=int(payload.get("elapsed_ms") or 0),
        skipped=skipped,
    )
