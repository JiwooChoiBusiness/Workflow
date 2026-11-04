"""sql_schema 노드(3단계 설계 10.3). 스냅샷에서 DDL 글을 만들고 DB에 닿지 않는다.

데이터 소스는 가짜 SqlPort(conftest의 FakeSql)다. 실제 서비스와 잇는 흐름은
tests/integration/test_sql_chatflow.py에서 본다.
"""

import uuid
from typing import Any

from app.knowledge.sql.sources import ColumnView, SourceView, TableView
from app.schemas.workflow import Graph
from app.workflow.graph import validate
from app.workflow.nodes.base import SqlSourceRef
from app.workflow.ports import SqlPortError


def schema_graph(kit: Any, make_node: Any, config: dict[str, Any]) -> Graph:
    return kit.graph(
        [
            make_node("start", "start", {"fields": []}),
            make_node("schema", "sql_schema", config),
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "ddl", "var": "schema.ddl"},
                        {"name": "tables", "var": "schema.tables"},
                        {"name": "glossary", "var": "schema.glossary"},
                    ]
                },
            ),
        ],
        ["start>schema", "schema>end_1"],
    )


async def test_ddl_comes_from_the_snapshot(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    graph = schema_graph(kit, make_node, {"source_id": str(sql_source_id)})
    port = fake_sql()

    run = await kit.run(graph, ports=kit.ports(sql=port))

    assert run.outcome.status == "succeeded", run.outcome.error
    outputs = run.outcome.outputs
    assert outputs["ddl"] == (
        "-- 데이터 소스: 매출 (DuckDB). SQL은 DuckDB 문법의 SELECT 하나로 쓴다.\n\n"
        "CREATE TABLE sales (\n"
        "  region VARCHAR, -- 예: '서울', '부산'\n"
        "  amount DOUBLE -- 예: 10.5\n"
        "); -- 판매, 3행\n\n"
        "-- 용어집\n-- 합계는 amount의 합이다"
    )
    assert "card" not in outputs["ddl"]
    assert outputs["tables"][0]["columns"] == [
        {"name": "region", "type": "VARCHAR", "description": ""},
        {"name": "amount", "type": "DOUBLE", "description": ""},
    ]
    assert outputs["glossary"] == "합계는 amount의 합이다"
    # DB에 닿지 않는다.
    assert port.executed == [] and port.validated == []


async def test_small_budget_lists_tables_and_warns(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    graph = schema_graph(
        kit,
        make_node,
        {"source_id": str(sql_source_id), "max_tokens": 500, "tables": ["sales", "gone"]},
    )
    port = fake_sql()
    # 표 하나의 열을 300개로 늘려 상한을 넘긴다.
    wide = tuple(ColumnView(name=f"c{index}", type="VARCHAR") for index in range(300))
    port.view = SourceView(
        dialect="duckdb",
        tables=(TableView(name="sales", kind="table", columns=wide, row_count=3),),
    )

    run = await kit.run(graph, ports=kit.ports(sql=port))

    completed = next(data for data in run.of("node.completed") if data["node_id"] == "schema")
    assert completed["status"] == "succeeded"
    assert "-- sales: 300열, 3행" in run.outcome.outputs["ddl"]
    assert any(w.startswith("schema_too_large:") for w in completed["warnings"])
    assert "table_not_found: 없는 표를 건너뛰었다: gone" in completed["warnings"]


async def test_missing_source_fails_the_node(kit: Any, make_node: Any, fake_sql: Any) -> None:
    graph = schema_graph(kit, make_node, {"source_id": str(uuid.uuid4())})

    run = await kit.run(graph, ports=kit.ports(sql=fake_sql()))

    assert run.outcome.status == "failed"
    assert run.outcome.error.code == "sql_source_not_found"
    unprepared = fake_sql()

    async def not_prepared(source_id: uuid.UUID) -> Any:
        raise SqlPortError("sql_source_not_prepared", "먼저 준비한다", stage="source")

    unprepared.describe = not_prepared
    second = await kit.run(
        schema_graph(kit, make_node, {"source_id": str(uuid.uuid4())}),
        ports=kit.ports(sql=unprepared),
    )
    assert second.outcome.error.code == "sql_source_not_prepared"


def test_validation_of_the_source(kit: Any, make_node: Any, sql_source_id: uuid.UUID) -> None:
    missing = schema_graph(kit, make_node, {})
    chosen = schema_graph(kit, make_node, {"source_id": str(sql_source_id)})
    paid = {sql_source_id: SqlSourceRef(id=sql_source_id, name="급여", paid=True)}

    assert [(i.code, i.field) for i in validate(missing, "workflow")] == [
        ("sql_source_required", "source_id")
    ]
    assert [i.code for i in validate(chosen, "workflow", sql_sources={})] == [
        "sql_source_not_found"
    ]
    [warning] = validate(chosen, "workflow", sql_sources=paid)
    assert (warning.level, warning.code) == ("warning", "paid_key_required")
    assert validate(chosen, "workflow") == []
