"""취소·시간 초과·실패 전파·입력 대기와 체크포인트 재개(2단계 설계 3.4, 3.7)."""

import asyncio
from collections.abc import Callable
from typing import Any

from app.runner.supervisor import CancelSignal
from app.workflow.engine import EngineLimits, RunContext, WfEvent

Node = Callable[..., dict[str, Any]]


def gate_graph(kit: Any, make_node: Node) -> Any:
    return kit.graph(
        [
            make_node("start", "start"),
            make_node("gate_1", "test_gate", {"value": 1}),
            make_node("end_1", "end", {"outputs": [{"name": "v", "var": "gate_1.value"}]}),
        ],
        ["start>gate_1", "gate_1>end_1"],
    )


async def test_cancel_signal_cancels_the_running_node(kit: Any, make_node: Node) -> None:
    cancel = CancelSignal()

    def stop(event: WfEvent) -> None:
        if event.type == "node.started" and event.data["node_id"] == "gate_1":
            cancel.set("cancel")

    run = await kit.run(gate_graph(kit, make_node), cancel=cancel, on_event=stop)

    assert run.outcome.status == "cancelled"
    assert run.statuses() == {"start": "succeeded", "gate_1": "cancelled"}
    assert kit.gate.cancelled == ["gate_1"]
    assert "end_1" not in run.started()
    completed = run.of("workflow.completed")[0]
    assert completed["status"] == "cancelled" and completed["error"] is None
    assert "workflow.error" not in run.types()
    cancelled = next(e.node_run for e in run.events if e.data.get("status") == "cancelled")
    assert cancelled.status == "cancelled" and cancelled.ended_at is not None


async def test_cancel_before_start_runs_nothing(kit: Any) -> None:
    cancel = CancelSignal()
    cancel.set("cancel")

    run = await kit.run(kit.load("linear"), inputs={"name": "x"}, cancel=cancel)

    assert run.outcome.status == "cancelled"
    assert run.started() == []
    assert run.types() == ["workflow.started", "workflow.completed"]


async def test_shutdown_fails_the_run_without_a_checkpoint(kit: Any, make_node: Node) -> None:
    cancel = CancelSignal()

    def stop(event: WfEvent) -> None:
        if event.type == "node.started" and event.data["node_id"] == "gate_1":
            cancel.set("shutdown")

    run = await kit.run(gate_graph(kit, make_node), cancel=cancel, on_event=stop)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "shutdown"
    assert run.statuses()["gate_1"] == "cancelled"
    assert run.ports.checkpoints == []


async def test_run_time_limit_cancels_nodes_and_fails_with_timeout(
    kit: Any, make_node: Node
) -> None:
    run = await kit.run(gate_graph(kit, make_node), limits=EngineLimits(max_run_seconds=0.05))

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "timeout"
    assert run.statuses()["gate_1"] == "cancelled"
    assert kit.gate.cancelled == ["gate_1"]
    assert len(run.ports.checkpoints) == 1


async def test_node_time_limit_is_a_retryable_node_error(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node(
                "gate_1",
                "test_gate",
                {"timeout": 0.02},
                retry={"max_attempts": 1, "interval_ms": 0},
            ),
            make_node("end_1", "end", {"outputs": [{"name": "v", "var": "gate_1.value"}]}),
        ],
        ["start>gate_1", "gate_1>end_1"],
    )

    run = await kit.run(graph)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "timeout"
    assert run.started().count("gate_1") == 2
    assert run.of("node.retrying")[0]["reason"] == "timeout"
    assert kit.gate.cancelled == ["gate_1", "gate_1"]


async def test_interval_checkpoints_are_optional(kit: Any) -> None:
    graph = kit.load("linear")

    off = await kit.run(graph, inputs={"name": "x"})
    on = await kit.run(
        graph, inputs={"name": "x"}, limits=EngineLimits(checkpoint_interval_sec=1e-9)
    )

    assert off.ports.checkpoints == []
    assert on.outcome.status == "succeeded"
    assert len(on.ports.checkpoints) == 3
    assert on.ports.checkpoints[-1].node_states == {
        "start": "succeeded",
        "template_1": "succeeded",
        "end_1": "succeeded",
    }


async def test_a_failing_branch_cancels_the_parallel_branch(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("gate_1", "test_gate", {"value": 1}),
            make_node("probe_1", "test_flaky", {"failures": 1, "retryable": False}),
            make_node("end_1", "end", {"outputs": [{"name": "v", "var": "gate_1.value"}]}),
        ],
        ["start>gate_1", "start>probe_1", "gate_1>end_1", "probe_1>end_1"],
    )

    run = await kit.run(graph)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.node_id == "probe_1"
    assert run.statuses()["gate_1"] == "cancelled"
    assert kit.gate.cancelled == ["gate_1"]
    assert run.types()[-2:] == ["workflow.error", "workflow.completed"]


async def test_closing_the_stream_cancels_running_nodes(kit: Any, make_node: Node) -> None:
    context: RunContext = kit.context(gate_graph(kit, make_node))
    run = kit.engine.run(context)
    seen: list[str] = []

    async def consume() -> None:
        async for event in run:
            seen.append(event.type)

    consumer = asyncio.create_task(consume())
    while not kit.gate.entered:
        await asyncio.sleep(0.005)
    # 소비자가 사라지면(실행기 태스크 취소) 엔진도 실행 중인 노드를 취소하고 끝난다.
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)
    await asyncio.wait_for(run.aclose(), timeout=2)

    assert kit.gate.cancelled == ["gate_1"]
    assert kit.gate.running == 0
    assert "workflow.completed" not in seen


async def test_breaking_out_right_after_a_start_event_leaves_nothing_running(
    kit: Any, make_node: Node
) -> None:
    run = kit.engine.run(kit.context(gate_graph(kit, make_node)))
    async for event in run:
        if event.type == "node.started" and event.data["node_id"] == "gate_1":
            break
    await asyncio.wait_for(run.aclose(), timeout=2)

    # node.started를 받은 소비자가 다음을 달라고 하지 않았으므로 노드는 run()에 들어가지 못했다.
    assert kit.gate.entered == []
    assert kit.gate.running == 0


def wait_graph(kit: Any, make_node: Node) -> Any:
    return kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            make_node("wait_1", "test_wait", {"value": "티켓-7"}),
            make_node("template_side", "template", {"template": "옆 가지"}),
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "v", "var": "wait_1.value"},
                        {"name": "side", "var": "template_side.output"},
                    ]
                },
            ),
        ],
        ["start>wait_1", "start>template_side", "wait_1>end_1", "template_side>end_1"],
    )


async def test_waiting_input_checkpoints_and_resume_finishes_the_same_node_run(
    kit: Any, make_node: Node
) -> None:
    graph = wait_graph(kit, make_node)

    first = await kit.run(graph, inputs={"q": "질문"})

    assert first.outcome.status == "waiting_input"
    # 병렬 가지는 대기와 관계없이 끝까지 돈다.
    assert first.statuses()["template_side"] == "succeeded"
    assert first.statuses()["wait_1"] == "waiting_input"
    assert "end_1" not in first.started()
    assert first.types()[-2:] == ["workflow.waiting_input", "workflow.completed"]
    waiting = first.of("workflow.waiting_input")[0]
    assert waiting["node_id"] == "wait_1"
    assert waiting["interaction"]["interaction_id"] == "int_wait_1"
    pending = first.outcome.pending
    assert pending is not None and pending.node_id == "wait_1"
    assert pending.child_run_id == waiting["child_run_id"]
    [checkpoint] = first.ports.checkpoints
    assert first.outcome.checkpoint == checkpoint
    assert checkpoint.node_states["wait_1"] == "waiting_input"
    assert checkpoint.node_state["wait_1"] == {"ticket": "티켓-7"}
    assert checkpoint.pool["template_side.output"] == "옆 가지"
    wait_run = next(
        e.node_run
        for e in first.events
        if e.type == "node.completed" and e.data["node_id"] == "wait_1"
    )

    resumed = await kit.run(graph, inputs={"q": "질문"}, resume=checkpoint)

    assert resumed.outcome.status == "succeeded"
    assert resumed.outcome.outputs == {"v": "티켓-7", "side": "옆 가지"}
    assert resumed.types()[:3] == ["workflow.started", "workflow.resumed", "node.started"]
    assert resumed.of("workflow.started")[0]["resumed"] is True
    assert resumed.of("workflow.resumed")[0] == {"node_id": "wait_1"}
    # 끝난 노드는 다시 돌지 않고, 대기하던 노드는 같은 노드 런으로 이어 간다.
    assert resumed.started() == ["wait_1", "end_1"]
    again = resumed.of("node.started")[0]
    assert again["node_run_id"] == wait_run.node_run_id
    assert again["index"] == wait_run.index
    assert again["child_run_id"] == pending.child_run_id
    assert kit.wait.states == [None, {"ticket": "티켓-7"}]
    assert resumed.outcome.node_runs == first.outcome.node_runs + 1


async def test_parallel_waits_record_one_pending_by_node_id(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("wait_b", "test_wait", {"value": "b"}),
            make_node("wait_a", "test_wait", {"value": "a"}),
            make_node("end_1", "end", {"outputs": [{"name": "a", "var": "wait_a.value"}]}),
        ],
        ["start>wait_b", "start>wait_a", "wait_a>end_1", "wait_b>end_1"],
    )

    run = await kit.run(graph)

    assert run.outcome.status == "waiting_input"
    assert run.outcome.pending is not None and run.outcome.pending.node_id == "wait_a"
    assert run.of("workflow.waiting_input")[0]["node_id"] == "wait_a"
    checkpoint = run.outcome.checkpoint
    assert checkpoint is not None
    assert set(checkpoint.waiting) == {"wait_a", "wait_b"}


async def test_failure_checkpoint_masks_secrets(kit: Any, make_node: Node) -> None:
    hidden = "password-1234567"
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node(
                "template_1",
                "template",
                {"template": "{{ s }}", "vars": [{"name": "s", "var": "env.SECRET"}]},
            ),
            make_node("probe_1", "test_flaky", {"failures": 1, "retryable": False}),
            make_node("end_1", "end", {"outputs": [{"name": "v", "var": "probe_1.value"}]}),
        ],
        ["start>template_1", "template_1>probe_1", "probe_1>end_1"],
    )

    run = await kit.run(graph, env={"SECRET": hidden}, secret_env=["SECRET"])

    assert run.outcome.status == "failed"
    [checkpoint] = run.ports.checkpoints
    assert checkpoint.pool["template_1.output"] == "***"
    assert "env.SECRET" not in checkpoint.pool
    assert hidden not in checkpoint.model_dump_json()
