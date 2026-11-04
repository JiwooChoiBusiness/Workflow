"""샌드박스 스크립트(3단계 설계 10.2, 10.5). 실제 실행은 tests/windows/test_sql_runner_*.py가 본다.

SQL·경로는 JSON 글 하나로 들어가 코드가 되지 않고, DuckDB 잠금 순서가 설계와 같다.
"""

import ast
import csv
import json
import time
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.knowledge.sql.prepare import (  # pyright: ignore[reportPrivateUsage]
    _COPY,
    _FILES,
    _SQLITE,
    _script,
)
from app.knowledge.sql.runner import (  # pyright: ignore[reportPrivateUsage]
    _COMMON,
    DUCKDB_LOCK,
    QueryRequest,
    duckdb_lock,
    duckdb_memory_mb,
    query_config,
    query_script,
    render,
)
from app.knowledge.sql.sources import RESERVED_PREFIXES
from app.sandbox.base import WorkspaceHandle

HANDLE = WorkspaceHandle(session_id=uuid.uuid4(), root=Path("C:/wf/kb-work/x"), backend="local")


def test_duckdb_lock_order_matches_the_design() -> None:
    # 자동 설치·로드를 끄고 → 파일 접근을 막고 → 자원 상한 → 마지막에 설정을 잠근다(10.5).
    assert duckdb_lock(256) == [
        "SET autoload_known_extensions=false",
        "SET autoinstall_known_extensions=false",
        "SET allowed_paths=[]",
        "SET enable_external_access=false",
        "SET memory_limit='256MB'",
        "SET threads=2",
        "SET max_temp_directory_size='1GB'",
        "SET lock_configuration=true",
    ]
    assert DUCKDB_LOCK[-1] == "SET lock_configuration=true"
    assert duckdb_memory_mb(512) == 256 and duckdb_memory_mb(64) == 64


def test_sql_cannot_break_out_of_the_script() -> None:
    sql = "SELECT 1 AS \"\"\"'''\\\nimport os; os.system('x')"
    request = QueryRequest(
        engine="sqlite",
        database=Path("C:/wf/storage/sql-sources/w/f"),
        sql=sql,
        max_rows=5,
        timeout_sec=3,
        memory_mb=512,
        tables=["Orders"],
        ctes=["T"],
        hidden={"Customers": ["Email"]},
        hidden_tables=["Secret"],
    )

    script = query_script(request, HANDLE)
    tree = ast.parse(script)

    # 설정은 문자열 리터럴 하나에만 있다.
    literal = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and '"sql"' in node.value
    )
    config = json.loads(literal)
    assert config["sql"] == sql
    assert config["tables"] == ["orders"] and config["ctes"] == ["t"]
    assert config["hidden"] == {"customers": ["email"]} and config["hidden_tables"] == ["secret"]
    assert "load_extension" not in config["functions"]
    # CTE 이름은 검증과 같은 규칙으로 러너도 거른다(카탈로그 이름·숨긴 표 이름은 CTE로 받지 않는다).
    assert config["reserved"] == list(RESERVED_PREFIXES)


def test_duckdb_query_config_carries_the_lock_and_temp_folder() -> None:
    request = QueryRequest(
        engine="duckdb",
        database=Path("C:/wf/cache/kb-sql/s.duckdb"),
        sql="SELECT 1",
        max_rows=200,
        timeout_sec=30,
        memory_mb=512,
        export_csv=True,
        lib=Path("C:/wf/cache/kb-sql/packages-abc"),
    )

    config = query_config(request, HANDLE)

    assert config["lock"] == duckdb_lock(256)
    assert config["temp"] == str(HANDLE.root / ".tmp" / "duckdb")
    assert config["csv"] is True and config["max_rows"] == 200
    ast.parse(query_script(request, HANDLE))


def test_prepare_scripts_parse() -> None:
    for template in (_FILES, _SQLITE, _COPY):
        ast.parse(render(_script(template), {"output": "result.json"}))


def common_functions(folder: Path, **config: Any) -> dict[str, Any]:
    """두 엔진이 같이 쓰는 스크립트 앞부분을 이 프로세스에서 불러 함수만 꺼낸다."""
    settings = {
        "output": "out.json",
        "csv_name": "result.csv",
        "csv": True,
        "csv_bytes": 1_000_000,
        "max_rows": 1,
        "rows_bytes": 1_000_000,
        **config,
    }
    namespace: dict[str, Any] = {"__file__": str(folder / "query.py")}
    # 샌드박스에서 돌릴 글을 시험에서 그대로 실행해 본다. 내용은 저장소의 템플릿이다.
    exec(compile(render(_COMMON, settings), "query.py", "exec"), namespace)  # noqa: S102
    return namespace


def test_truncated_csv_neutralizes_formulas_but_keeps_numbers_and_dates(tmp_path: Path) -> None:
    # CSV 수식 주입. =·+·-·@·탭·CR로 시작하는 글 값과 열 이름 앞에 '를 붙인다. 수·날짜는 그대로다.
    # 잘린 결과 CSV는 워크플로우 sql_query의 wf_file과 query_sql의 sql/<call>.csv가 같이 쓴다.
    functions = common_functions(tmp_path)
    columns = ["=cmd", "n", "money", "day", "plus", "at", "tab", "cr", "dash", "plain", "none"]
    columns += ["flag", "list"]
    row = (
        '=HYPERLINK("http://x")',
        -5,
        Decimal("-1.5"),
        date(2026, 1, 2),
        "+1",
        "@SUM(A1)",
        "\tx",
        "\rx",
        "-dash",
        "그대로",
        None,
        True,
        ["=a"],
    )

    functions["_finish"](columns, [row, row], time.monotonic())

    with (tmp_path / "result.csv").open(encoding="utf-8-sig", newline="") as file:
        header, first = list(csv.reader(file))[:2]
    assert header[0] == "'=cmd" and header[1:3] == ["n", "money"]
    assert first == [
        '\'=HYPERLINK("http://x")',
        "-5",
        "-1.5",
        "2026-01-02",
        "'+1",
        "'@SUM(A1)",
        "'\tx",
        "'\rx",
        "'-dash",
        "그대로",
        "",
        "true",
        '["=a"]',
    ]
    # JSON 결과(rows)는 값 그대로다. 중화는 표 프로그램이 여는 CSV에만 건다.
    result = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert result["truncated"] is True and result["csv"] == "result.csv"
    assert result["rows"][0][0] == '=HYPERLINK("http://x")' and result["rows"][0][1] == -5
