"""sql_query 노드(3단계 설계 10.3~10.6). 검증·실행, 잘린 결과 CSV, 실패 출력, 승인 폼.

데이터 소스는 가짜 SqlPort(conftest의 FakeSql)이고 검증은 실제 validate_sql이다. 승인 폼은
human_input과 같은 폼 대기 기계로 멈추고 RunContext.reply로 재개한다.
"""

import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any

from app.schemas.workflow import WF_EVENT_DATA_MODELS, Graph
from app.streaming.event_bus import jsonable
from app.workflow.engine import NodeReply
from app.workflow.graph import validate
from app.workflow.ports import SqlPortError

OUTPUTS = [
    "columns",
    "rows",
    "row_count",
    "truncated",
    "sql_executed",
    "result_text",
    "file",
    "error",
]


def query_graph(
    kit: Any, make_node: Any, config: dict[str, Any], *, error: dict[str, Any] | None = None
) -> Graph:
    extra = {"error": error} if error is not None else {}
    return kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "q", "type": "paragraph"}]}),
            make_node("run_sql", "sql_query", config, **extra),
            make_node(
                "end_1",
                "end",
                {"outputs": [{"name": name, "var": f"run_sql.{name}"} for name in OUTPUTS]},
            ),
        ],
        ["start>run_sql", "run_sql>end_1"],
    )


class FakeFiles:
    def __init__(self) -> None:
        self.saved: list[tuple[Path, str, str | None]] = []

    async def save_output(self, source: Path, *, filename: str, mime: str | None) -> dict[str, Any]:
        self.saved.append((source, filename, mime))
        return {
            "file_id": str(uuid.uuid4()),
            "filename": filename,
            "mime": mime,
            "size_bytes": 10,
            "storage_key": "wf-files/w/x",
            "source": "wf_file",
        }

    async def read_bytes(self, ref: Any, *, max_bytes: int) -> bytes:
        raise AssertionError("쓰지 않는다")

    async def copy_to(self, ref: Any, dest: Path, *, max_bytes: int) -> int:
        raise AssertionError("쓰지 않는다")


async def resume(kit: Any, graph: Graph, checkpoint: Any, reply: NodeReply, **options: Any) -> Any:
    context = dataclasses.replace(kit.context(graph, resume=checkpoint, **options), reply=reply)
    run = kit.engine.run(context)
    async with run:
        async for event in run:
            payload = jsonable(event.data)
            WF_EVENT_DATA_MODELS[event.type].model_validate(payload)
    return run.outcome


ANSWER = (
    "지역별 합계는 아래 SQL로 구한다.\n```sql\n"
    "SELECT region, SUM(amount) AS total FROM sales GROUP BY region\n```"
)


async def test_sql_from_the_fence_runs_and_fills_the_outputs(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    graph = query_graph(kit, make_node, {"source_id": str(sql_source_id), "sql": "{{ start.q }}"})
    assert validate(graph, "workflow") == []
    port = fake_sql()

    run = await kit.run(graph, inputs={"q": ANSWER}, ports=kit.ports(sql=port))

    assert run.outcome.status == "succeeded", run.outcome.error
    out = run.outcome.outputs
    executed = "SELECT region, SUM(amount) AS total FROM sales GROUP BY region LIMIT 201"
    assert port.executed == [(executed, 200, 30)]
    assert out["sql_executed"] == executed
    assert out["columns"] == ["region", "total"]
    assert out["rows"] == [{"region": "서울", "total": 30.5}, {"region": "부산", "total": 10.0}]
    assert out["row_count"] == 2 and out["truncated"] is False
    assert out["file"] is None and out["error"] == ""
    assert out["result_text"].startswith('<document name="질의 결과" source="매출">')
    completed = next(d for d in run.of("node.completed") if d["node_id"] == "run_sql")
    assert completed["warnings"] == []


async def test_truncated_results_become_a_csv_file(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    graph = query_graph(
        kit,
        make_node,
        {"source_id": str(sql_source_id), "sql": "SELECT region FROM sales", "max_rows": 5000},
    )
    port = fake_sql(truncated=True, max_rows_cap=1000)
    files = FakeFiles()

    run = await kit.run(graph, ports=kit.ports(sql=port, files=files))

    out = run.outcome.outputs
    # max_rows는 SQL_MAX_ROWS(가짜 포트의 상한 1000)까지만이다.
    assert port.executed[0][1] == 1000 and out["sql_executed"].endswith("LIMIT 1001")
    assert out["truncated"] is True
    assert [(name, mime) for _, name, mime in files.saved] == [("run_sql-result.csv", "text/csv")]
    assert out["file"]["filename"] == "run_sql-result.csv"
    completed = next(d for d in run.of("node.completed") if d["node_id"] == "run_sql")
    assert "truncated: 결과가 1000행을 넘어 잘렸다" in completed["warnings"]
    no_export = query_graph(
        kit,
        make_node,
        {"source_id": str(sql_source_id), "sql": "SELECT region FROM sales", "export_file": False},
    )
    again = await kit.run(no_export, ports=kit.ports(sql=fake_sql(truncated=True), files=files))
    assert again.outcome.outputs["file"] is None and len(files.saved) == 1


async def test_rejected_sql_fails_or_feeds_the_error_output(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    config = {"source_id": str(sql_source_id), "sql": "SELECT card FROM sales"}

    failed = await kit.run(query_graph(kit, make_node, config), ports=kit.ports(sql=fake_sql()))
    handled = await kit.run(
        query_graph(kit, make_node, config, error={"strategy": "default"}),
        ports=kit.ports(sql=fake_sql()),
    )

    assert failed.outcome.status == "failed"
    assert failed.outcome.error.code == "column_hidden"
    assert handled.outcome.status == "succeeded"
    out = handled.outcome.outputs
    # 기본값 전략이면 엔진이 노드가 낸 error·sql_executed를 기본값보다 먼저 쓴다(고쳐 다시 돌리기).
    assert out["error"] == "column_hidden: 열 sales.card은(는) 숨긴 열이라 쓸 수 없다"
    assert out["sql_executed"] == "SELECT card FROM sales"
    assert out["rows"] is None and out["row_count"] is None


async def test_execution_failures_carry_the_engine_message(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    port = fake_sql(fail=SqlPortError("sql_error", "Binder Error: column nope", stage="execute"))
    graph = query_graph(
        kit,
        make_node,
        {"source_id": str(sql_source_id), "sql": "SELECT region FROM sales"},
        error={"strategy": "default", "default_outputs": {"error": "무시됨", "row_count": 0}},
    )

    run = await kit.run(graph, ports=kit.ports(sql=port))

    out = run.outcome.outputs
    assert out["error"] == "sql_error: Binder Error: column nope"
    assert out["row_count"] == 0


async def test_approval_waits_with_the_validated_sql_and_runs_the_edited_one(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    graph = query_graph(
        kit,
        make_node,
        {"source_id": str(sql_source_id), "sql": "{{ start.q }}", "approval": "always"},
    )
    port = fake_sql()

    first = await kit.run(graph, inputs={"q": ANSWER}, ports=kit.ports(sql=port))

    assert first.outcome.status == "waiting_input"
    pending = first.outcome.pending
    assert pending.kind == "form" and pending.node_id == "run_sql"
    form = pending.form
    assert form.description.startswith("**SQL 승인** · 데이터 소스 매출")
    assert [(f.name, f.type, f.required, f.options) for f in form.fields] == [
        ("sql", "paragraph", True, None),
        ("decision", "select", True, ["approve", "reject"]),
    ]
    validated = "SELECT region, SUM(amount) AS total FROM sales GROUP BY region LIMIT 201"
    assert form.fields[0].default == validated and form.fields[1].default == "approve"
    assert port.executed == []

    edited = "SELECT region FROM sales WHERE amount > 10"
    reply = NodeReply(
        "run_sql", None, {"form_id": form.form_id, "values": {"sql": edited, "decision": "approve"}}
    )
    outcome = await resume(
        kit, graph, first.outcome.checkpoint, reply, inputs={"q": ANSWER}, ports=kit.ports(sql=port)
    )

    assert outcome.status == "succeeded", outcome.error
    assert port.executed == [(f"{edited} LIMIT 201", 200, 30)]


async def test_approval_reject_and_a_write_statement_are_refused(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    graph = query_graph(kit, make_node, {"source_id": str(sql_source_id), "sql": "{{ start.q }}"})
    # 노드에 approval이 없으면 데이터 소스의 approval을 따른다.
    port = fake_sql(approval="always")
    first = await kit.run(graph, inputs={"q": ANSWER}, ports=kit.ports(sql=port))
    form = first.outcome.pending.form

    rejected = await resume(
        kit,
        graph,
        first.outcome.checkpoint,
        NodeReply("run_sql", None, {"form_id": form.form_id, "values": {"decision": "reject"}}),
        inputs={"q": ANSWER},
        ports=kit.ports(sql=port),
    )
    # 보안 확인표 KS11: 승인 카드에서 쓰기 문장으로 고쳐 승인해도 다시 검증해 거부한다.
    rewritten = await resume(
        kit,
        graph,
        first.outcome.checkpoint,
        NodeReply(
            "run_sql",
            None,
            {
                "form_id": form.form_id,
                "values": {"sql": "DELETE FROM sales", "decision": "approve"},
            },
        ),
        inputs={"q": ANSWER},
        ports=kit.ports(sql=port),
    )

    assert rejected.status == "failed" and rejected.error.code == "sql_rejected"
    assert rewritten.status == "failed" and rewritten.error.code == "not_select"
    assert port.executed == []


async def test_a_refused_sql_does_not_open_the_approval_form(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    graph = query_graph(
        kit,
        make_node,
        {"source_id": str(sql_source_id), "sql": "DROP TABLE sales", "approval": "always"},
    )

    run = await kit.run(graph, ports=kit.ports(sql=fake_sql()))

    assert run.outcome.status == "failed" and run.outcome.error.code == "not_select"
    assert run.outcome.pending is None


def test_config_validation(kit: Any, make_node: Any, sql_source_id: uuid.UUID) -> None:
    empty = query_graph(kit, make_node, {"source_id": str(sql_source_id)})
    by_var = query_graph(kit, make_node, {"source_id": str(sql_source_id), "sql_var": "start.q"})
    missing_var = query_graph(
        kit, make_node, {"source_id": str(sql_source_id), "sql_var": "start.nope"}
    )

    assert [(i.code, i.field) for i in validate(empty, "workflow")] == [("sql_required", "sql")]
    assert validate(by_var, "workflow") == []
    assert [i.code for i in validate(missing_var, "workflow")] == ["unknown_variable"]


async def test_the_executed_sql_is_masked_like_other_outputs(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    hidden_value = "sk-region-secret-7781"
    graph = query_graph(
        kit,
        make_node,
        {
            "source_id": str(sql_source_id),
            "sql": "SELECT region FROM sales WHERE region = '{{ env.REGION }}'",
        },
    )
    port = fake_sql()

    run = await kit.run(
        graph, env={"REGION": hidden_value}, secret_env=["REGION"], ports=kit.ports(sql=port)
    )

    assert run.outcome.status == "succeeded", run.outcome.error
    # 실행에는 실제 값이 가고, 이벤트·출력의 sql_executed는 다른 출력처럼 마스킹된다.
    assert hidden_value in port.executed[0][0]
    completed = next(d for d in run.of("node.completed") if d["node_id"] == "run_sql")
    assert "sql_executed" in completed["outputs_preview"]
    assert hidden_value not in json.dumps(jsonable(completed), ensure_ascii=False)
    assert hidden_value not in json.dumps(run.outcome.outputs, ensure_ascii=False)


async def test_an_expired_approval_fails_with_input_timeout(
    kit: Any, make_node: Any, fake_sql: Any, sql_source_id: uuid.UUID
) -> None:
    graph = query_graph(
        kit,
        make_node,
        {"source_id": str(sql_source_id), "sql": "SELECT region FROM sales", "approval": "always"},
    )
    port = fake_sql()
    first = await kit.run(graph, ports=kit.ports(sql=port))
    form = first.outcome.pending.form

    outcome = await resume(
        kit,
        graph,
        first.outcome.checkpoint,
        NodeReply("run_sql", None, {"form_id": form.form_id, "timeout": True}),
        ports=kit.ports(sql=port),
    )

    assert outcome.status == "failed" and outcome.error.code == "input_timeout"
    assert port.executed == []
