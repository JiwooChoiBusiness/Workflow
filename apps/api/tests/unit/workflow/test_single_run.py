"""단일 노드 실행의 엔진 쪽(2단계 설계 6.1, 3.7의 6번).

노드 하나(iteration이면 하위 그래프까지)만 돌고, 참조 변수는 seed로 받으며, 그래프의 다른 곳에 있는
검증 오류는 막지 않는다. agent 노드는 미리보기 런과 같은 대기·재개를 거친다.
"""

from collections.abc import Callable
from typing import Any

import pytest

Node = Callable[..., dict[str, Any]]

AGENT_ID = "0190c1b2-0000-7000-8000-0000000000aa"


def flow(kit: Any, make_node: Node, *, broken_end: bool = False) -> Any:
    """start(q, score) → template_1 → if_else_1 → end_hi / end_lo, 옆 가지로 agent_1."""
    end_var = "nowhere.value" if broken_end else "template_1.output"
    return kit.graph(
        [
            make_node(
                "start",
                "start",
                {
                    "fields": [
                        {"name": "q", "type": "string"},
                        {"name": "score", "type": "number"},
                    ]
                },
            ),
            make_node(
                "template_1",
                "template",
                {"template": "[{{ x }}]", "vars": [{"name": "x", "var": "start.q"}]},
            ),
            make_node(
                "if_else_1",
                "if_else",
                {
                    "cases": [
                        {
                            "id": "hi",
                            "conditions": [{"var": "start.score", "op": "≥", "value": 50}],
                        }
                    ]
                },
            ),
            make_node(
                "agent_1",
                "agent",
                {"agent_id": AGENT_ID, "instruction": "{{ template_1.output }} 확인"},
            ),
            make_node("end_hi", "end", {"outputs": [{"name": "v", "var": end_var}]}),
            make_node("end_lo", "end", {"outputs": [{"name": "v", "var": "agent_1.text"}]}),
        ],
        [
            "start>template_1",
            "template_1>if_else_1",
            "template_1>agent_1",
            "if_else_1:case_hi>end_hi",
            "if_else_1:else>end_lo",
            "agent_1>end_lo",
        ],
    )


async def test_template_runs_alone_with_seeded_values(kit: Any, make_node: Node) -> None:
    run = await kit.run(flow(kit, make_node), single_node="template_1", seed={"start.q": "값"})

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"output": "[값]"}
    assert run.started() == ["template_1"]
    assert run.of("workflow.started")[0]["inputs_preview"] == {"start.q": "값"}
    assert run.outcome.node_runs == 1


@pytest.mark.parametrize(("score", "branch"), [(90, "case_hi"), (10, "else")])
async def test_if_else_runs_alone_and_reports_the_branch(
    kit: Any, make_node: Node, score: int, branch: str
) -> None:
    run = await kit.run(flow(kit, make_node), single_node="if_else_1", seed={"start.score": score})

    assert run.outcome.status == "succeeded"
    assert run.started() == ["if_else_1"]
    [completed] = run.of("node.completed")
    assert completed["branch"] == branch
    assert run.outcome.outputs == {}


async def test_errors_elsewhere_do_not_block_but_errors_in_the_node_do(
    kit: Any, make_node: Node
) -> None:
    broken = flow(kit, make_node, broken_end=True)

    fine = await kit.run(broken, single_node="template_1", seed={"start.q": "x"})
    blocked = await kit.run(broken, single_node="end_hi")
    missing = await kit.run(broken, single_node="no_such_node")

    assert fine.outcome.status == "succeeded"
    assert blocked.outcome.status == "failed"
    assert blocked.outcome.error is not None
    assert (blocked.outcome.error.code, blocked.outcome.error.node_id) == (
        "graph_invalid",
        "end_hi",
    )
    assert missing.outcome.error is not None and missing.outcome.error.code == "graph_invalid"


@pytest.mark.parametrize("key", ["template_1.output", "env.API", "start.q.inner", "not a key"])
async def test_seed_cannot_fill_the_node_itself_env_or_paths(
    kit: Any, make_node: Node, key: str
) -> None:
    run = await kit.run(flow(kit, make_node), single_node="template_1", seed={key: "x"})

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "invalid_inputs"


async def test_missing_seed_reads_as_null_with_a_warning(kit: Any, make_node: Node) -> None:
    run = await kit.run(flow(kit, make_node), single_node="template_1")

    assert run.outcome.outputs == {"output": "[]"}
    [completed] = run.of("node.completed")
    assert "missing_variable: start.q" in completed["warnings"]


async def test_agent_runs_alone_and_resumes_after_waiting(kit: Any, make_node: Node) -> None:
    tasks = kit.tasks()
    tasks.plan.append(lambda fake, child, spec: fake.waiting(child))
    ports = kit.ports(tasks=tasks)
    graph = flow(kit, make_node)
    seed = {"template_1.output": "[보고서]"}

    first = await kit.run(graph, single_node="agent_1", seed=seed, ports=ports)

    assert first.outcome.status == "waiting_input"
    assert first.outcome.pending is not None and first.outcome.pending.node_id == "agent_1"
    [spec] = tasks.specs
    assert spec.instruction == "[보고서] 확인"
    [child] = tasks.started
    tasks.settle(child, tasks.result(child, text="확인했다"))

    resumed = await kit.run(
        graph, single_node="agent_1", ports=ports, resume=first.outcome.checkpoint
    )

    assert resumed.outcome.status == "succeeded"
    assert resumed.outcome.outputs["text"] == "확인했다"
    assert resumed.outcome.outputs["child_run_id"] == str(child)
    assert resumed.started() == ["agent_1"]


def iteration_flow(kit: Any, make_node: Node) -> Any:
    return kit.graph(
        [
            make_node("start", "start"),
            make_node("list_1", "test_flaky", {"value": ["무시"]}),
            make_node(
                "iteration_1",
                "iteration",
                {"input_var": "list_1.value", "output_var": "template_2.output"},
            ),
            make_node("iteration_start_1", "iteration_start", parent_id="iteration_1"),
            make_node(
                "template_2",
                "template",
                {
                    "template": "{{ i }}={{ x }}",
                    "vars": [
                        {"name": "x", "var": "iteration_start_1.item"},
                        {"name": "i", "var": "iteration_start_1.index"},
                    ],
                },
                parent_id="iteration_1",
            ),
            make_node("end_1", "end", {"outputs": [{"name": "v", "var": "iteration_1.output"}]}),
        ],
        [
            "start>list_1",
            "list_1>iteration_1",
            "iteration_1>end_1",
            "iteration_start_1>template_2",
        ],
    )


async def test_iteration_runs_its_whole_subgraph_once(kit: Any, make_node: Node) -> None:
    run = await kit.run(
        iteration_flow(kit, make_node),
        single_node="iteration_1",
        seed={"list_1.value": ["a", "b"]},
    )

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"output": ["0=a", "1=b"], "count": 2, "failed_count": 0}
    assert run.started() == ["iteration_1", "template_2", "template_2"]


async def test_a_node_inside_an_iteration_runs_alone_with_a_given_item(
    kit: Any, make_node: Node
) -> None:
    run = await kit.run(
        iteration_flow(kit, make_node),
        single_node="template_2",
        seed={"iteration_start_1.item": "c", "iteration_start_1.index": 7},
    )

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"output": "7=c"}
    [started] = run.of("node.started")
    # 혼자 돌 때는 반복 프레임이 없다.
    assert started["iteration_id"] is None
