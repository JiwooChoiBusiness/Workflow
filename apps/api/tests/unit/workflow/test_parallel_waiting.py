"""병렬로 멈춘 agent 노드의 순차 처리(2단계 설계 3.7의 2번, 11.2).

wf_run.pending_interaction은 하나라서 둘이 같이 멈추면 노드 id·index가 앞선 것만 기록한다. 그것에
답해 재개하면 다른 하나가 다시 멈춰 런이 두 번째로 멈춘다. 병렬 가지와 병렬 iteration 둘 다 본다.
"""

from collections.abc import Callable
from typing import Any

Node = Callable[..., dict[str, Any]]

AGENT_ID = "0190c1b2-0000-7000-8000-0000000000aa"


def agent(make_node: Node, node_id: str, instruction: str, **fields: Any) -> dict[str, Any]:
    return make_node(node_id, "agent", {"agent_id": AGENT_ID, "instruction": instruction}, **fields)


def child_of(tasks: Any, instruction: str) -> Any:
    return next(
        child
        for child, spec in zip(tasks.started, tasks.specs, strict=True)
        if spec.instruction == instruction
    )


async def test_two_waiting_branches_pause_the_run_twice(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            agent(make_node, "agent_b", "b 일"),
            agent(make_node, "agent_a", "a 일"),
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "a", "var": "agent_a.text"},
                        {"name": "b", "var": "agent_b.text"},
                    ]
                },
            ),
        ],
        ["start>agent_b", "start>agent_a", "agent_a>end_1", "agent_b>end_1"],
    )
    tasks = kit.tasks()
    tasks.plan.extend([lambda fake, child, spec: fake.waiting(child)] * 2)
    ports = kit.ports(tasks=tasks)

    first = await kit.run(graph, ports=ports)

    assert first.outcome.status == "waiting_input"
    assert first.outcome.pending is not None and first.outcome.pending.node_id == "agent_a"
    child_a, child_b = child_of(tasks, "a 일"), child_of(tasks, "b 일")
    assert first.outcome.pending.child_run_id == child_a
    checkpoint = first.outcome.checkpoint
    assert checkpoint is not None and set(checkpoint.waiting) == {"agent_a", "agent_b"}

    tasks.settle(child_a, tasks.result(child_a, text="a 끝"))
    second = await kit.run(graph, ports=ports, resume=checkpoint)

    assert second.outcome.status == "waiting_input"
    assert second.outcome.pending is not None
    assert (second.outcome.pending.node_id, second.outcome.pending.child_run_id) == (
        "agent_b",
        child_b,
    )
    assert second.of("workflow.waiting_input")[0]["node_id"] == "agent_b"
    assert "end_1" not in second.started()

    tasks.settle(child_b, tasks.result(child_b, text="b 끝"))
    third = await kit.run(graph, ports=ports, resume=second.outcome.checkpoint)

    assert third.outcome.status == "succeeded"
    assert third.outcome.outputs == {"a": "a 끝", "b": "b 끝"}
    assert len(tasks.started) == 2


def iteration(kit: Any, make_node: Node, values: list[str], **config: Any) -> Any:
    return kit.graph(
        [
            make_node("start", "start"),
            make_node("list_1", "test_flaky", {"value": values}),
            make_node(
                "iteration_1",
                "iteration",
                {"input_var": "list_1.value", "output_var": "agent_1.text", **config},
            ),
            make_node("iteration_start_1", "iteration_start", parent_id="iteration_1"),
            agent(make_node, "agent_1", "{{ iteration_start_1.item }} 일", parent_id="iteration_1"),
            make_node("end_1", "end", {"outputs": [{"name": "out", "var": "iteration_1.output"}]}),
        ],
        ["start>list_1", "list_1>iteration_1", "iteration_1>end_1", "iteration_start_1>agent_1"],
    )


async def test_two_waiting_frames_of_a_parallel_iteration_pause_twice(
    kit: Any, make_node: Node
) -> None:
    graph = iteration(kit, make_node, ["x", "y"], parallel=True, max_parallel=2)
    tasks = kit.tasks()
    tasks.plan.extend([lambda fake, child, spec: fake.waiting(child)] * 2)
    ports = kit.ports(tasks=tasks)

    first = await kit.run(graph, ports=ports)

    assert first.outcome.status == "waiting_input"
    pending = first.outcome.pending
    assert pending is not None and (pending.node_id, pending.iteration_index) == ("agent_1", 0)
    child_x, child_y = child_of(tasks, "x 일"), child_of(tasks, "y 일")
    assert pending.child_run_id == child_x
    checkpoint = first.outcome.checkpoint
    assert checkpoint is not None
    assert checkpoint.node_state["iteration_1"]["stalled"] == [0, 1]
    assert set(checkpoint.waiting) == {"agent_1@0", "agent_1@1", "iteration_1"}
    assert checkpoint.node_state["agent_1@1"]["child_run_id"] == str(child_y)

    tasks.settle(child_x, tasks.result(child_x, text="x 끝"))
    second = await kit.run(graph, ports=ports, resume=checkpoint)

    assert second.outcome.status == "waiting_input"
    pending = second.outcome.pending
    assert pending is not None and (pending.iteration_index, pending.child_run_id) == (1, child_y)
    assert second.outcome.checkpoint is not None
    assert second.outcome.checkpoint.node_state["iteration_1"]["done"] == {"0": "succeeded"}

    tasks.settle(child_y, tasks.result(child_y, text="y 끝"))
    third = await kit.run(graph, ports=ports, resume=second.outcome.checkpoint)

    assert third.outcome.status == "succeeded"
    assert third.outcome.outputs == {"out": ["x 끝", "y 끝"]}
    assert len(tasks.started) == 2


def x_waits_y_fails(fake: Any, child: Any, spec: Any) -> Any:
    if spec.instruction == "x 일":
        return fake.waiting(child)
    return fake.failed(child, "timeout")


async def test_terminate_closes_a_waiting_frame_and_cancels_its_child(
    kit: Any, make_node: Node
) -> None:
    graph = iteration(kit, make_node, ["x", "y"], parallel=True, max_parallel=2)
    tasks = kit.tasks()
    tasks.plan.extend([x_waits_y_fails] * 2)

    run = await kit.run(graph, ports=kit.ports(tasks=tasks))

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "timeout"
    child_x = child_of(tasks, "x 일")
    # 멈춰 있던 반복은 이어 갈 일이 없으니 노드 런은 취소로, 자식도 취소로 끝낸다.
    assert tasks.cancelled == [child_x]
    records = [
        event.node_run
        for event in run.events
        if event.type == "node.completed" and event.data["node_id"] == "agent_1"
    ]
    last = {record.iteration_index: record.status for record in records if record}
    assert last == {0: "cancelled", 1: "failed"}


async def test_continue_mode_keeps_a_failed_frame_while_another_waits(
    kit: Any, make_node: Node
) -> None:
    graph = iteration(
        kit, make_node, ["x", "y"], parallel=True, max_parallel=2, error_mode="continue"
    )
    tasks = kit.tasks()
    tasks.plan.extend([x_waits_y_fails] * 2)
    ports = kit.ports(tasks=tasks)

    first = await kit.run(graph, ports=ports)

    assert first.outcome.status == "waiting_input"
    checkpoint = first.outcome.checkpoint
    assert checkpoint is not None
    assert checkpoint.node_state["iteration_1"]["done"] == {"1": "failed"}
    child_x = child_of(tasks, "x 일")
    tasks.settle(child_x, tasks.result(child_x, text="x 끝"))

    second = await kit.run(graph, ports=ports, resume=checkpoint)

    assert second.outcome.status == "succeeded"
    assert second.outcome.outputs == {"out": ["x 끝", None]}
    assert tasks.cancelled == []
