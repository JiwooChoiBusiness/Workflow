"""agent 노드 재개와 자식 런의 경합(2단계 설계 3.7의 3번, 11.3 test_resume_child).

워크플로우 런이 다시 큐에 들어가 돌기 전에 자식이 이미 끝났거나, 또 멈췄거나, 아직 돌고 있을 때
재개한 노드가 새 자식을 만들지 않고 그 자식의 지금 상태에서 이어 가는지 본다.
"""

import asyncio
from collections.abc import Callable
from typing import Any

Node = Callable[..., dict[str, Any]]

AGENT_ID = "0190c1b2-0000-7000-8000-0000000000aa"


def graph(kit: Any, make_node: Node) -> Any:
    return kit.graph(
        [
            make_node("start", "start"),
            make_node("agent_1", "agent", {"agent_id": AGENT_ID, "instruction": "일한다"}),
            make_node("end_1", "end", {"outputs": [{"name": "text", "var": "agent_1.text"}]}),
        ],
        ["start>agent_1", "agent_1>end_1"],
    )


async def paused(kit: Any, make_node: Node) -> tuple[Any, Any, Any]:
    tasks = kit.tasks()
    tasks.plan.append(lambda fake, child, spec: fake.waiting(child, "int_first"))
    ports = kit.ports(tasks=tasks)
    first = await kit.run(graph(kit, make_node), ports=ports)
    assert first.outcome.status == "waiting_input"
    return tasks, ports, first.outcome.checkpoint


async def test_child_that_finished_before_the_resume_is_read_without_waiting(
    kit: Any, make_node: Node
) -> None:
    tasks, ports, checkpoint = await paused(kit, make_node)
    [child] = tasks.started
    # 승인 뒤 자식이 먼저 끝났고 워크플로우 런은 그다음에야 레인에서 꺼내졌다.
    tasks.settle(child, tasks.result(child, text="먼저 끝났다"))

    resumed = await asyncio.wait_for(
        kit.run(graph(kit, make_node), ports=ports, resume=checkpoint), 2
    )

    assert resumed.outcome.status == "succeeded"
    assert resumed.outcome.outputs == {"text": "먼저 끝났다"}
    assert tasks.started == [child]
    assert tasks.waits == [child, child]


async def test_child_that_expired_before_the_resume_fails_the_node(
    kit: Any, make_node: Node
) -> None:
    tasks, ports, checkpoint = await paused(kit, make_node)
    [child] = tasks.started
    tasks.settle(child, tasks.failed(child, "input_timeout", status="cancelled"))

    resumed = await kit.run(graph(kit, make_node), ports=ports, resume=checkpoint)

    assert resumed.outcome.status == "failed"
    error = resumed.outcome.error
    assert error is not None and (error.code, error.node_id) == ("input_timeout", "agent_1")


async def test_child_still_running_at_the_resume_is_waited_for(kit: Any, make_node: Node) -> None:
    tasks, ports, checkpoint = await paused(kit, make_node)
    [child] = tasks.started
    # 답을 받아 자식이 다시 큐에 들어갔고 아직 끝나지 않았다.
    tasks.settle(child, None)

    def finish_later(event: Any) -> None:
        if event.type == "node.progress":
            tasks.settle(child, tasks.result(child, text="나중에 끝났다"))

    resumed = await asyncio.wait_for(
        kit.run(graph(kit, make_node), ports=ports, resume=checkpoint, on_event=finish_later), 2
    )

    assert resumed.outcome.status == "succeeded"
    assert resumed.outcome.outputs == {"text": "나중에 끝났다"}
    assert tasks.started == [child]


async def test_child_that_paused_again_pauses_the_run_with_the_new_question(
    kit: Any, make_node: Node
) -> None:
    tasks, ports, checkpoint = await paused(kit, make_node)
    [child] = tasks.started
    tasks.settle(child, tasks.waiting(child, "int_second"))

    resumed = await kit.run(graph(kit, make_node), ports=ports, resume=checkpoint)

    assert resumed.outcome.status == "waiting_input"
    pending = resumed.outcome.pending
    assert pending is not None
    assert pending.child_run_id == child
    assert pending.interaction.interaction_id == "int_second"
    assert tasks.started == [child]
