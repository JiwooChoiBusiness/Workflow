"""Text2SQL의 백엔드 흐름(3단계 설계 10.3~10.6, 11장, 14.4, M27).

text_to_sql 템플릿 챗플로우가 가짜 어댑터 각본으로 SQL을 쓰고 실행해 표로 답하며, 실패하면 고치는
갈래로 한 번 다시 돈다. 승인 폼은 콘솔 /input으로 고친 SQL을 받아 다시 검증한 뒤 실행한다. 데이터
소스를 붙인 에이전트는 describe_sql_source·query_sql을 쓰고 잘린 결과를 작업공간 CSV로 받는다.
유료 데이터 소스는 런 전체(하위 에이전트 포함)를 paid로 만들고, DSL은 source_slug로 다시 잇는다.

데이터 소스는 sqlite라 샌드박스 파이썬의 sqlite3만 쓴다(DuckDB 런타임 없이 실제 하위 프로세스로
돈다).
"""

import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr

from app.config import Settings
from app.db.models import SqlSource
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ModelEvent, ToolResultBlock
from app.services.sql_sources import WorkflowSqlPort

FETCH = {"X-Requested-With": "fetch"}
BASE = "/api/console/sql-sources"
DRAFT_FIELDS = (
    "instructions",
    "model",
    "tools",
    "limits",
    "sandbox",
    "approval_mode",
    "skill_version_ids",
    "file_ids",
    "sub_agents",
    "updated_at",
)
Turn = Callable[[ChatRequest], list[ModelEvent]]


@pytest.fixture
def settings(short_settings: Settings) -> Settings:
    return short_settings.model_copy(
        update={"llm_fake": True, "gemini_paid_api_key": SecretStr("paid-" + "7" * 30)}
    )


@pytest.fixture
def app(scripted_app: FastAPI) -> FastAPI:
    return scripted_app


def shop_db(tmp_path: Path) -> bytes:
    path = tmp_path / f"{uuid.uuid4().hex}.db"
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
    data = path.read_bytes()
    path.unlink()
    return data


async def make_source(
    client: AsyncClient, tmp_path: Path, *, slug: str = "shop", tier: str = "free"
) -> dict[str, Any]:
    created = await client.post(
        BASE,
        json={
            "name": "주문 DB",
            "slug": slug,
            "kind": "sqlite",
            "description": "쇼핑몰 주문",
            "data_policy": {"acknowledged": True, "key_tier": tier},
        },
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    uploaded = await client.post(
        f"{BASE}/{source_id}/files",
        files=[("files", ("shop.db", shop_db(tmp_path), "application/octet-stream"))],
        headers=FETCH,
    )
    assert uploaded.status_code == 200, uploaded.text
    prepared = await client.post(f"{BASE}/{source_id}/prepare", json={})
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["status"]["state"] == "ready"
    return prepared.json()


async def query_logs(client: AsyncClient, source_id: str) -> list[tuple[str, str, str | None]]:
    items = (await client.get(f"{BASE}/{source_id}/query-logs")).json()["items"]
    return [(item["origin"], item["status"], item["reject_code"]) for item in reversed(items)]


def node(
    node_id: str, node_type: str, config: dict[str, Any], x: float = 0, **extra: Any
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": node_type,
        "title": node_id,
        "position": {"x": x, "y": 0},
        "config": config,
        **extra,
    }


def chain(*ids: str) -> list[dict[str, Any]]:
    return [
        {"id": f"e{index}", "source": source, "target": target}
        for index, (source, target) in enumerate(zip(ids, ids[1:], strict=False), start=1)
    ]


async def save_flow(client: AsyncClient, graph: dict[str, Any], *, name: str = "주문 조회") -> str:
    created = await client.post("/api/console/workflows", json={"name": name, "kind": "workflow"})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    current = (await client.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    saved = await client.put(
        f"/api/console/workflows/{workflow_id}/draft",
        json={
            "graph": graph,
            "env_vars": [],
            "conversation_vars": [],
            "settings": current["settings"],
            "updated_at": current["updated_at"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert [issue for issue in saved.json()["issues"] if issue["level"] == "error"] == []
    return workflow_id


def replying(seen: list[ChatRequest], text: str) -> Turn:
    def turn(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return [*fake_text(text), fake_end()]

    return turn


def said(text: str) -> list[ModelEvent]:
    return [*fake_text(text), fake_end()]


TOTALS = (
    "```sql\nSELECT c.region, SUM(o.amount) AS total FROM orders o "
    "JOIN customers c ON c.id = o.customer_id GROUP BY c.region ORDER BY total DESC\n```"
)
TABLE_ANSWER = "| 지역 | 합계 |\n| --- | --- |\n| 서울 | 400.5 |\n| 부산 | 99.0 |"


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


async def node_runs(client: AsyncClient, run_id: str) -> dict[str, dict[str, Any]]:
    detail = (await client.get(f"/api/console/wf-runs/{run_id}")).json()
    return {item["node_id"]: item for item in detail["node_runs"]}


async def test_text_to_sql_template_answers_with_a_table(
    logged_in: AsyncClient, script: Any, tmp_path: Path
) -> None:
    source = await make_source(logged_in, tmp_path)
    created = await logged_in.post(
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
    draft = (await logged_in.get(f"/api/console/workflows/{workflow_id}/draft")).json()
    assert draft["issues"] == []
    sql_nodes = [item for item in draft["graph"]["nodes"] if item["type"].startswith("sql_")]
    assert {item["config"]["source_id"] for item in sql_nodes} == {source["id"]}
    seen: list[ChatRequest] = []
    script.add(replying(seen, TOTALS), replying(seen, TABLE_ANSWER))

    body = await chat(logged_in, workflow_id, "지역별 주문 합계를 표로 보여 줘")

    assert body["run"]["status"] == "succeeded", body["run"]
    assert body["answer"] == TABLE_ANSWER
    write_sql, answer_llm = seen
    # SQL 만들기 프롬프트에는 스냅샷으로 만든 DDL이 들어간다.
    assert "-- 데이터 소스: 주문 DB (SQLite). SQL은 SQLite 문법의 SELECT 하나로 쓴다." in (
        write_sql.system
    )
    assert "CREATE TABLE customers (" in write_sql.system
    # 답변 프롬프트에는 실행한 SQL과 데이터 표식으로 감싼 결과 표가 들어간다.
    assert '<document name="질의 결과" source="주문 DB">' in answer_llm.system
    assert "| 서울 | 400.5 |" in answer_llm.system and "| 부산 | 99.0 |" in answer_llm.system
    assert "LIMIT 201" in answer_llm.system
    runs = await node_runs(logged_in, body["run"]["id"])
    assert runs["run_sql"]["status"] == "succeeded"
    assert runs["fix_sql"]["status"] == "skipped"
    assert body["run"]["limits"]["tier"] == "free"
    assert await query_logs(logged_in, source["id"]) == [("node", "ok", None)]


async def test_a_failed_sql_takes_the_fix_branch_once(
    logged_in: AsyncClient, script: Any, tmp_path: Path
) -> None:
    source = await make_source(logged_in, tmp_path)
    created = await logged_in.post(
        "/api/console/workflows",
        json={
            "name": "주문 분석",
            "kind": "chatflow",
            "template": "text_to_sql",
            "sql_source_id": source["id"],
        },
    )
    workflow_id = created.json()["id"]
    seen: list[ChatRequest] = []
    script.add(
        replying(seen, "```sql\nSELECT region, SUM(amount) FROM payments GROUP BY region\n```"),
        replying(seen, TOTALS),
        replying(seen, TABLE_ANSWER),
    )

    body = await chat(logged_in, workflow_id, "지역별 합계")

    assert body["run"]["status"] == "succeeded", body["run"]
    assert body["answer"] == TABLE_ANSWER
    fix_sql = seen[1]
    # 고치기 프롬프트에 실패한 SQL과 거부 이유(쓸 수 있는 표 목록)가 들어간다.
    assert "FROM payments" in fix_sql.system
    assert "table_not_allowed: 표 payments은(는) 이 데이터 소스에 없다" in fix_sql.system
    assert "쓸 수 있는 표: customers, orders" in fix_sql.system
    assert "| 서울 | 400.5 |" in seen[2].system
    runs = await node_runs(logged_in, body["run"]["id"])
    assert runs["run_sql"]["status"] == "failed"
    assert runs["run_sql"]["error"]["code"] == "table_not_allowed"
    assert runs["rerun_sql"]["status"] == "succeeded"
    assert await query_logs(logged_in, source["id"]) == [
        ("node", "rejected", "table_not_allowed"),
        ("node", "ok", None),
    ]


async def test_a_rejected_approval_answers_with_a_fixed_sentence_and_skips_the_fix(
    logged_in: AsyncClient, script: Any, tmp_path: Path
) -> None:
    source = await make_source(logged_in, tmp_path)
    # 템플릿의 SQL 노드는 approval을 비워 두어 데이터 소스의 승인 방식을 따른다.
    patched = await logged_in.patch(f"{BASE}/{source['id']}", json={"approval": "always"})
    assert patched.status_code == 200, patched.text
    created = await logged_in.post(
        "/api/console/workflows",
        json={
            "name": "주문 분석",
            "kind": "chatflow",
            "template": "text_to_sql",
            "sql_source_id": source["id"],
        },
    )
    workflow_id = created.json()["id"]
    seen: list[ChatRequest] = []
    script.add(replying(seen, TOTALS))

    waiting = await chat(logged_in, workflow_id, "지역별 합계")
    assert waiting["run"]["status"] == "waiting_input", waiting["run"]
    shown = waiting["run"]["pending_interaction"]["form"]["fields"][0]["default"]
    finished = await logged_in.post(
        f"/api/console/wf-runs/{waiting['run']['id']}/input",
        json={
            "kind": "form",
            "values": {"sql": shown, "decision": "reject"},
            "response_mode": "blocking",
        },
    )

    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "succeeded", finished.json()
    messages = (
        await logged_in.get(f"/api/console/wf-conversations/{waiting['conversation_id']}/messages")
    ).json()["items"]
    assert messages[-1]["text"] == (
        "SQL 실행이 승인되지 않았거나 승인 대기 시간이 지나 이번 질문에는 답하지 않았습니다."
    )
    # 모델은 SQL 만들기 한 번만 불렀다. 거절한 SQL을 고치거나 빈 결과로 답하지 않는다.
    assert len(seen) == 1
    runs = await node_runs(logged_in, waiting["run"]["id"])
    assert runs["run_sql"]["status"] == "failed"
    assert runs["run_sql"]["error"]["code"] == "sql_rejected"
    assert runs["answer_rejected"]["status"] == "succeeded"
    for skipped in ("answer_llm", "fix_sql", "rerun_sql", "answer_llm_2"):
        assert runs[skipped]["status"] == "skipped", (skipped, runs[skipped]["status"])
    assert await query_logs(logged_in, source["id"]) == []


async def test_a_node_failure_without_outputs_takes_the_fix_branch_not_the_answer(
    logged_in: AsyncClient, script: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = await make_source(logged_in, tmp_path)
    created = await logged_in.post(
        "/api/console/workflows",
        json={
            "name": "주문 분석",
            "kind": "chatflow",
            "template": "text_to_sql",
            "sql_source_id": source["id"],
        },
    )
    workflow_id = created.json()["id"]

    async def broken(*args: Any, **kwargs: Any) -> Any:
        # 노드가 잡지 않는 예외. 엔진이 출력 없는 node_error로 끝낸다(시간 상한도 같은 모양이다).
        raise RuntimeError("실행 환경이 갑자기 멈췄다")

    monkeypatch.setattr(WorkflowSqlPort, "execute", broken)
    seen: list[ChatRequest] = []
    script.add(
        replying(seen, TOTALS),
        replying(seen, TOTALS),
        replying(seen, "SQL을 실행하지 못해 답하지 못했습니다."),
    )

    body = await chat(logged_in, workflow_id, "지역별 합계")

    assert body["run"]["status"] == "succeeded", body["run"]
    assert body["answer"] == "SQL을 실행하지 못해 답하지 못했습니다."
    runs = await node_runs(logged_in, body["run"]["id"])
    assert runs["run_sql"]["status"] == "failed"
    assert runs["run_sql"]["error"]["code"] == "node_error"
    # 빈 결과로 답하는 성공 가지가 아니라 고치기 가지로 갔고, 두 프롬프트에 기본값 error가 실렸다.
    assert runs["answer_llm"]["status"] == "skipped"
    assert runs["fix_sql"]["status"] == "succeeded"
    write_sql, fix_sql, answer_llm_2 = seen
    assert "node_failed: 노드가 결과 없이 끝났다" in fix_sql.system
    assert "오류: node_failed: 노드가 결과 없이 끝났다" in answer_llm_2.system


def approval_flow(source_id: str) -> dict[str, Any]:
    nodes = [
        node("start", "start", {"fields": []}),
        node(
            "run_sql",
            "sql_query",
            {
                "source_id": source_id,
                "sql": "SELECT name, region FROM customers",
                "approval": "always",
            },
            200,
        ),
        node(
            "end_1",
            "end",
            {
                "outputs": [
                    {"name": "rows", "var": "run_sql.rows"},
                    {"name": "sql", "var": "run_sql.sql_executed"},
                ]
            },
            400,
        ),
    ]
    return {"nodes": nodes, "edges": chain("start", "run_sql", "end_1")}


async def test_approval_form_runs_the_edited_sql_after_checking_it_again(
    logged_in: AsyncClient, tmp_path: Path
) -> None:
    source = await make_source(logged_in, tmp_path)
    workflow_id = await save_flow(logged_in, approval_flow(source["id"]))

    async def start() -> dict[str, Any]:
        response = await logged_in.post(
            f"/api/console/workflows/{workflow_id}/draft/runs",
            json={"inputs": {}, "response_mode": "blocking"},
        )
        assert response.status_code == 200, response.text
        return response.json()

    run = await start()

    assert run["status"] == "waiting_input", run
    pending = run["pending_interaction"]
    assert pending["kind"] == "form"
    fields = {field["name"]: field for field in pending["form"]["fields"]}
    assert fields["sql"]["default"] == "SELECT name, region FROM customers LIMIT 201"
    assert fields["decision"]["options"] == ["approve", "reject"]
    assert pending["form"]["description"].startswith("**SQL 승인** · 데이터 소스 주문 DB")
    # 승인 전에는 실행하지 않는다.
    assert await query_logs(logged_in, source["id"]) == []
    edited = "SELECT name FROM customers WHERE region = '서울' ORDER BY id"

    finished = await logged_in.post(
        f"/api/console/wf-runs/{run['id']}/input",
        json={
            "kind": "form",
            "values": {"sql": edited, "decision": "approve"},
            "response_mode": "blocking",
        },
    )

    assert finished.status_code == 200, finished.text
    body = finished.json()
    assert body["status"] == "succeeded", body
    assert body["outputs"]["rows"] == [{"name": "김하나"}, {"name": "박세찬"}]
    assert body["outputs"]["sql"] == f"{edited} LIMIT 201"

    # 승인 카드에서 쓰기 문장으로 바꾸면 다시 검증해 거부한다(보안 확인표 KS11).
    second = await start()
    refused = await logged_in.post(
        f"/api/console/wf-runs/{second['id']}/input",
        json={
            "kind": "form",
            "values": {"sql": "DELETE FROM customers", "decision": "approve"},
            "response_mode": "blocking",
        },
    )
    third = await start()
    rejected = await logged_in.post(
        f"/api/console/wf-runs/{third['id']}/input",
        json={"kind": "form", "values": {"decision": "reject"}, "response_mode": "blocking"},
    )

    assert refused.json()["status"] == "failed"
    assert refused.json()["error"]["code"] == "not_select"
    assert rejected.json()["status"] == "failed"
    assert rejected.json()["error"]["code"] == "sql_rejected"
    assert await query_logs(logged_in, source["id"]) == [
        ("node", "ok", None),
        ("node", "rejected", "not_select"),
    ]


async def save_agent(client: AsyncClient, agent_id: str, **draft: Any) -> dict[str, Any]:
    current = (await client.get(f"/api/console/agents/{agent_id}/draft")).json()
    body = {key: current[key] for key in DRAFT_FIELDS}
    body.update({"env_vars": [], "tools": [], **draft})
    saved = await client.put(f"/api/console/agents/{agent_id}/draft", json=body)
    assert saved.status_code == 200, saved.text
    return saved.json()


async def make_agent(
    client: AsyncClient,
    name: str,
    slug: str,
    *,
    publish: bool = True,
    **draft: Any,
) -> dict[str, Any]:
    created = await client.post(
        "/api/console/agents", json={"name": name, "slug": slug, "description": f"{name} 일"}
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    saved = await save_agent(client, agent["id"], **draft)
    agent["draft"] = saved
    if publish:
        published = await client.post(f"/api/console/agents/{agent['id']}/publish", json={})
        assert published.status_code == 201, published.text
    return agent


async def preview(client: AsyncClient, agent_id: str, text: str) -> dict[str, Any]:
    session = (
        await client.post(f"/api/console/agents/{agent_id}/preview/sessions", json={})
    ).json()
    response = await client.post(
        f"/api/console/sessions/{session['id']}/messages",
        json={"text": text, "response_mode": "blocking"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_agent_queries_its_source_and_keeps_the_full_result_as_csv(
    logged_in: AsyncClient, script: Any, tmp_path: Path
) -> None:
    source = await make_source(logged_in, tmp_path)
    agent = await make_agent(logged_in, "주문 분석가", "analyst", sql_sources=[source["id"]])
    assert agent["draft"]["sql_sources"] == [source["id"]]
    assert agent["draft"]["warnings"] == []
    seen: list[ChatRequest] = []

    def describe(request: ChatRequest) -> list[ModelEvent]:
        seen.append(request)
        return [
            *fake_tool_call("describe_sql_source", {"source": "shop"}, call_id="call_d"),
            fake_end("tool_use"),
        ]

    query = fake_tool_call(
        "query_sql",
        {"source": "shop", "sql": "SELECT id, amount FROM orders ORDER BY id", "max_rows": 2},
        call_id="call_q",
    )
    script.add(describe, [*query, fake_end("tool_use")], replying(seen, "주문은 4건이다."))

    body = await preview(logged_in, agent["id"], "주문을 보여 줘")

    assert body["run"]["status"] == "succeeded", body["run"]
    first, last = seen
    assert [tool.name for tool in first.tools] == ["describe_sql_source", "query_sql"]
    assert "# 데이터 소스" in first.system
    assert "- shop: 주문 DB (SQLite) — 쇼핑몰 주문" in first.system
    described, queried = (
        block.content
        for message in last.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    )
    assert "CREATE TABLE orders (" in described
    assert "결과가 2행을 넘어 잘렸다" in queried
    assert "행 상한까지 받은 행만 작업공간 sql/call_q.csv에 CSV로 저장했다." in queried
    events = (await logged_in.get(f"/api/console/runs/{body['run']['id']}/events")).json()
    completed = [item["data"] for item in events["items"] if item["type"] == "tool_call.completed"]
    assert completed[0].get("sql") is None
    assert completed[1]["sql"] == {
        "sql_executed": "SELECT id, amount FROM orders ORDER BY id LIMIT 3",
        "row_count": 2,
        "truncated": True,
    }
    changed = [item["data"]["path"] for item in events["items"] if item["type"] == "file.changed"]
    assert "sql/call_q.csv" in changed
    assert await query_logs(logged_in, source["id"]) == [("tool", "ok", None)]


async def test_an_agent_without_sources_has_no_sql_tools(
    logged_in: AsyncClient, script: Any
) -> None:
    agent = await make_agent(logged_in, "비서", "helper", publish=False)
    seen: list[ChatRequest] = []
    script.add(replying(seen, "안녕"))

    await preview(logged_in, agent["id"], "안녕")

    assert seen[0].tools == [] and "# 데이터 소스" not in seen[0].system


def paid_flow(source_id: str) -> dict[str, Any]:
    nodes = [
        node("start", "start", {"fields": []}),
        node(
            "run_sql",
            "sql_query",
            {"source_id": source_id, "sql": "SELECT COUNT(*) AS n FROM orders"},
            200,
        ),
        node("llm_1", "llm", {"prompt": [{"role": "user", "text": "{{ run_sql.result_text }}"}]}),
        node("end_1", "end", {"outputs": [{"name": "answer", "var": "llm_1.text"}]}, 600),
    ]
    return {"nodes": nodes, "edges": chain("start", "run_sql", "llm_1", "end_1")}


async def test_a_paid_source_makes_the_runs_that_reach_it_paid(
    app: FastAPI, logged_in: AsyncClient, script: Any, tmp_path: Path
) -> None:
    source = await make_source(logged_in, tmp_path, tier="paid")
    assert source["data_policy"]["key_tier"] == "paid"
    workflow_id = await save_flow(logged_in, paid_flow(source["id"]))
    script.add(said("4건"))

    started = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {}, "response_mode": "blocking"},
    )

    run = started.json()
    assert run["status"] == "succeeded", run["error"]
    assert run["outputs"] == {"answer": "4건"}
    assert run["limits"]["tier"] == "paid"
    assert app.state.llm.tiers == ["paid"]

    researcher = await make_agent(logged_in, "조사", "researcher", sql_sources=[source["id"]])
    assert [(item["code"], item["source_id"]) for item in researcher["draft"]["warnings"]] == [
        ("paid_key_required", source["id"])
    ]
    conductor = await make_agent(
        logged_in, "지휘자", "conductor", publish=False, sub_agents=[{"agent_id": researcher["id"]}]
    )
    assert ("paid_key_required", researcher["id"]) in {
        (item["code"], item["agent_id"]) for item in conductor["draft"]["warnings"]
    }
    script.add(
        [
            *fake_tool_call("call_agent", {"agent": "researcher", "task": "주문 수를 센다"}),
            fake_end("tool_use"),
        ],
        said("4건"),
        said("주문은 4건이다"),
    )

    body = await preview(logged_in, conductor["id"], "주문이 몇 건이야?")

    assert body["run"]["status"] == "succeeded", body["run"]
    # 지휘자는 데이터 소스가 없어도 하위 에이전트가 유료 소스를 쓰므로 첫 호출부터 유료 키다.
    assert body["run"]["limits"]["tier"] == "paid"
    assert app.state.llm.tiers == ["paid"] * 4


async def test_without_the_paid_key_paid_sources_block_runs_and_publishing(
    app: FastAPI, logged_in: AsyncClient, tmp_path: Path
) -> None:
    source = await make_source(logged_in, tmp_path, tier="paid")
    workflow_id = await save_flow(logged_in, paid_flow(source["id"]))
    agent = await make_agent(
        logged_in, "분석", "analyst", publish=False, sql_sources=[source["id"]]
    )
    settings: Settings = app.state.settings
    app.state.settings = settings.model_copy(update={"gemini_paid_api_key": None})

    started = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {}, "response_mode": "blocking"},
    )
    flow_refused = await logged_in.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    agent_refused = await logged_in.post(f"/api/console/agents/{agent['id']}/publish", json={})

    assert started.status_code == 422, started.text
    assert started.json()["error"]["code"] == "paid_key_missing"
    assert flow_refused.status_code == 422, flow_refused.text
    assert flow_refused.json()["error"]["code"] == "paid_key_missing"
    assert agent_refused.status_code == 422, agent_refused.text
    details = agent_refused.json()["error"]["details"]
    assert [(item["type"], item["loc"][-2:]) for item in details] == [
        ("paid_key_missing", ["sql_sources", 0])
    ]
    assert app.state.llm.tiers == []


async def test_dsl_relinks_by_source_slug_and_flags_a_deleted_source(
    logged_in: AsyncClient, tmp_path: Path
) -> None:
    source = await make_source(logged_in, tmp_path)
    graph = {
        "nodes": [
            node("start", "start", {"fields": []}),
            node("schema", "sql_schema", {"source_id": source["id"]}, 200),
            node(
                "run_sql",
                "sql_query",
                {"source_id": source["id"], "sql": "SELECT COUNT(*) AS n FROM orders"},
                400,
            ),
            node("end_1", "end", {"outputs": [{"name": "ddl", "var": "schema.ddl"}]}, 600),
        ],
        "edges": chain("start", "schema", "run_sql", "end_1"),
    }
    workflow_id = await save_flow(logged_in, graph)
    exported = await logged_in.post(f"/api/console/workflows/{workflow_id}/export", headers=FETCH)
    assert exported.status_code == 200, exported.text
    text = exported.text
    assert text.count("source_slug: shop") == 2
    # 다른 PC에서 내보낸 파일처럼 id를 바꾸면 slug로 이 워크스페이스의 데이터 소스를 찾는다.
    moved = text.replace(source["id"], str(uuid.uuid4()))

    imported = await logged_in.post(
        "/api/console/workflows/import",
        files={"file": ("sql.yml", moved.encode("utf-8"), "application/yaml")},
        headers=FETCH,
    )

    assert imported.status_code == 201, imported.text
    body = imported.json()
    relinked = [
        item for item in body["converted"]["warnings"] if item["code"] == "sql_source_relinked"
    ]
    assert len(relinked) == 2
    assert body["issues"] == []
    draft = (await logged_in.get(f"/api/console/workflows/{body['workflow_id']}/draft")).json()
    for item in draft["graph"]["nodes"]:
        if item["type"] in ("sql_schema", "sql_query"):
            assert item["config"]["source_id"] == source["id"]
            assert "source_slug" not in item["config"]

    deleted = await logged_in.delete(f"{BASE}/{source['id']}", headers=FETCH)
    assert deleted.status_code == 204, deleted.text
    again = (await logged_in.get(f"/api/console/workflows/{body['workflow_id']}/draft")).json()
    assert sorted((item["code"], item["node_id"]) for item in again["issues"]) == [
        ("sql_source_not_found", "run_sql"),
        ("sql_source_not_found", "schema"),
    ]
    refused = await logged_in.post(f"/api/console/workflows/{body['workflow_id']}/publish", json={})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "draft_invalid"
    # 지운 데이터 소스의 slug는 다시 쓸 수 있고, 이름이 같아도 id가 달라 옛 그래프는 잇지 않는다.
    imported_again = await logged_in.post(
        "/api/console/workflows/import",
        files={"file": ("sql.yml", moved.encode("utf-8"), "application/yaml")},
        headers=FETCH,
    )
    assert {item["code"] for item in imported_again.json()["converted"]["warnings"]} >= {
        "sql_source_not_found"
    }


async def test_node_types_list_the_sql_nodes_and_options(logged_in: AsyncClient) -> None:
    response = await logged_in.get("/api/console/node-types")

    assert response.status_code == 200, response.text
    body = response.json()
    types = {item["type"]: item for item in body["items"]}
    assert [o["name"] for o in types["sql_schema"]["outputs"]] == ["ddl", "tables", "glossary"]
    assert [o["name"] for o in types["sql_query"]["outputs"]] == [
        "columns",
        "rows",
        "row_count",
        "truncated",
        "sql_executed",
        "result_text",
        "file",
        "error",
    ]
    options = body["sql_options"]
    assert options["max_rows_max"] == 1000 and options["timeout_max"] == 60
    assert options["default_max_rows"] == 200 and options["approvals"] == ["none", "always"]


async def test_the_approval_form_is_answered_through_the_service_api_and_can_be_stopped(
    logged_in: AsyncClient, make_client: Callable[[], AsyncClient], tmp_path: Path
) -> None:
    source = await make_source(logged_in, tmp_path)
    workflow_id = await save_flow(logged_in, approval_flow(source["id"]))
    published = await logged_in.post(f"/api/console/workflows/{workflow_id}/publish", json={})
    assert published.status_code == 201, published.text
    created = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/api-keys", json={"name": "자동화"}
    )
    key = {"Authorization": f"Bearer {created.json()['key']}"}
    api = make_client()

    started = await api.post(
        "/api/v1/workflows/run",
        headers=key,
        json={"inputs": {}, "user": "u-1", "response_mode": "blocking"},
    )
    body = started.json()
    assert body["status"] == "waiting_input" and body["pending_interaction"]["kind"] == "form"
    finished = await api.post(
        f"/api/v1/workflows/run/{body['workflow_run_id']}/input",
        headers=key,
        json={
            "kind": "form",
            "values": {"sql": "SELECT name FROM customers WHERE id = 2", "decision": "approve"},
            "user": "u-1",
            "response_mode": "blocking",
        },
    )

    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "succeeded", finished.json()
    assert finished.json()["outputs"]["rows"] == [{"name": "이두리"}]

    waiting = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {}, "response_mode": "blocking"},
    )
    run_id = waiting.json()["id"]
    stopped = await logged_in.post(f"/api/console/wf-runs/{run_id}/stop", headers=FETCH)
    late = await logged_in.post(
        f"/api/console/wf-runs/{run_id}/input",
        json={"kind": "form", "values": {"decision": "approve"}},
    )

    assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
    assert late.status_code == 409 and late.json()["error"]["code"] == "run_not_waiting"
    # 중지된 승인은 실행하지 않는다(질의 기록은 서비스 API 런의 한 번뿐이고 출처는 노드다).
    assert await query_logs(logged_in, source["id"]) == [("node", "ok", None)]


async def test_a_resumed_run_follows_a_source_that_became_paid(
    app: FastAPI, logged_in: AsyncClient, script: Any, tmp_path: Path
) -> None:
    source = await make_source(logged_in, tmp_path)
    graph = approval_flow(source["id"])
    graph["nodes"].insert(
        2,
        node(
            "llm_1",
            "llm",
            {"prompt": [{"role": "user", "text": "{{ run_sql.sql_executed }}"}]},
            300,
        ),
    )
    graph["nodes"][-1]["config"]["outputs"].append({"name": "said", "var": "llm_1.text"})
    graph["edges"] = chain("start", "run_sql", "llm_1", "end_1")
    workflow_id = await save_flow(logged_in, graph)
    waiting = await logged_in.post(
        f"/api/console/workflows/{workflow_id}/draft/runs",
        json={"inputs": {}, "response_mode": "blocking"},
    )
    run = waiting.json()
    assert run["status"] == "waiting_input" and run["limits"]["tier"] == "free"
    async with app.state.db.write() as session:
        row = await session.get(SqlSource, uuid.UUID(source["id"]))
        assert row is not None
        row.data_policy = {**(row.data_policy or {}), "key_tier": "paid"}
    script.add(said("2명"))
    form_id = run["pending_interaction"]["form"]["form_id"]

    resumed = await logged_in.post(
        f"/api/console/wf-runs/{run['id']}/input",
        json={"kind": "form", "values": {"decision": "approve"}, "response_mode": "blocking"},
    )

    assert resumed.status_code == 200, resumed.text
    assert form_id
    detail = (await logged_in.get(f"/api/console/wf-runs/{run['id']}")).json()
    assert detail["status"] == "succeeded", detail["error"]
    # 재개할 때 지금 등급으로 올린다. 승인 뒤의 모델 호출은 유료 키로 간다.
    assert detail["limits"]["tier"] == "paid"
    assert app.state.llm.tiers == ["paid"]
