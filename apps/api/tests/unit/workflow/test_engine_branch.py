"""분기·합류·skipped 전파·오류 정책·재시도(2단계 설계 3.4, 2.2, 완료 기준 A2)."""

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    NodeContext,
    NodeNotImplemented,
    NodeResult,
    NodeRunner,
    OutputSpec,
)

Node = Callable[..., dict[str, Any]]


class OpenConfig(BaseModel):
    model_config = ConfigDict(extra="allow")


class PendingNode(NodeRunner[OpenConfig]):
    """레지스트리에 자리만 있는 노드(1차 노드는 M14에서 모두 구현되어 시험용으로 둔다)."""

    type = "test_pending"
    Config = OpenConfig
    implemented = False
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (OutputSpec("text", "string"),)

    async def run(
        self, ctx: NodeContext, config: OpenConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        raise NodeNotImplemented(self.type, "M19")


@pytest.mark.parametrize(
    ("score", "ran", "skipped", "result"),
    [
        (90, "template_pass", ["template_fail", "end_fail"], "합격(90점)"),
        (40, "template_fail", ["template_pass", "end_pass"], "불합격(40점)"),
    ],
)
async def test_branch_fixture_runs_one_side_and_skips_the_other(
    kit: Any, score: int, ran: str, skipped: list[str], result: str
) -> None:
    run = await kit.run(kit.load("branch"), inputs={"score": score})

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"result": result}
    statuses = run.statuses()
    assert statuses[ran] == "succeeded"
    assert [statuses[node_id] for node_id in skipped] == ["skipped", "skipped"]
    # skipped는 시작 없이 바로 기록된다.
    assert not set(skipped) & set(run.started())
    branch = next(data for data in run.of("node.completed") if data["node_id"] == "if_else_1")
    assert branch["branch"] == ("case_pass" if score >= 80 else "else")


async def test_skipped_records_get_their_own_node_run(kit: Any) -> None:
    run = await kit.run(kit.load("branch"), inputs={"score": 90})

    skipped = [
        event.node_run
        for event in run.events
        if event.type == "node.completed" and event.data["status"] == "skipped"
    ]
    assert [record.node_id for record in skipped] == ["template_fail", "end_fail"]
    assert all(record.duration_ms == 0 and record.attempt == 1 for record in skipped)
    indexes = [event.node_run.index for event in run.events if event.type == "node.completed"]
    assert indexes == sorted(indexes) and len(set(indexes)) == len(indexes)


@pytest.mark.parametrize(("score", "grade"), [(95, "상"), (10, "하")])
async def test_join_runs_once_after_one_active_branch(kit: Any, score: int, grade: str) -> None:
    run = await kit.run(kit.load("join"), inputs={"score": score})

    assert run.outcome.status == "succeeded"
    assert run.started().count("variable_aggregator_1") == 1
    assert run.started().count("template_join") == 1
    assert run.outcome.outputs == {"grade": f"{score}점은 {grade} 등급"}
    aggregator = next(
        event.node_run
        for event in run.events
        if event.type == "node.completed" and event.data["node_id"] == "variable_aggregator_1"
    )
    expected = "template_high.output" if grade == "상" else "template_low.output"
    assert aggregator.process == {"selected": expected}


async def test_aggregator_picks_vars_order_even_if_the_other_branch_finished_first(
    kit: Any, make_node: Node
) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("gate_a", "test_gate", {"value": "a"}),
            make_node("gate_b", "test_gate", {"value": "b"}),
            make_node(
                "variable_aggregator_1",
                "variable_aggregator",
                {"vars": ["gate_a.value", "gate_b.value"]},
            ),
            make_node(
                "end_1", "end", {"outputs": [{"name": "v", "var": "variable_aggregator_1.output"}]}
            ),
        ],
        [
            "start>gate_a",
            "start>gate_b",
            "gate_a>variable_aggregator_1",
            "gate_b>variable_aggregator_1",
            "variable_aggregator_1>end_1",
        ],
    )

    def release(event: Any) -> None:
        if event.type == "node.started" and event.data["node_id"] == "gate_b":
            kit.gate.open("gate_b")
        if event.type == "node.completed" and event.data["node_id"] == "gate_b":
            kit.gate.open("gate_a")

    run = await kit.run(graph, on_event=release)

    assert [n for n, _ in run.completed_order()][1:3] == ["gate_b", "gate_a"]
    assert run.started().count("variable_aggregator_1") == 1
    assert run.outcome.outputs == {"v": "a"}


async def test_aggregator_is_skipped_when_every_candidate_was_skipped(
    kit: Any, make_node: Node
) -> None:
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "n", "type": "number"}]}),
            make_node(
                "if_else_1",
                "if_else",
                {
                    "cases": [
                        {"id": "big", "conditions": [{"var": "start.n", "op": ">", "value": 5}]}
                    ]
                },
            ),
            make_node("template_big", "template", {"template": "큼"}),
            make_node("template_other", "template", {"template": "기타"}),
            make_node(
                "variable_aggregator_1",
                "variable_aggregator",
                {"vars": ["template_big.output"], "output_type": "string"},
            ),
            make_node("template_after", "template", {"template": "뒤"}),
            make_node(
                "end_1", "end", {"outputs": [{"name": "other", "var": "template_other.output"}]}
            ),
        ],
        [
            "start>if_else_1",
            "if_else_1:case_big>template_big",
            "if_else_1:else>template_other",
            "template_big>variable_aggregator_1",
            "template_other>variable_aggregator_1",
            "variable_aggregator_1>template_after",
            "template_other>end_1",
        ],
    )

    run = await kit.run(graph, inputs={"n": 1})

    statuses = run.statuses()
    assert statuses["template_big"] == "skipped"
    assert statuses["variable_aggregator_1"] == "skipped"
    # 노드가 스스로 skipped로 끝나면 나가는 엣지가 모두 비활성이 되어 뒤도 건너뛴다.
    assert statuses["template_after"] == "skipped"
    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"other": "기타"}


async def test_retry_runs_a_new_node_run_per_attempt(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node(
                "probe_1",
                "test_flaky",
                {"failures": 2, "value": "ok"},
                retry={"max_attempts": 2, "interval_ms": 0},
            ),
            make_node("end_1", "end", {"outputs": [{"name": "v", "var": "probe_1.value"}]}),
        ],
        ["start>probe_1", "probe_1>end_1"],
    )

    run = await kit.run(graph)

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"v": "ok"}
    probe = [e for e in run.events if e.data.get("node_id") == "probe_1"]
    assert [(e.type, e.data.get("status") or e.data.get("attempt")) for e in probe] == [
        ("node.started", 1),
        ("node.completed", "failed"),
        ("node.retrying", 2),
        ("node.started", 2),
        ("node.completed", "failed"),
        ("node.retrying", 3),
        ("node.started", 3),
        ("node.completed", "succeeded"),
    ]
    run_ids = {e.data["node_run_id"] for e in probe if e.type == "node.started"}
    assert len(run_ids) == 3
    retrying = run.of("node.retrying")[0]
    assert retrying["reason"] == "flaky"


async def test_non_retryable_errors_and_exhausted_retries_fail_the_run(
    kit: Any, make_node: Node
) -> None:
    def graph_with(config: dict[str, Any]) -> Any:
        return kit.graph(
            [
                make_node("start", "start"),
                make_node(
                    "probe_1", "test_flaky", config, retry={"max_attempts": 1, "interval_ms": 0}
                ),
                make_node("end_1", "end", {"outputs": [{"name": "v", "var": "probe_1.value"}]}),
            ],
            ["start>probe_1", "probe_1>end_1"],
        )

    fixed = await kit.run(graph_with({"failures": 5, "retryable": False, "code": "bad_input"}))
    assert fixed.outcome.status == "failed"
    assert fixed.outcome.error is not None and fixed.outcome.error.code == "bad_input"
    assert fixed.started().count("probe_1") == 1

    kit.flaky.calls.clear()
    exhausted = await kit.run(graph_with({"failures": 5}))
    assert exhausted.outcome.status == "failed"
    assert exhausted.started().count("probe_1") == 2
    assert exhausted.of("workflow.error")[0] == {
        "code": "flaky",
        "message": "2번째 실패",
        "node_id": "probe_1",
        "retryable": True,
    }


async def test_default_strategy_continues_with_default_outputs(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node(
                "probe_1",
                "test_flaky",
                {"failures": 9},
                error={"strategy": "default", "default_outputs": {"value": "기본값"}},
            ),
            make_node("end_1", "end", {"outputs": [{"name": "v", "var": "probe_1.value"}]}),
        ],
        ["start>probe_1", "probe_1>end_1"],
    )

    run = await kit.run(graph)

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"v": "기본값"}
    probe = next(d for d in run.of("node.completed") if d["node_id"] == "probe_1")
    assert probe["status"] == "failed"
    assert probe["error"]["code"] == "flaky"
    assert probe["outputs_preview"] == {"value": "기본값"}


async def test_branch_strategy_follows_only_the_fail_handle(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("probe_1", "test_flaky", {"failures": 9}, error={"strategy": "branch"}),
            make_node("template_ok", "template", {"template": "정상"}),
            make_node("template_err", "template", {"template": "실패 처리"}),
            make_node("end_ok", "end", {"outputs": [{"name": "v", "var": "template_ok.output"}]}),
            make_node("end_err", "end", {"outputs": [{"name": "v", "var": "template_err.output"}]}),
        ],
        [
            "start>probe_1",
            "probe_1>template_ok",
            "probe_1:fail>template_err",
            "template_ok>end_ok",
            "template_err>end_err",
        ],
    )

    run = await kit.run(graph)

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"v": "실패 처리"}
    statuses = run.statuses()
    assert statuses["probe_1"] == "failed"
    assert statuses["template_ok"] == "skipped" and statuses["end_ok"] == "skipped"
    probe = next(d for d in run.of("node.completed") if d["node_id"] == "probe_1")
    assert probe["branch"] == "fail"


async def test_success_with_a_branch_strategy_skips_the_fail_side(
    kit: Any, make_node: Node
) -> None:
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node("probe_1", "test_flaky", {"value": 1}, error={"strategy": "branch"}),
            make_node("template_err", "template", {"template": "실패 처리"}),
            make_node("end_ok", "end", {"outputs": [{"name": "v", "var": "probe_1.value"}]}),
            make_node("end_err", "end", {"outputs": [{"name": "e", "var": "template_err.output"}]}),
        ],
        ["start>probe_1", "probe_1>end_ok", "probe_1:fail>template_err", "template_err>end_err"],
    )

    run = await kit.run(graph)

    assert run.outcome.outputs == {"v": 1}
    assert run.statuses()["template_err"] == "skipped"


async def test_unimplemented_nodes_fail_regardless_of_error_strategy(
    kit: Any, make_node: Node
) -> None:
    kit.registry.register(PendingNode())
    graph = kit.graph(
        [
            make_node("start", "start"),
            make_node(
                "pending_1",
                "test_pending",
                {"anything": "x"},
                error={"strategy": "default", "default_outputs": {"text": "x"}},
                retry={"max_attempts": 3, "interval_ms": 0},
            ),
            make_node("end_1", "end", {"outputs": [{"name": "text", "var": "pending_1.text"}]}),
        ],
        ["start>pending_1", "pending_1>end_1"],
    )

    run = await kit.run(graph)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert run.outcome.error.code == "node_not_implemented"
    assert run.started().count("pending_1") == 1
    assert "M19" in run.outcome.error.message
