"""agent 노드(2단계 설계 2.3, 3.7, 3.8, 3.10, 오케스트레이션 9장).

가짜 과업 포트(FakeTasks)로 자식 런을 흉내 낸다. 자식에게 넘기는 spec, 결과 출력, 대기와 재개,
자식 실패의 매핑, 진행 이벤트, 런이 멈출 때 자식 정리, 사용량과 검증을 본다.
"""

import uuid
from collections.abc import Callable
from typing import Any

import pytest

from app.workflow.engine import EngineLimits
from app.workflow.graph import validate
from app.workflow.nodes import agent as agent_node
from app.workflow.ports import TaskPortError
from app.workflow.variables import FileRef

Node = Callable[..., dict[str, Any]]

AGENT_ID = "0190c1b2-0000-7000-8000-0000000000aa"
FILE = {
    "file_id": "0190c1b2-0000-7000-8000-000000000001",
    "filename": "보고서.pdf",
    "mime": "application/pdf",
    "size_bytes": 10,
    "storage_key": "wf-files/w/f",
    "source": "wf_file",
}
SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
OUTPUTS = ["text", "result", "files", "status", "usage", "child_run_id"]


def agent_graph(
    kit: Any, make_node: Node, config: dict[str, Any] | None = None, **node_fields: Any
) -> Any:
    """start(q, docs) → agent_1 → end_1(agent_1의 출력 전부)."""
    return kit.graph(
        [
            make_node(
                "start",
                "start",
                {
                    "fields": [
                        {"name": "q", "type": "string"},
                        {"name": "docs", "type": "file_list"},
                    ]
                },
            ),
            make_node(
                "agent_1",
                "agent",
                {"agent_id": AGENT_ID, "instruction": "{{ start.q }}를 정리한다", **(config or {})},
                **node_fields,
            ),
            make_node(
                "end_1",
                "end",
                {"outputs": [{"name": name, "var": f"agent_1.{name}"} for name in OUTPUTS]},
            ),
        ],
        ["start>agent_1", "agent_1>end_1"],
    )


def node_run(run: Any, node_id: str) -> Any:
    return next(
        event.node_run
        for event in reversed(run.events)
        if event.type == "node.completed" and event.data["node_id"] == node_id
    )


async def test_child_gets_the_rendered_instruction_inputs_files_and_correlation(
    kit: Any, make_node: Node
) -> None:
    tasks = kit.tasks()
    usage = {"input_tokens": 120, "output_tokens": 30}
    tasks.plan.append(
        lambda fake, child, spec: fake.result(
            child, text="요약했다", result={"summary": "짧게"}, usage=usage
        )
    )
    graph = agent_graph(
        kit,
        make_node,
        {
            "inputs": [
                {"name": "topic", "var": "start.q"},
                {"name": "doc", "var": "start.docs"},
            ],
            "files_var": "start.docs",
            "result": {"format": "json", "schema": SCHEMA},
        },
    )

    run = await kit.run(graph, inputs={"q": "보고서", "docs": [FILE]}, ports=kit.ports(tasks=tasks))

    assert run.outcome.status == "succeeded", run.outcome.error
    [spec] = tasks.specs
    [child] = tasks.started
    record = node_run(run, "agent_1")
    assert str(spec.agent_id) == AGENT_ID and spec.agent_version_id is None
    # 지시는 엔진이 렌더해 넘긴다. 과업 서비스는 다시 채우지 않는다.
    assert spec.instruction == "보고서를 정리한다"
    assert spec.inputs == {"topic": "보고서", "doc": [FILE]}
    assert spec.files == [FileRef.model_validate(FILE)]
    assert spec.output_schema == SCHEMA
    assert spec.approval_mode is None and spec.session_mode == "ephemeral"
    assert spec.correlation == {
        "node_run_id": str(record.node_run_id),
        "node_id": "agent_1",
        "iteration_index": None,
    }
    outputs = run.outcome.outputs
    assert outputs["text"] == "요약했다" and outputs["result"] == {"summary": "짧게"}
    assert outputs["status"] == "succeeded" and outputs["child_run_id"] == str(child)
    assert outputs["usage"]["input_tokens"] == 120 and outputs["files"] == []
    assert record.child_run_id == child
    assert record.process["child_session_id"] == str(tasks.sessions[child])
    assert record.inputs["instruction"] == "보고서를 정리한다"
    # 자식 사용량은 노드 런에만 남고 런 사용량에는 더하지 않는다(3.10).
    assert record.usage is not None and record.usage.input_tokens == 120
    assert run.outcome.usage.input_tokens == 0
    progress = run.of("node.progress")[0]
    assert progress["child_run_id"] == child
    assert progress["child_session_id"] == tasks.sessions[child]


@pytest.mark.parametrize("keep_files", [True, False])
async def test_result_files_come_from_the_child_unless_turned_off(
    kit: Any, make_node: Node, keep_files: bool
) -> None:
    tasks = kit.tasks()
    produced = {**FILE, "source": "session_file", "filename": "결과.md"}

    def finish(fake: Any, child: uuid.UUID, spec: Any) -> Any:
        fake.files[child] = [produced]
        return fake.result(
            child,
            text="끝",
            files=[
                {
                    "id": produced["file_id"],
                    "path": "outputs/결과.md",
                    "filename": "결과.md",
                    "mime": "text/markdown",
                    "size_bytes": 10,
                    "sha256": "0" * 64,
                    "downloadable": True,
                }
            ],
        )

    tasks.plan.append(finish)
    graph = agent_graph(kit, make_node, {"result": {"files": keep_files}})

    run = await kit.run(graph, inputs={"q": "x"}, ports=kit.ports(tasks=tasks))

    assert run.outcome.outputs["files"] == ([produced] if keep_files else [])


async def test_waiting_child_pauses_the_run_and_resume_reuses_it(kit: Any, make_node: Node) -> None:
    tasks = kit.tasks()
    tasks.plan.append(lambda fake, child, spec: fake.waiting(child))
    graph = agent_graph(kit, make_node)
    ports = kit.ports(tasks=tasks)

    first = await kit.run(graph, inputs={"q": "x"}, ports=ports)

    assert first.outcome.status == "waiting_input"
    [child] = tasks.started
    pending = first.outcome.pending
    assert pending is not None
    assert (pending.node_id, pending.child_run_id) == ("agent_1", child)
    assert pending.interaction.kind == "approval"
    checkpoint = first.outcome.checkpoint
    assert checkpoint is not None
    session = tasks.sessions[child]
    assert checkpoint.node_state["agent_1"] == {
        "child_run_id": str(child),
        "child_session_id": str(session),
    }
    assert checkpoint.waiting["agent_1"].child_session_id == session
    waiting_run = node_run(first, "agent_1")
    assert waiting_run.status == "waiting_input" and waiting_run.child_run_id == child

    tasks.settle(child, tasks.result(child, text="승인 뒤 끝냈다"))
    resumed = await kit.run(graph, inputs={"q": "x"}, ports=ports, resume=checkpoint)

    assert resumed.outcome.status == "succeeded"
    assert resumed.outcome.outputs["text"] == "승인 뒤 끝냈다"
    started = resumed.of("node.started")[0]
    assert started["node_run_id"] == waiting_run.node_run_id
    assert (started["child_run_id"], started["child_session_id"]) == (child, session)
    # 재개는 새 자식을 만들지 않는다.
    assert tasks.started == [child]
    assert node_run(resumed, "agent_1").inputs["instruction"] == "x를 정리한다"


@pytest.mark.parametrize(
    ("status", "code"),
    [("failed", "timeout"), ("cancelled", "input_timeout"), ("cancelled", "orphaned")],
)
async def test_child_failure_is_the_node_error(
    kit: Any, make_node: Node, status: str, code: str
) -> None:
    tasks = kit.tasks()
    tasks.plan.append(lambda fake, child, spec: fake.failed(child, code, status=status))

    run = await kit.run(
        agent_graph(kit, make_node), inputs={"q": "x"}, ports=kit.ports(tasks=tasks)
    )

    assert run.outcome.status == "failed"
    error = run.outcome.error
    assert error is not None and (error.code, error.node_id) == (code, "agent_1")


async def test_error_policy_default_keeps_the_run_going(kit: Any, make_node: Node) -> None:
    tasks = kit.tasks()
    tasks.plan.append(lambda fake, child, spec: fake.failed(child, "timeout"))
    graph = agent_graph(
        kit,
        make_node,
        error={"strategy": "default", "default_outputs": {"text": "대신 쓴 글"}},
    )

    run = await kit.run(graph, inputs={"q": "x"}, ports=kit.ports(tasks=tasks))

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs["text"] == "대신 쓴 글"
    assert node_run(run, "agent_1").status == "failed"


async def test_approval_limits_and_version_go_into_the_spec(kit: Any, make_node: Node) -> None:
    tasks = kit.tasks()
    version = "0190c1b2-0000-7000-8000-0000000000bb"
    graph = agent_graph(
        kit,
        make_node,
        {"approval_mode": "off", "limits": {"max_steps": 5}, "agent_version_id": version},
    )

    await kit.run(graph, inputs={"q": "x"}, ports=kit.ports(tasks=tasks))

    [spec] = tasks.specs
    assert spec.approval_mode == "off" and str(spec.agent_version_id) == version
    assert spec.limits is not None and spec.limits.max_steps == 5
    # 준 필드만 버전 한도를 덮어쓴다(merged_limits가 model_fields_set을 본다).
    assert spec.limits.model_fields_set == {"max_steps"}


async def test_progress_and_queue_position_while_the_child_runs(
    kit: Any, make_node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_node, "PROGRESS_SEC", 0.01)
    tasks = kit.tasks()

    def queued(fake: Any, child: uuid.UUID, spec: Any) -> None:
        fake.positions[child] = 2
        return None

    tasks.plan.append(queued)
    reports: list[str] = []

    def follow(event: Any) -> None:
        if event.type not in ("node.progress", "node.waiting"):
            return
        reports.append(event.type)
        [child] = tasks.started
        if event.type == "node.waiting":
            tasks.positions.pop(child)
        elif len(reports) >= 4 and tasks.results[child] is None:
            tasks.settle(child, tasks.result(child, text="끝"))

    run = await kit.run(
        agent_graph(kit, make_node),
        inputs={"q": "x"},
        ports=kit.ports(tasks=tasks),
        on_event=follow,
    )

    assert run.outcome.status == "succeeded"
    waiting = run.of("node.waiting")
    assert waiting == [
        {
            "reason": "queue",
            "position": 2,
            "node_id": "agent_1",
            "node_run_id": waiting[0]["node_run_id"],
        }
    ]
    progress = run.of("node.progress")
    assert len(progress) >= 3
    assert progress[0]["position"] == 2 and progress[-1]["position"] is None
    assert all(item["steps"] == 2 for item in progress)


@pytest.mark.parametrize(("reason", "cancelled"), [("user", True), ("shutdown", False)])
async def test_stopping_the_run_cancels_the_running_child_unless_shutting_down(
    kit: Any, make_node: Node, reason: str, cancelled: bool
) -> None:
    tasks = kit.tasks()
    tasks.plan.append(lambda fake, child, spec: None)
    context = kit.context(
        agent_graph(kit, make_node), inputs={"q": "x"}, ports=kit.ports(tasks=tasks)
    )
    run = kit.engine.run(context)
    async with run:
        async for event in run:
            if event.type == "node.progress":
                context.cancel.set(reason)

    assert run.outcome.status == ("cancelled" if cancelled else "failed")
    [child] = tasks.started
    # 종료 중에는 자식을 건드리지 않는다. 기동 복구가 고아로 닫는다.
    assert tasks.cancelled == ([child] if cancelled else [])


async def test_port_errors_and_missing_port_are_node_errors(kit: Any, make_node: Node) -> None:
    tasks = kit.tasks()

    async def refuse(spec: Any) -> uuid.UUID:
        raise TaskPortError("agent_not_published", "발행한 버전이 없다")

    tasks.start = refuse
    graph = agent_graph(kit, make_node)

    refused = await kit.run(graph, inputs={"q": "x"}, ports=kit.ports(tasks=tasks))
    missing = await kit.run(graph, inputs={"q": "x"})

    assert refused.outcome.error is not None
    assert refused.outcome.error.code == "agent_not_published"
    assert missing.outcome.error is not None
    assert missing.outcome.error.code == "tasks_unavailable"


async def test_files_var_must_be_files_and_has_a_limit(kit: Any, make_node: Node) -> None:
    tasks = kit.tasks()
    wrong = agent_graph(kit, make_node, {"files_var": "start.q"})
    many = agent_graph(kit, make_node, {"files_var": "start.docs"})

    bad = await kit.run(wrong, inputs={"q": "x"}, ports=kit.ports(tasks=tasks))
    over = await kit.run(many, inputs={"q": "x", "docs": [FILE] * 21}, ports=kit.ports(tasks=tasks))

    assert bad.outcome.error is not None and bad.outcome.error.code == "type_mismatch"
    assert over.outcome.error is not None and over.outcome.error.code == "file_limit"
    assert tasks.started == []


async def test_child_tokens_count_toward_the_run_budget(kit: Any, make_node: Node) -> None:
    tasks = kit.tasks()
    usage = {"input_tokens": 90, "output_tokens": 20}
    tasks.plan.append(lambda fake, child, spec: fake.result(child, text="끝", usage=usage))

    run = await kit.run(
        agent_graph(kit, make_node),
        inputs={"q": "x"},
        ports=kit.ports(tasks=tasks),
        limits=EngineLimits(max_total_tokens=100),
    )

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "token_budget"


def test_validation_of_the_agent_config(kit: Any, make_node: Node) -> None:
    def codes(config: dict[str, Any]) -> list[tuple[str, str, str | None]]:
        graph = agent_graph(kit, make_node, config)
        return [
            (issue.level, issue.code, issue.field)
            for issue in validate(graph, "workflow")
            if issue.node_id == "agent_1"
        ]

    assert codes({}) == []
    assert codes({"result": {"format": "json"}}) == [
        ("error", "output_schema_required", "result.schema")
    ]
    assert codes({"result": {"format": "json", "schema": {"type": "object"}}}) == [
        ("error", "output_schema_invalid", "result.schema")
    ]
    assert codes({"instruction": " "}) == [("error", "instruction_missing", "instruction")]
    assert codes({"session_mode": "per_conversation"}) == [
        ("warning", "session_mode_ignored", "session_mode")
    ]
    assert codes(
        {"inputs": [{"name": "a", "var": "start.q"}, {"name": "a", "var": "start.q"}]}
    ) == [("error", "duplicate_name", "inputs[1].name")]
    assert codes({"agent_id": "아님"})[0][1] == "invalid_config"
    # 상류가 아닌 변수를 넘기면 참조 오류다.
    assert codes({"inputs": [{"name": "a", "var": "end_1.x"}]})[0][1] == "unreachable_reference"
