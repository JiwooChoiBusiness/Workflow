"""데이터 소스 콘솔 API(3단계 설계 10장, 12.1, M27).

sqlite 소스는 샌드박스 파이썬의 sqlite3만 쓰므로 DuckDB 런타임 없이 준비·질의가 실제 하위
프로세스로 돈다. files 소스는 런타임이 없으면 409 sql_runtime_missing이다(실제 DuckDB는
tests/windows/test_sql_runner_duckdb.py). 다른 워크스페이스의 데이터 소스는 404다.
"""

import asyncio
import os
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select, text

from app.config import Settings
from app.db.engine import Database
from app.db.models import SqlQueryLog, SqlSource, Workspace
from app.db.repositories.sql_sources import SqlSourceRepository
from app.db.types import new_id
from app.knowledge.sql.runner import EXTENSIONS_MARKER, extensions_dir, runtime_dir
from app.sandbox.base import SandboxBackend, WorkspaceHandle
from app.services import sql_runtime
from app.workflow.extract_runner import READY_MARKER, run_script

FETCH = {"X-Requested-With": "fetch"}
BASE = "/api/console/sql-sources"


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings


def sqlite_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / f"{uuid.uuid4().hex}.db"
    # sqlite3의 with는 커밋만 하고 닫지 않는다. 닫아야 Windows에서 파일을 지운다.
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, email TEXT, region TEXT);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL);
            CREATE VIEW big_orders AS SELECT id, amount FROM orders WHERE amount >= 100;
            INSERT INTO customers VALUES (1, '김하나', 'kim@example.com', '서울'),
                (2, '이두리', 'lee@example.com', '부산'), (3, '박세찬', 'park@example.com', '서울');
            INSERT INTO orders VALUES (1, 1, 120.5), (2, 1, 30.0), (3, 2, 99.0), (4, 3, 250.0);
            """
        )
    data = path.read_bytes()
    path.unlink()
    return data


async def create_source(client: AsyncClient, name: str, kind: str, **body: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name, "kind": kind, "data_policy": {"acknowledged": True}}
    payload.update(body)
    response = await client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def upload(client: AsyncClient, source_id: str, files: list[tuple[str, bytes]]) -> Any:
    return await client.post(
        f"{BASE}/{source_id}/files",
        files=[("files", (name, data, "application/octet-stream")) for name, data in files],
        headers=FETCH,
    )


async def ready_sqlite_source(
    client: AsyncClient, tmp_path: Path, *, name: str = "주문 DB"
) -> dict[str, Any]:
    source = await create_source(client, name, "sqlite")
    uploaded = await upload(client, source["id"], [("shop.db", sqlite_bytes(tmp_path))])
    assert uploaded.status_code == 200, uploaded.text
    prepared = await client.post(f"{BASE}/{source['id']}/prepare", json={})
    assert prepared.status_code == 200, prepared.text
    return prepared.json()


async def test_policy_kind_and_validation(logged_in: AsyncClient) -> None:
    missing = await logged_in.post(BASE, json={"name": "x", "data_policy": {"acknowledged": False}})
    paid = await logged_in.post(
        BASE, json={"name": "x", "data_policy": {"acknowledged": True, "key_tier": "paid"}}
    )
    created = await create_source(logged_in, "매출 파일", "files", slug="sales")
    taken = await logged_in.post(
        BASE, json={"name": "y", "slug": "sales", "data_policy": {"acknowledged": True}}
    )

    assert missing.status_code == 422
    assert missing.json()["error"]["details"][0]["type"] == "data_policy_not_acknowledged"
    assert paid.status_code == 422 and paid.json()["error"]["code"] == "paid_key_missing"
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "slug_taken"
    assert created["kind"] == "files" and created["dialect"] == "duckdb"
    assert created["status"] == {
        "state": "empty",
        "runtime_required": True,
        "message": "파일을 올린다",
    }
    assert created["limits"]["max_rows"] == 200 and created["approval"] == "none"
    listed = (await logged_in.get(BASE)).json()["items"]
    assert [item["slug"] for item in listed] == ["sales"]


async def test_files_source_needs_the_duckdb_runtime(
    logged_in: AsyncClient, tmp_path: Path
) -> None:
    source = await create_source(logged_in, "매출", "files")
    csv = "id,지역,금액\n1,서울,100\n2,부산,200\n".encode()

    uploaded = await upload(
        logged_in, source["id"], [("2024 매출.csv", csv), ("Sales Q1.tsv", b"a\tb\n1\t2\n")]
    )
    wrong = await upload(logged_in, source["id"], [("shop.db", b"x")])

    assert uploaded.status_code == 200, uploaded.text
    assert wrong.status_code == 415, wrong.text
    files = uploaded.json()["files"]
    assert [(item["table"], item["format"]) for item in files] == [
        ("t_2024", "csv"),
        ("sales_q1", "tsv"),
    ]
    assert wrong.status_code == 415 and wrong.json()["error"]["code"] == "unsupported_file_type"
    renamed = await logged_in.patch(
        f"{BASE}/{source['id']}",
        json={"files": [{"file_id": files[0]["file_id"], "table": "sales"}]},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["files"][0]["table"] == "sales"
    prepare = await logged_in.post(f"{BASE}/{source['id']}/prepare", json={})
    assert prepare.status_code == 409 and prepare.json()["error"]["code"] == "sql_runtime_missing"
    query = await logged_in.post(f"{BASE}/{source['id']}/query", json={"sql": "SELECT 1"})
    assert query.status_code == 409
    assert query.json()["error"]["code"] == "sql_source_not_prepared"
    runtime = (await logged_in.get("/api/console/sql-runtime")).json()
    assert runtime["state"] == "missing" and runtime["packages_ready"] is False
    assert runtime["extensions"] == ["excel", "encodings"]
    removed = await logged_in.delete(
        f"{BASE}/{source['id']}/files/{files[1]['file_id']}", headers=FETCH
    )
    assert [item["table"] for item in removed.json()["files"]] == ["sales"]


async def test_new_sources_fill_missing_limits_from_the_server_settings(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings: Settings = app.state.settings
    monkeypatch.setattr(settings, "sql_max_rows", 150)
    monkeypatch.setattr(settings, "sql_timeout_sec", 12)
    monkeypatch.setattr(settings, "sql_memory_mb", 768)
    from_settings = {
        "max_rows": 150,
        "timeout_sec": 12,
        "memory_mb": 768,
        "llm_rows": 50,
        "llm_bytes": 65536,
    }

    plain = await create_source(logged_in, "기본", "sqlite")
    partial = await create_source(logged_in, "일부", "sqlite", limits={"max_rows": 20})
    options = (await logged_in.get("/api/console/node-types")).json()["sql_options"]

    assert plain["limits"] == from_settings
    assert partial["limits"] == {**from_settings, "max_rows": 20}
    assert (
        options["default_max_rows"],
        options["default_timeout_sec"],
        options["default_memory_mb"],
        options["default_llm_rows"],
        options["default_llm_bytes"],
    ) == (150, 12, 768, 50, 65536)

    # 고칠 때 비운 칸은 저장된 값이고, 설정을 바꿔도 이미 만든 소스에는 소급하지 않는다.
    monkeypatch.setattr(settings, "sql_memory_mb", 2048)
    patched = await logged_in.patch(f"{BASE}/{partial['id']}", json={"limits": {"timeout_sec": 5}})
    stored = (await logged_in.get(f"{BASE}/{plain['id']}")).json()

    assert patched.status_code == 200, patched.text
    assert patched.json()["limits"] == {**from_settings, "max_rows": 20, "timeout_sec": 5}
    assert stored["limits"] == from_settings
    wrong = await logged_in.post(
        BASE,
        json={"name": "x", "limits": {"memory_mb": 64}, "data_policy": {"acknowledged": True}},
    )
    assert wrong.status_code == 422


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE. WAIT_TIMEOUT(0x102)이면 살아 있다.
        process = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not process:
            return False
        try:
            return kernel32.WaitForSingleObject(process, 0) == 0x102
        finally:
            kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def runtime_state(client: AsyncClient, wanted: str, seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while True:
        status = (await client.get("/api/console/sql-runtime")).json()
        if status["state"] == wanted or time.monotonic() > deadline:
            return status
        await asyncio.sleep(0.1)


async def test_a_stuck_runtime_install_stops_at_the_time_limit_and_can_be_installed_again(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(app.state.settings, "sql_runtime_install_timeout_sec", 4)
    pid_file = tmp_path / "pip.pid"
    work_dirs: list[Path] = []

    async def stuck(
        sandbox: SandboxBackend, handle: WorkspaceHandle, cache_dir: Path, *, exec_prefix: str
    ) -> tuple[Path, str]:
        # 응답 없이 붙잡힌 pip처럼 실제 하위 프로세스가 오래 잔다. 자기 시간 상한(300초)보다 먼저
        # 설치 전체의 상한(4초)에 걸려야 한다.
        work_dirs.append(handle.root)
        script = (
            "import os, time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(300)\n"
        )
        await run_script(
            sandbox,
            handle,
            exec_id=f"{exec_prefix}-pip",
            script=script,
            output="never.json",
            output_limit=1024,
            timeout_sec=300,
        )
        raise AssertionError("설치 시간 상한에 끊겨야 한다")

    monkeypatch.setattr(sql_runtime, "install_runtime", stuck)
    started = time.monotonic()
    accepted = await logged_in.post("/api/console/sql-runtime/prepare", json={})
    failed = await runtime_state(logged_in, "failed", 30)

    assert accepted.status_code == 202 and accepted.json()["state"] == "installing"
    assert failed["state"] == "failed", failed
    assert failed["error"]["code"] == "sql_runtime_install_timeout"
    assert "4초" in failed["error"]["message"]
    assert time.monotonic() - started < 20
    pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert not _alive(pid)
    assert work_dirs and not work_dirs[0].exists()

    async def quick(
        sandbox: SandboxBackend, handle: WorkspaceHandle, cache_dir: Path, *, exec_prefix: str
    ) -> tuple[Path, str]:
        # 202 응답이 installing을 보이게 조금 머문다.
        await asyncio.sleep(0.5)
        lib = runtime_dir(cache_dir)
        lib.mkdir(parents=True)
        (lib / READY_MARKER).write_text("duckdb==2.0.0\n", encoding="utf-8")
        extensions = extensions_dir(cache_dir)
        extensions.mkdir(parents=True)
        (extensions / EXTENSIONS_MARKER).write_text("v2.0.0\n", encoding="utf-8")
        return lib, "v2.0.0"

    monkeypatch.setattr(sql_runtime, "install_runtime", quick)
    retried = await logged_in.post("/api/console/sql-runtime/prepare", json={})
    ready = await runtime_state(logged_in, "ready", 10)

    assert retried.status_code == 202 and retried.json()["state"] == "installing"
    assert ready["state"] == "ready" and ready["error"] is None


async def test_two_install_requests_at_once_start_one_install(
    app: FastAPI, logged_in: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Path] = []
    release = asyncio.Event()

    async def slow(
        sandbox: SandboxBackend, handle: WorkspaceHandle, cache_dir: Path, *, exec_prefix: str
    ) -> tuple[Path, str]:
        started.append(handle.root)
        await release.wait()
        lib = runtime_dir(cache_dir)
        lib.mkdir(parents=True)
        (lib / READY_MARKER).write_text("duckdb==2.0.0\n", encoding="utf-8")
        extensions = extensions_dir(cache_dir)
        extensions.mkdir(parents=True)
        (extensions / EXTENSIONS_MARKER).write_text("v2.0.0\n", encoding="utf-8")
        return lib, "v2.0.0"

    monkeypatch.setattr(sql_runtime, "install_runtime", slow)
    runtime = app.state.sql_runtime

    first, second = await asyncio.gather(
        logged_in.post("/api/console/sql-runtime/prepare", json={}),
        logged_in.post("/api/console/sql-runtime/prepare", json={}),
    )
    task = runtime._install
    deadline = time.monotonic() + 10
    while not started and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    again = await logged_in.post("/api/console/sql-runtime/prepare", json={})

    assert [first.status_code, second.status_code] == [202, 202]
    assert {first.json()["state"], second.json()["state"]} == {"installing"}
    assert again.json()["state"] == "installing" and runtime._install is task
    release.set()
    ready = await runtime_state(logged_in, "ready", 10)
    assert ready["state"] == "ready"
    # 표시 파일을 읽는 사이에 두 요청이 겹쳐도 설치는 한 번만 돌았다.
    assert len(started) == 1


async def test_broken_or_partial_stored_limits_fall_back_to_the_server_settings(
    app: FastAPI, logged_in: AsyncClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings: Settings = app.state.settings
    monkeypatch.setattr(settings, "sql_timeout_sec", 12)
    monkeypatch.setattr(settings, "sql_memory_mb", 768)
    broken = await create_source(logged_in, "깨진 한도", "sqlite")
    partial = await create_source(logged_in, "빠진 한도", "sqlite")
    async with db.write() as session:
        for source_id, limits in (
            (broken["id"], {"max_rows": 0, "timeout_sec": "x"}),
            (partial["id"], {"max_rows": 40}),
        ):
            row = await session.get(SqlSource, uuid.UUID(source_id))
            assert row is not None
            row.limits = limits

    read_broken = (await logged_in.get(f"{BASE}/{broken['id']}")).json()
    read_partial = (await logged_in.get(f"{BASE}/{partial['id']}")).json()

    from_settings = {
        "max_rows": 200,
        "timeout_sec": 12,
        "memory_mb": 768,
        "llm_rows": 50,
        "llm_bytes": 65536,
    }
    assert read_broken["limits"] == from_settings
    # 빠진 칸만 설정값으로 채우고 저장된 칸은 그대로다.
    assert read_partial["limits"] == {**from_settings, "max_rows": 40}


async def test_sqlite_source_prepares_and_answers_queries(
    logged_in: AsyncClient, db: Database, tmp_path: Path
) -> None:
    source = await ready_sqlite_source(logged_in, tmp_path)

    assert source["status"]["state"] == "ready" and source["dialect"] == "sqlite"
    tables = {table["name"]: table for table in source["schema"]["tables"]}
    assert set(tables) == {"customers", "orders", "big_orders"}
    assert tables["big_orders"]["kind"] == "view"
    assert tables["customers"]["row_count"] == 3
    region = next(c for c in tables["customers"]["columns"] if c["name"] == "region")
    assert region["samples"] == ["서울", "부산"]

    ok = await logged_in.post(
        f"{BASE}/{source['id']}/query",
        json={
            "sql": "SELECT c.region, SUM(o.amount) AS total FROM orders o "
            "JOIN customers c ON c.id = o.customer_id GROUP BY c.region ORDER BY total DESC"
        },
    )
    rejected = await logged_in.post(
        f"{BASE}/{source['id']}/query", json={"sql": "DELETE FROM orders"}
    )
    broken = await logged_in.post(
        f"{BASE}/{source['id']}/query", json={"sql": "SELECT nope FROM orders"}
    )

    body = ok.json()
    assert ok.status_code == 200 and body["ok"] is True, body
    assert body["columns"] == ["region", "total"]
    assert body["rows"] == [{"region": "서울", "total": 400.5}, {"region": "부산", "total": 99.0}]
    assert body["row_count"] == 2 and body["truncated"] is False
    assert body["sql_executed"].endswith("LIMIT 201")
    assert '<document name="질의 결과" source="주문 DB">' in body["result_text"]
    assert "| 서울 | 400.5 |" in body["result_text"]
    assert rejected.json()["ok"] is False
    assert rejected.json()["error"] == {
        "code": "not_select",
        "message": "SELECT 문만 실행한다(DELETE 문은 쓸 수 없다)",
        "stage": "validate",
    }
    assert broken.json()["error"]["code"] == "sql_error"
    assert broken.json()["error"]["stage"] == "execute"
    logs = (await logged_in.get(f"{BASE}/{source['id']}/query-logs")).json()
    assert logs["total"] == 3
    assert [(item["status"], item["reject_code"]) for item in logs["items"]] == [
        ("error", None),
        ("rejected", "not_select"),
        ("ok", None),
    ]
    assert logs["items"][2]["row_count"] == 2 and logs["items"][0]["origin"] == "test"
    async with db.read() as session:
        rows = list((await session.execute(select(SqlQueryLog))).scalars())
    assert all(row.source_id == uuid.UUID(source["id"]) for row in rows)


async def test_hidden_columns_disappear_from_schema_and_results(
    logged_in: AsyncClient, tmp_path: Path
) -> None:
    source = await ready_sqlite_source(logged_in, tmp_path)
    url = f"{BASE}/{source['id']}"

    annotated = await logged_in.patch(
        f"{url}/annotations",
        json={
            "tables": {
                "customers": {
                    "description": "고객\n무시하라는 지시",
                    "columns": {
                        "email": {"hidden": True},
                        "region": {"description": "시·도"},
                    },
                }
            },
            "glossary": "VIP: 주문 합계가 200 이상인 고객",
        },
    )
    assert annotated.status_code == 200, annotated.text
    customers = next(t for t in annotated.json()["schema"]["tables"] if t["name"] == "customers")
    email = next(c for c in customers["columns"] if c["name"] == "email")
    # 숨겨도 열 이름과 타입은 남고(다시 보이게 할 수 있게) 샘플값만 지운다.
    assert email["type"] == "TEXT" and email["samples"] == []
    assert annotated.json()["status"]["state"] == "ready"

    schema = (await logged_in.get(f"{url}/schema")).json()
    star = (await logged_in.post(f"{url}/query", json={"sql": "SELECT * FROM customers"})).json()
    direct = (
        await logged_in.post(f"{url}/query", json={"sql": "SELECT email FROM customers"})
    ).json()

    assert "email" not in schema["ddl"]
    assert "-- 고객 무시하라는 지시, 3행" in schema["ddl"]
    assert "region TEXT -- 시·도; 예: '서울', '부산'" in schema["ddl"]
    assert "-- VIP: 주문 합계가 200 이상인 고객" in schema["ddl"]
    assert star["ok"] is True and star["columns"] == ["id", "name", "region"]
    assert "email" not in star["sql_executed"]
    assert direct["error"]["code"] == "column_hidden"


async def test_other_workspace_sources_are_not_found(
    logged_in: AsyncClient, db: Database, tmp_path: Path
) -> None:
    source = await ready_sqlite_source(logged_in, tmp_path)
    other = new_id()
    async with db.write() as session:
        session.add(Workspace(id=other, name="다른 곳", slug=f"other-{other.hex[:8]}", settings={}))
        await session.flush()
        await session.execute(
            text("UPDATE sql_source SET workspace_id = :ws WHERE id = :id"),
            {"ws": str(other), "id": source["id"]},
        )
    url = f"{BASE}/{source['id']}"

    responses = [
        await logged_in.get(url),
        await logged_in.patch(url, json={"name": "남의 것"}),
        await logged_in.post(f"{url}/prepare", json={}),
        await logged_in.post(f"{url}/query", json={"sql": "SELECT 1"}),
        await logged_in.get(f"{url}/query-logs"),
        await logged_in.get(f"{url}/schema"),
        await logged_in.patch(f"{url}/annotations", json={"tables": {}}),
        await upload(logged_in, source["id"], [("x.db", b"x")]),
        await logged_in.delete(url, headers=FETCH),
    ]

    assert [response.status_code for response in responses] == [404] * len(responses)
    assert (await logged_in.get(BASE)).json()["items"] == []
    async with db.read() as session:
        assert await SqlSourceRepository(session, other).get_active(uuid.UUID(source["id"]))


async def test_schema_export_and_examples_go_to_knowledge_bases(
    app: FastAPI, logged_in: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services.knowledge_index_executor import KnowledgeLane

    # 문서는 waiting으로 생기기만 하면 된다. 지식 레인은 돌리지 않는다.
    monkeypatch.setattr(KnowledgeLane, "index", lambda self, workspace_id, document_id: False)
    source = await ready_sqlite_source(logged_in, tmp_path)
    url = f"{BASE}/{source['id']}"

    async def make_kb(name: str, doc_form: str) -> dict[str, Any]:
        response = await logged_in.post(
            "/api/console/knowledge",
            json={
                "name": name,
                "doc_form": doc_form,
                "indexing_technique": "economy",
                "data_policy": {"acknowledged": True},
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    schema_kb = await make_kb("스키마", "text_model")
    examples_kb = await make_kb("예시 SQL", "qa_model")

    exported = await logged_in.post(f"{url}/export-schema", json={"kb_id": schema_kb["id"]})
    saved = await logged_in.post(
        f"{url}/examples",
        json={
            "kb_id": examples_kb["id"],
            "question": "지역별 주문 합계는?",
            "sql": "SELECT region, SUM(amount) FROM orders GROUP BY 1",
        },
    )
    twice = await logged_in.post(
        f"{url}/examples",
        json={
            "kb_id": examples_kb["id"],
            "question": "지역별 주문 합계는?",
            "sql": "SELECT region, SUM(amount) FROM orders GROUP BY 1",
        },
    )
    not_qa = await logged_in.post(
        f"{url}/examples", json={"kb_id": schema_kb["id"], "question": "q", "sql": "SELECT 1"}
    )

    assert exported.status_code == 201, exported.text
    assert sorted(item["table"] for item in exported.json()["documents"]) == [
        "big_orders",
        "customers",
        "orders",
    ]
    documents = (await logged_in.get(f"/api/console/knowledge/{schema_kb['id']}/documents")).json()
    assert sorted(item["name"] for item in documents["items"]) == [
        "주문 DB · big_orders",
        "주문 DB · customers",
        "주문 DB · orders",
    ]
    assert {item["data_source_type"] for item in documents["items"]} == {"api"}
    # 다시 내보내면 새 문서를 만든 뒤 같은 이름의 이전 문서를 지운다(쌓이지 않는다).
    again = await logged_in.post(f"{url}/export-schema", json={"kb_id": schema_kb["id"]})
    assert again.status_code == 201, again.text
    assert sorted(again.json()["removed"]) == sorted(
        item["document_id"] for item in exported.json()["documents"]
    )
    assert again.json()["failed"] == []
    after = (await logged_in.get(f"/api/console/knowledge/{schema_kb['id']}/documents")).json()
    assert sorted(item["id"] for item in after["items"]) == sorted(
        item["document_id"] for item in again.json()["documents"]
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["name"] == "주문 DB SQL 예시: 지역별 주문 합계는?"
    assert twice.status_code == 409 and twice.json()["error"]["code"] == "duplicate_document"
    assert not_qa.status_code == 422 and not_qa.json()["error"]["code"] == "kb_not_qa"


async def test_replacing_the_sqlite_file_makes_the_source_stale(
    logged_in: AsyncClient, tmp_path: Path, make_client: Callable[[], AsyncClient]
) -> None:
    source = await ready_sqlite_source(logged_in, tmp_path)

    replaced = await upload(logged_in, source["id"], [("other.sqlite", sqlite_bytes(tmp_path))])
    query = await logged_in.post(f"{BASE}/{source['id']}/query", json={"sql": "SELECT 1"})

    assert replaced.status_code == 200, replaced.text
    assert len(replaced.json()["files"]) == 1
    assert replaced.json()["files"][0]["filename"] == "other.sqlite"
    assert replaced.json()["status"]["state"] == "stale"
    assert query.status_code == 409
    deleted = await logged_in.delete(f"{BASE}/{source['id']}", headers=FETCH)
    assert deleted.status_code == 204
    assert (await logged_in.get(f"{BASE}/{source['id']}")).status_code == 404


def view_db(tmp_path: Path) -> bytes:
    """보이는 표·숨길 열·숨길 표와 그것들을 읽는 뷰, 깨진 뷰가 든 원본."""
    path = tmp_path / f"{uuid.uuid4().hex}.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            """
            CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, email TEXT, region TEXT);
            CREATE INDEX customers_region ON customers(region);
            CREATE INDEX customers_email ON customers(email);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL);
            CREATE TABLE secret(code TEXT);
            CREATE TABLE gone(x INTEGER);
            CREATE VIEW v_public AS SELECT id, name, region FROM customers;
            CREATE VIEW v_contacts AS SELECT name, email AS contact FROM customers;
            CREATE VIEW v_secret AS SELECT code FROM secret;
            CREATE VIEW v_broken AS SELECT x FROM gone;
            DROP TABLE gone;
            INSERT INTO customers VALUES (1, '김하나', 'kim@example.com', '서울'),
                (2, '이두리', 'lee@example.com', '부산');
            INSERT INTO orders VALUES (1, 1, 120.5), (2, 2, 99.0);
            INSERT INTO secret VALUES ('s3cr3t-code');
            """
        )
    data = path.read_bytes()
    path.unlink()
    return data


async def test_hidden_settings_build_a_copy_and_hide_views_that_read_them(
    app: FastAPI, logged_in: AsyncClient, tmp_path: Path
) -> None:
    source = await create_source(logged_in, "뷰 DB", "sqlite")
    url = f"{BASE}/{source['id']}"
    uploaded = await upload(logged_in, source["id"], [("views.db", view_db(tmp_path))])
    assert uploaded.status_code == 200, uploaded.text
    plain = (await logged_in.post(f"{url}/prepare", json={})).json()
    tables = {item["name"]: item for item in plain["schema"]["tables"]}
    # 깨진 뷰는 그 뷰만 오류로 남고 스냅샷은 성공한다.
    assert tables["v_broken"]["error"] and tables["v_broken"]["columns"] == []
    assert [item["code"] for item in plain["schema"]["warnings"]] == ["view_broken"]
    assert plain["materialized"] is None and plain["status"]["state"] == "ready"

    hidden = await logged_in.patch(
        f"{url}/annotations",
        json={
            "tables": {
                "customers": {"columns": {"email": {"hidden": True}}},
                "secret": {"hidden": True},
            }
        },
    )

    assert hidden.status_code == 200, hidden.text
    body = hidden.json()
    assert body["status"]["state"] == "ready", body["status"]
    warnings = {(item["code"], item["table"]) for item in body["schema"]["warnings"]}
    assert warnings == {
        ("view_exposes_hidden", "v_contacts"),
        ("view_exposes_hidden", "v_secret"),
        ("view_broken", "v_broken"),
    }
    assert body["annotations"]["tables"]["v_contacts"]["hidden"] is True
    assert body["annotations"]["tables"]["v_secret"]["hidden"] is True
    for name in ("v_contacts", "v_secret"):
        exposed = next(t for t in body["schema"]["tables"] if t["name"] == name)
        # 숨긴 열·표를 읽는 뷰의 샘플은 비어 DDL·스키마 내보내기로 나가지 않는다.
        assert all(column["samples"] == [] for column in exposed["columns"]), name
    runtime = app.state.sql_runtime
    copy = runtime.copy_path(uuid.UUID(source["id"]))
    assert body["materialized"]["filename"] == copy.name and body["materialized"]["bytes"] > 0
    with closing(sqlite3.connect(copy)) as conn:
        objects = set(conn.execute("SELECT type, name FROM sqlite_master").fetchall())
        columns = [row[1] for row in conn.execute("PRAGMA table_info(customers)")]
        primary = [
            (row[1], row[5]) for row in conn.execute("PRAGMA table_info(customers)") if row[5]
        ]
        indexed = sorted(
            (table, [row[2] for row in conn.execute(f'PRAGMA index_info("{name}")')])
            for table in ("customers", "orders")
            for _, name, *_ in conn.execute(f"PRAGMA index_list({table})")
        )
    assert {("table", "customers"), ("table", "orders"), ("view", "v_public")} <= objects
    assert not {name for _, name in objects} & {"secret", "v_contacts", "v_secret", "v_broken"}
    assert columns == ["id", "name", "region"]
    # 보이는 열의 인덱스만 옮기고(email 인덱스는 없다) INTEGER PRIMARY KEY는 rowid 별칭으로 둔다.
    assert indexed == [("customers", ["region"])]
    assert primary == [("id", 1)]
    raw = copy.read_bytes()
    assert b"kim@example.com" not in raw and b"s3cr3t-code" not in raw

    ddl = (await logged_in.get(f"{url}/schema")).json()["ddl"]
    public = await logged_in.post(
        f"{url}/query", json={"sql": "SELECT * FROM v_public ORDER BY id"}
    )
    contact = await logged_in.post(f"{url}/query", json={"sql": "SELECT contact FROM v_contacts"})
    using = await logged_in.post(
        f"{url}/query",
        json={
            "sql": "SELECT * FROM customers c JOIN (SELECT 'kim@example.com' AS email) g "
            "USING (email)"
        },
    )
    # 콘솔 시험부터 러너까지 통째로: USING(숨긴 열)은 검증이 막고 기록에 거부로 남는다.
    assert using.json()["ok"] is False and using.json()["error"]["code"] == "column_hidden"

    assert "CREATE VIEW v_public" in ddl and "v_contacts" not in ddl and "secret" not in ddl
    assert public.json()["rows"] == [
        {"id": 1, "name": "김하나", "region": "서울"},
        {"id": 2, "name": "이두리", "region": "부산"},
    ]
    assert contact.json()["error"]["code"] == "table_not_allowed"

    cleared = await logged_in.patch(f"{url}/annotations", json={"tables": {}})

    # 숨김이 없으면 사본을 지우고 원본을 그대로 읽는다.
    assert cleared.json()["materialized"] is None and not copy.exists()
    assert cleared.json()["status"]["state"] == "ready"


async def test_changing_hidden_columns_without_preparing_blocks_queries(
    logged_in: AsyncClient, tmp_path: Path
) -> None:
    source = await ready_sqlite_source(logged_in, tmp_path)
    url = f"{BASE}/{source['id']}"

    changed = await logged_in.patch(
        f"{url}/annotations",
        json={"tables": {"customers": {"columns": {"email": {"hidden": True}}}}, "prepare": False},
    )
    blocked = await logged_in.post(f"{url}/query", json={"sql": "SELECT name FROM customers"})
    prepared = await logged_in.post(f"{url}/prepare", json={})
    answered = await logged_in.post(f"{url}/query", json={"sql": "SELECT name FROM customers"})

    assert changed.json()["status"]["state"] == "stale"
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "sql_source_not_prepared"
    assert prepared.json()["status"]["state"] == "ready"
    assert answered.json()["ok"] is True and answered.json()["row_count"] == 3


async def test_deleted_sources_keep_their_files_for_seven_days_after_deletion(
    app: FastAPI, logged_in: AsyncClient, db: Database, tmp_path: Path
) -> None:
    from datetime import timedelta

    from app.db.types import utcnow
    from app.services.sql_sources import SqlMaintenance

    source = await ready_sqlite_source(logged_in, tmp_path)
    async with db.read() as session:
        row = await session.get(SqlSource, uuid.UUID(source["id"]))
        assert row is not None
        key = str(row.files[0]["storage_key"])
        workspace_id = row.workspace_id
    storage = app.state.storage
    path = storage.local_path(key)
    orphan = path.parent / str(uuid.uuid4())
    orphan.write_bytes(b"x")
    old = time.time() - 30 * 24 * 3600
    # 올린 지 오래된 원본이라도 지운 시각에서 7일을 센다.
    os.utime(path, (old, old))
    os.utime(orphan, (old, old))
    deleted = await logged_in.delete(f"{BASE}/{source['id']}", headers=FETCH)
    assert deleted.status_code == 204
    now = utcnow()

    def maintenance(days: float) -> SqlMaintenance:
        return SqlMaintenance(
            db,
            app.state.settings,
            storage,
            app.state.sql_runtime,
            clock=lambda: now + timedelta(days=days),
        )

    soon = await maintenance(1).purge_loose_files()

    assert soon == 1 and path.exists() and not orphan.exists()
    later = await maintenance(8).purge_loose_files()
    assert later == 1 and not path.exists()
    assert str(workspace_id) in key
