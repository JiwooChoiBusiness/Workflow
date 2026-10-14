"""실행 순서·병렬 가지·end 합치기·이벤트 모양(2단계 설계 3.4, 3.9, M11)."""

import asyncio
from collections.abc import Callable
from typing import Any

from app.workflow.engine import EngineLimits, WfEvent

Node = Callable[..., dict[str, Any]]


async def test_linear_fixture_runs_in_order_and_returns_end_outputs(kit: Any) -> None:
    result = await kit.run(kit.load("linear"), inputs={"name": "엘론"})

    assert result.types() == [
        "workflow.started",
        "node.started",
        "node.completed",
        "node.started",
        "node.completed",
        "node.started",
        "node.completed",
        "workflow.completed",
    ]
    assert result.started() == ["start", "template_1", "end_1"]
    assert result.outcome.status == "succeeded"
    assert result.outcome.outputs == {"greeting": "안녕하세요, 엘론님"}
    completed = result.of("workflow.completed")[0]
    assert completed["status"] == "succeeded"
    assert completed["outputs"] == {"greeting": "안녕하세요, 엘론님"}
    assert completed["node_runs"] == 3
    started = result.of("workflow.started")[0]
    assert started["resumed"] is False
    assert started["node_count"] == 3
    assert started["inputs_preview"] == {"name": "엘론"}


async def test_node_runs_carry_index_attempt_inputs_and_outputs(kit: Any) -> None:
    result = await kit.run(kit.load("linear"), inputs={"name": "엘론"})

    records = [event.node_run for event in result.events if event.type == "node.completed"]
    assert [record.index for record in records] == [0, 1, 2]
    assert all(record.attempt == 1 and record.status == "succeeded" for record in records)
    template = records[1]
    assert template.inputs == {"name": "엘론"}
    assert template.outputs == {"output": "안녕하세요, 엘론님"}
    assert template.ended_at is not None and template.duration_ms is not None
    # end 노드 런에는 런 출력에 합친 값이 남는다.
    assert records[2].outputs == {"greeting": "안녕하세요, 엘론님"}
    started_ids = [data["node_run_id"] for data in result.of("node.started")]
    assert started_ids == [record.node_run_id for record in records]


async def test_parallel_fixture_merges_end_outputs_and_joins_once(kit: Any) -> None:
    result = await kit.run(kit.load("parallel"), inputs={"topic": "비"})

    assert result.outcome.status == "succeeded"
    assert result.started().count("variable_aggregator_1") == 1
    outputs = result.outcome.outputs
    assert outputs["a"] == "A:비" and outputs["b"] == "B:비"
    # 합류 노드는 도착 순서가 아니라 vars 순서상 첫 값을 고른다.
    assert outputs["joined"] == "B:비"
    # end는 런을 멈추지 않고, 같은 이름은 나중에 끝난 end의 값이 이긴다.
    ends = [node_id for node_id, _ in result.completed_order() if node_id.startswith("end_")]
    assert ends == ["end_a", "end_b", "end_join"]
    assert outputs["result"] == "B:비"


async def test_later_end_wins_by_completion_order_not_graph_order(
    kit: Any, make_node: Node
) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("gate_a", "test_gate", {"value": "from a"}),
            make_node("gate_b", "test_gate", {"value": "from b"}),
            make_node("end_a", "end", {"outputs": [{"name": "result", "var": "gate_a.value"}]}),
            make_node("end_b", "end", {"outputs": [{"name": "result", "var": "gate_b.value"}]}),
        ],
        ["start>gate_a", "start>gate_b", "gate_a>end_a", "gate_b>end_b"],
    )

    async def release(event: WfEvent) -> None:
        if event.type == "node.started" and event.data["node_id"] == "gate_a":
            # 두 문이 다 열리기를 기다리던 중 b를 먼저 끝낸다.
            kit.gate.open("gate_b")
        if event.type == "node.completed" and event.data["node_id"] == "end_b":
            kit.gate.open("gate_a")

    result = await kit.run(graph, on_event=release)

    ends = [node_id for node_id, _ in result.completed_order() if node_id.startswith("end_")]
    assert ends == ["end_b", "end_a"]
    assert result.outcome.outputs == {"result": "from a"}


async def test_parallel_nodes_are_limited_by_the_semaphore(kit: Any, make_node: Node) -> None:
    gates = [make_node(f"gate_{i}", "test_gate", {"gate": "all", "value": i}) for i in range(4)]
    graph = kit.graph(
        [
            make_node("start", "start"),
            *gates,
            make_node("end_1", "end", {"outputs": [{"name": "v", "var": "gate_3.value"}]}),
        ],
        [*(f"start>gate_{i}" for i in range(4)), *(f"gate_{i}>end_1" for i in range(4))],
    )
    observed: list[int] = []
    openers: list[asyncio.Task[None]] = []

    async def open_when_full() -> None:
        while kit.gate.running < 2:
            await asyncio.sleep(0.005)
        # 상한이 없다면 이 사이에 세 번째·네 번째 노드도 들어온다.
        await asyncio.sleep(0.05)
        observed.append(kit.gate.running)
        kit.gate.open("all")

    def watch(event: WfEvent) -> None:
        if event.type == "node.started" and event.data["node_id"] == "gate_0":
            openers.append(asyncio.create_task(open_when_full()))

    result = await kit.run(graph, limits=EngineLimits(max_parallel_nodes=2), on_event=watch)

    assert result.outcome.status == "succeeded"
    assert observed == [2]
    assert kit.gate.peak == 2
    assert result.outcome.outputs == {"v": 3}
    assert result.started().count("end_1") == 1


async def test_start_input_errors_fail_the_run(kit: Any) -> None:
    result = await kit.run(kit.load("linear"), inputs={})

    assert result.outcome.status == "failed"
    assert result.outcome.error is not None
    assert result.outcome.error.code == "invalid_inputs"
    assert result.outcome.error.node_id == "start"
    assert result.types()[-2:] == ["workflow.error", "workflow.completed"]
    assert result.statuses() == {"start": "failed"}
    # 실패 런은 체크포인트를 남긴다(3.7).
    assert len(result.ports.checkpoints) == 1


async def test_invalid_graph_is_refused_before_any_node_runs(kit: Any) -> None:
    graph = kit.load("linear")
    graph.edges.pop()

    result = await kit.run(graph, inputs={"name": "x"})

    assert result.outcome.status == "failed"
    assert result.outcome.error is not None
    assert result.outcome.error.code == "graph_invalid"
    assert result.started() == []
    assert result.ports.checkpoints == []


async def test_usage_is_summed_and_token_budget_stops_the_run(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("probe_1", "test_flaky", {"tokens": 60, "value": 1}),
            make_node("probe_2", "test_flaky", {"tokens": 60, "value": 2}),
            make_node("end_1", "end", {"outputs": [{"name": "v", "var": "probe_2.value"}]}),
        ],
        ["start>probe_1", "probe_1>probe_2", "probe_2>end_1"],
    )

    ok = await kit.run(graph)
    assert ok.outcome.usage.input_tokens == 120
    assert ok.outcome.usage.output_tokens == 2
    assert ok.of("workflow.completed")[0]["usage_total"]["input_tokens"] == 120

    stopped = await kit.run(graph, limits=EngineLimits(max_total_tokens=100))
    assert stopped.outcome.status == "failed"
    assert stopped.outcome.error is not None
    assert stopped.outcome.error.code == "token_budget"
    assert "end_1" not in stopped.started()


async def test_variable_pool_limit_fails_the_run(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("probe_1", "test_flaky", {"value": "x" * 2000}),
            make_node("end_1", "end", {"outputs": [{"name": "v", "var": "probe_1.value"}]}),
        ],
        ["start>probe_1", "probe_1>end_1"],
    )

    result = await kit.run(graph, limits=EngineLimits(pool_max_bytes=1000))

    assert result.outcome.status == "failed"
    assert result.outcome.error is not None
    assert result.outcome.error.code == "variable_pool_limit"
    assert result.outcome.error.node_id == "probe_1"


async def test_secret_env_values_are_masked_in_events_records_and_outputs(
    kit: Any, make_node: Node
) -> None:
    hidden = "sk-very-hidden-value"
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            make_node(
                "template_1",
                "template",
                {"template": "토큰은 {{ key }}", "vars": [{"name": "key", "var": "env.API_KEY"}]},
            ),
            make_node("end_1", "end", {"outputs": [{"name": "text", "var": "template_1.output"}]}),
        ],
        ["start>template_1", "template_1>end_1"],
    )

    result = await kit.run(
        graph, inputs={"q": hidden}, env={"API_KEY": hidden}, secret_env=["API_KEY"]
    )

    assert result.outcome.status == "succeeded"
    flat = repr([event.data for event in result.events]) + repr(
        [event.node_run for event in result.events]
    )
    assert hidden not in flat
    assert result.outcome.outputs == {"text": "토큰은 ***"}
    unmasked = await kit.run(
        graph, env={"API_KEY": hidden}, secret_env=["API_KEY"], mask_outputs=False
    )
    assert unmasked.outcome.outputs == {"text": f"토큰은 {hidden}"}
    # 출력 마스킹을 꺼도 노드 런 기록과 이벤트 미리보기는 가린다.
    assert hidden not in repr([event.node_run for event in unmasked.events])
