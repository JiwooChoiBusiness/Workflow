"""iteration 노드와 반복 프레임(2단계 설계 2.3, 3.5, 3.7, 완료 기준 A3).

순차·병렬 반복의 출력 순서, error_mode 세 가지, 상한, 취소·시간 초과, 반복 안 입력 대기와
@index 체크포인트를 본다. 반복할 배열은 상류의 test_flaky 노드가 config.value로 낸다.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.workflow.engine import EngineLimits
from app.workflow.graph import validate
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    VarRef,
)

Node = Callable[..., dict[str, Any]]


class ItemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    var: str = "iteration_start_1.item"
    # 이 값의 원소를 받으면 실패한다.
    fail_on: list[Any] = []
    # 참이면 release()로 풀어 줄 때까지 기다린다.
    hold: bool = False


class ItemRunner(NodeRunner[ItemConfig]):
    """반복 원소를 받아 "item-<원소>"를 낸다. 동시에 몇 개가 돌았는지와 시작 순서를 센다."""

    type = "test_item"
    Config = ItemConfig
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (OutputSpec("value", "string"),)

    def __init__(self) -> None:
        self.started: list[Any] = []
        self.cancelled: list[Any] = []
        self.gates: dict[Any, asyncio.Event] = {}
        self.running = 0
        self.peak = 0

    def gate(self, item: Any) -> asyncio.Event:
        return self.gates.setdefault(item, asyncio.Event())

    def release(self, *items: Any) -> None:
        for item in items:
            self.gate(item).set()

    def var_refs(self, config: ItemConfig) -> list[VarRef]:
        return [VarRef("var", config.var)]

    async def run(
        self, ctx: NodeContext, config: ItemConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        item = ctx.resolve(config.var)
        self.started.append(item)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if config.hold:
                await self.gate(item).wait()
        except asyncio.CancelledError:
            self.cancelled.append(item)
            raise
        finally:
            self.running -= 1
        if item in config.fail_on:
            raise NodeFailure("bad_item", f"{item}은(는) 처리할 수 없다")
        return NodeResult(status="succeeded", outputs={"value": f"item-{item}"})


async def until(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not check():
            await asyncio.sleep(0.005)


def iteration_graph(
    kit: Any,
    make_node: Node,
    values: Any,
    inner: dict[str, Any],
    *,
    output: str = "test_1.value",
    **config: Any,
) -> Any:
    """start → list_1(values) → iteration_1[iteration_start_1 → inner] → end_1."""
    return kit.graph(
        [
            make_node("start", "start"),
            make_node("list_1", "test_flaky", {"value": values}),
            make_node(
                "iteration_1",
                "iteration",
                {"input_var": "list_1.value", "output_var": output, **config},
            ),
            make_node("iteration_start_1", "iteration_start", parent_id="iteration_1"),
            {**inner, "parent_id": "iteration_1"},
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "out", "var": "iteration_1.output"},
                        {"name": "count", "var": "iteration_1.count"},
                        {"name": "failed", "var": "iteration_1.failed_count"},
                    ]
                },
            ),
        ],
        [
            "start>list_1",
            "list_1>iteration_1",
            "iteration_1>end_1",
            f"iteration_start_1>{inner['id']}",
        ],
    )


@pytest.fixture
def items(kit: Any) -> ItemRunner:
    runner = ItemRunner()
    kit.registry.register(runner)
    return runner


def completed_of(run: Any, node_id: str) -> list[Any]:
    return [
        event.node_run
        for event in run.events
        if event.type == "node.completed" and event.data["node_id"] == node_id
    ]


async def test_ten_items_run_in_order_with_one_node_run_each(kit: Any, make_node: Node) -> None:
    letters = [chr(ord("a") + index) for index in range(10)]
    template = make_node(
        "template_1",
        "template",
        {
            "template": "{{ i }}:{{ x }}/{{ base }}",
            "vars": [
                {"name": "x", "var": "iteration_start_1.item"},
                {"name": "i", "var": "iteration_start_1.index"},
                # 반복 안에서도 바깥 상류(start)를 읽는다.
                {"name": "base", "var": "start.base"},
            ],
        },
    )
    graph = iteration_graph(kit, make_node, letters, template, output="template_1.output")
    raw = graph.model_dump()
    raw["nodes"][0]["config"] = {"fields": [{"name": "base", "type": "string"}]}
    graph = type(graph).model_validate(raw)

    run = await kit.run(graph, inputs={"base": "B"})

    assert run.outcome.status == "succeeded", run.outcome.error
    assert run.outcome.outputs == {
        "out": [f"{index}:{letter}/B" for index, letter in enumerate(letters)],
        "count": 10,
        "failed": 0,
    }
    inner = [data for data in run.of("node.started") if data["node_id"] == "template_1"]
    assert [(data["iteration_id"], data["iteration_index"]) for data in inner] == [
        ("iteration_1", index) for index in range(10)
    ]
    # iteration_start는 실행하지 않고 값만 넣는다.
    assert "iteration_start_1" not in run.started()
    assert run.of("iteration.started") == [{"node_id": "iteration_1", "total": 10}]
    assert [data["index"] for data in run.of("iteration.next")] == list(range(10))
    [completed] = run.of("iteration.completed")
    assert (completed["count"], completed["failed_count"]) == (10, 0)
    records = completed_of(run, "template_1")
    assert [(r.iteration_node_id, r.iteration_index) for r in records] == [
        ("iteration_1", index) for index in range(10)
    ]
    # 루트 4(start, list_1, iteration_1, end_1) + 하위 10.
    assert run.outcome.node_runs == 14


async def test_parallel_two_keeps_index_order_and_never_runs_more_than_two(
    kit: Any, make_node: Node, items: ItemRunner
) -> None:
    values = [str(index) for index in range(6)]
    graph = iteration_graph(
        kit,
        make_node,
        values,
        make_node("test_1", "test_item", {"hold": True}),
        parallel=True,
        max_parallel=2,
    )
    run_task = asyncio.create_task(kit.run(graph))

    # 뒤 반복이 먼저 끝나도 결과는 index 순이고, 하나가 끝나야 다음 반복이 시작한다.
    await until(lambda: items.started == ["0", "1"])
    items.release("1")
    await until(lambda: items.started == ["0", "1", "2"])
    items.release("2")
    await until(lambda: items.started == ["0", "1", "2", "3"])
    items.release(*values)
    run = await asyncio.wait_for(run_task, 5)

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs["out"] == [f"item-{value}" for value in values]
    assert items.peak == 2
    # 반복 안 노드 런은 끝난 순서대로 기록되지만 출력은 index 순이다.
    finished = [r.iteration_index for r in completed_of(run, "test_1")]
    assert finished[:2] == [1, 2]


@pytest.mark.parametrize(
    ("mode", "out"),
    [
        ("continue", ["item-1", None, "item-3", None]),
        ("remove", ["item-1", "item-3"]),
    ],
)
async def test_continue_and_remove_keep_going_after_failures(
    kit: Any, make_node: Node, items: ItemRunner, mode: str, out: list[Any]
) -> None:
    graph = iteration_graph(
        kit,
        make_node,
        ["1", "2", "3", "4"],
        make_node("test_1", "test_item", {"fail_on": ["2", "4"]}),
        error_mode=mode,
    )

    run = await kit.run(graph)

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"out": out, "count": 4, "failed": 2}
    statuses = [record.status for record in completed_of(run, "test_1")]
    assert statuses == ["succeeded", "failed", "succeeded", "failed"]
    [iteration] = completed_of(run, "iteration_1")
    assert iteration.process["failed_indices"] == [1, 3]
    assert iteration.process["error_mode"] == mode


async def test_terminate_stops_at_the_first_failure_and_fails_the_run(
    kit: Any, make_node: Node, items: ItemRunner
) -> None:
    graph = iteration_graph(
        kit,
        make_node,
        ["1", "2", "3", "4"],
        make_node("test_1", "test_item", {"fail_on": ["2"]}),
    )

    run = await kit.run(graph)

    assert run.outcome.status == "failed"
    error = run.outcome.error
    assert error is not None and error.node_id == "iteration_1" and error.code == "bad_item"
    assert error.message.startswith("1번째 반복의 test_1:")
    # 순차라 실패한 뒤의 반복은 시작하지 않았다.
    assert items.started == ["1", "2"]
    [completed] = run.of("iteration.completed")
    assert completed["failed_count"] == 1


async def test_terminate_in_parallel_cancels_the_other_running_frame(
    kit: Any, make_node: Node, items: ItemRunner
) -> None:
    graph = iteration_graph(
        kit,
        make_node,
        ["bad", "slow", "x"],
        make_node("test_1", "test_item", {"fail_on": ["bad"], "hold": True}),
        parallel=True,
        max_parallel=2,
    )
    run_task = asyncio.create_task(kit.run(graph))
    await until(lambda: items.started == ["bad", "slow"])
    items.release("bad")

    run = await asyncio.wait_for(run_task, 5)

    assert run.outcome.status == "failed"
    assert items.cancelled == ["slow"]
    assert "x" not in items.started
    assert sorted(record.status for record in completed_of(run, "test_1")) == [
        "cancelled",
        "failed",
    ]


def test_iteration_accepts_only_the_fail_policy(kit: Any, make_node: Node) -> None:
    graph = iteration_graph(
        kit,
        make_node,
        ["1"],
        make_node("test_1", "template", {"template": "x"}),
        output="test_1.output",
    )
    raw = graph.model_dump()
    raw["nodes"][2]["error"] = {"strategy": "default"}

    issues = validate(type(graph).model_validate(raw), "workflow", registry=kit.registry)

    # 반복 하나의 실패는 error_mode가 다룬다. 노드 오류 정책은 제어 노드처럼 fail뿐이다.
    assert [issue.code for issue in issues] == ["unsupported_error_strategy"]


def test_validation_of_parallel_settings(kit: Any, make_node: Node) -> None:
    graph = iteration_graph(
        kit,
        make_node,
        ["1"],
        make_node("test_1", "template", {"template": "x"}),
        output="test_1.output",
        parallel=True,
    )
    assert [
        (issue.code, issue.level) for issue in validate(graph, "workflow", registry=kit.registry)
    ] == [("parallel_one", "warning")]
    raw = graph.model_dump()
    raw["nodes"][2]["config"]["max_parallel"] = 5
    issues = validate(type(graph).model_validate(raw), "workflow", registry=kit.registry)
    assert [(issue.code, issue.field) for issue in issues] == [("invalid_config", "max_parallel")]


@pytest.mark.parametrize(
    ("values", "out"),
    [
        ([], []),
        # 건너뛴 상류처럼 null이면 반복 없이 끝난다.
        (None, []),
        # 배열이 아닌 값은 1원소 배열로 돈다(3.2).
        ("하나", ["item-하나"]),
    ],
)
async def test_empty_null_and_single_value_inputs(
    kit: Any, make_node: Node, items: ItemRunner, values: Any, out: list[str]
) -> None:
    graph = iteration_graph(kit, make_node, values, make_node("test_1", "test_item"))

    run = await kit.run(graph)

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"out": out, "count": len(out), "failed": 0}


async def test_more_than_a_thousand_items_is_a_node_error(
    kit: Any, make_node: Node, items: ItemRunner
) -> None:
    values = [str(index) for index in range(1001)]
    graph = iteration_graph(kit, make_node, values, make_node("test_1", "test_item"))

    run = await kit.run(graph)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "iteration_limit"
    assert items.started == []


async def test_waiting_inside_a_sequential_iteration_resumes_frame_by_frame(
    kit: Any, make_node: Node
) -> None:
    graph = iteration_graph(
        kit,
        make_node,
        ["a", "b"],
        make_node("wait_1", "test_wait", {"value": "티켓"}),
        output="wait_1.value",
    )

    first = await kit.run(graph)

    assert first.outcome.status == "waiting_input"
    pending = first.outcome.pending
    assert pending is not None
    assert (pending.node_id, pending.iteration_index) == ("wait_1", 0)
    waiting = first.of("workflow.waiting_input")[0]
    assert (waiting["node_id"], waiting["iteration_index"]) == ("wait_1", 0)
    checkpoint = first.outcome.checkpoint
    assert checkpoint is not None
    # 반복 안 상태는 @index 키다. 순차라 두 번째 반복은 시작하지 않았다.
    assert checkpoint.node_states["wait_1@0"] == "waiting_input"
    assert checkpoint.node_states["iteration_start_1@0"] == "succeeded"
    assert checkpoint.node_states["iteration_1"] == "waiting_input"
    assert not any(key.endswith("@1") for key in checkpoint.node_states)
    assert checkpoint.node_state["iteration_1"]["stalled"] == [0]
    assert checkpoint.node_state["wait_1@0"] == {"ticket": "티켓"}
    assert checkpoint.frames == [{"iteration_id": "iteration_1", "index": 0}]
    assert checkpoint.pool["iteration_start_1@0.item"] == "a"
    [iteration_run] = completed_of(first, "iteration_1")
    assert iteration_run.status == "waiting_input"
    assert "iteration.completed" not in first.types()

    second = await kit.run(graph, resume=checkpoint)

    assert second.outcome.status == "waiting_input"
    assert second.of("workflow.resumed") == [{"node_id": "wait_1"}]
    pending = second.outcome.pending
    assert pending is not None and pending.iteration_index == 1
    again = second.of("node.started")[0]
    # iteration은 같은 노드 런으로 이어 가고 멈췄던 반복부터 다시 돈다.
    assert again["node_id"] == "iteration_1"
    assert again["node_run_id"] == iteration_run.node_run_id
    assert [data["index"] for data in second.of("iteration.next")] == [0, 1]
    checkpoint = second.outcome.checkpoint
    assert checkpoint is not None
    assert checkpoint.node_state["iteration_1"]["done"] == {"0": "succeeded"}

    third = await kit.run(graph, resume=checkpoint)

    assert third.outcome.status == "succeeded"
    assert third.outcome.outputs == {"out": ["티켓", "티켓"], "count": 2, "failed": 0}
    [completed] = third.of("iteration.completed")
    assert completed["count"] == 2
    assert kit.wait.states == [None, {"ticket": "티켓"}, None, {"ticket": "티켓"}]
    # 같은 노드 런 하나가 세 구간에 걸쳐 끝났다.
    [final] = completed_of(third, "iteration_1")
    assert final.node_run_id == iteration_run.node_run_id and final.status == "succeeded"


async def test_cancel_during_iteration_marks_the_iteration_cancelled(
    kit: Any, make_node: Node, items: ItemRunner
) -> None:
    graph = iteration_graph(
        kit, make_node, ["1", "2"], make_node("test_1", "test_item", {"hold": True})
    )
    context = kit.context(graph)
    run = kit.engine.run(context)
    seen: list[Any] = []

    async def consume() -> None:
        async for event in run:
            seen.append(event)
            if event.type == "node.started" and event.data["node_id"] == "test_1":
                context.cancel.set("user")

    await asyncio.wait_for(consume(), 5)

    assert run.outcome.status == "cancelled"
    statuses = {
        event.data["node_id"]: event.data["status"]
        for event in seen
        if event.type == "node.completed"
    }
    assert statuses["test_1"] == "cancelled" and statuses["iteration_1"] == "cancelled"
    assert items.cancelled == ["1"]


async def test_run_timeout_inside_an_iteration_fails_the_run(
    kit: Any, make_node: Node, items: ItemRunner
) -> None:
    graph = iteration_graph(kit, make_node, ["1"], make_node("test_1", "test_item", {"hold": True}))

    run = await asyncio.wait_for(kit.run(graph, limits=EngineLimits(max_run_seconds=0.2)), 5)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "timeout"
    assert items.cancelled == ["1"]
    assert completed_of(run, "iteration_1")[0].status == "cancelled"
