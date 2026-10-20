"""loop 노드와 반복 변수(2단계 설계 2.4, 3.5, 3.7, 11.2의 loop 100회).

반복 변수는 하위 그래프의 variable_assigner가 "<loop id>.<이름>"으로 바꾸고, 반복이 끝날 때마다 종료
조건을 본다. 종료 조건·상한, 반복 변수의 처음 값, 하위 그래프에서 읽기, 검증 규칙(중첩 금지,
loop_start 하나, 반복 변수 참조), 실패·취소, 반복 안 입력 대기와 재개, 단일 노드 실행을 본다.
"""

import asyncio
from typing import Any

from app.runner.supervisor import CancelSignal
from app.workflow.graph import validate

Node = Any


def counter_loop(
    kit: Any,
    make_node: Node,
    *,
    conditions: list[dict[str, Any]] | None = None,
    max_iterations: int = 10,
    inner: list[dict[str, Any]] | None = None,
    inner_edges: list[str] | None = None,
    loop_vars: list[dict[str, Any]] | None = None,
    kind: str = "workflow",
) -> Any:
    """start → loop_1[loop_start_1 → variable_assigner_1(n += 1)] → end_1(n, loop_count)."""
    variables = loop_vars or [{"name": "n", "type": "number", "value": 0}]
    body = inner or [
        make_node(
            "variable_assigner_1",
            "variable_assigner",
            {"items": [{"target": "loop_1.n", "op": "+=", "value": 1}]},
            parent_id="loop_1",
        )
    ]
    last = "answer_1" if kind == "chatflow" else "end_1"
    tail = (
        make_node("answer_1", "answer", {"text": "{{ loop_1.n }}/{{ loop_1.loop_count }}"})
        if kind == "chatflow"
        else make_node(
            "end_1",
            "end",
            {
                "outputs": [
                    {"name": "n", "var": "loop_1.n"},
                    {"name": "count", "var": "loop_1.loop_count"},
                ]
            },
        )
    )
    return kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "limit", "type": "number"}]}),
            make_node(
                "loop_1",
                "loop",
                {
                    "loop_vars": variables,
                    "break_conditions": conditions or [],
                    "max_iterations": max_iterations,
                },
            ),
            make_node("loop_start_1", "loop_start", parent_id="loop_1"),
            *body,
            tail,
        ],
        [
            "start>loop_1",
            f"loop_1>{last}",
            *(inner_edges or ["loop_start_1>variable_assigner_1"]),
        ],
    )


def node_runs(run: Any, node_id: str) -> list[Any]:
    return [
        event.node_run
        for event in run.events
        if event.type == "node.completed" and event.data["node_id"] == node_id
    ]


async def test_counter_stops_when_the_break_condition_holds(kit: Any, make_node: Node) -> None:
    graph = counter_loop(kit, make_node, conditions=[{"var": "loop_1.n", "op": "≥", "value": 3}])

    assert validate(graph, "workflow") == []
    run = await kit.run(graph)

    assert run.outcome.status == "succeeded", run.outcome.error
    assert run.outcome.outputs == {"n": 3, "count": 3}
    assert run.of("iteration.started") == [{"node_id": "loop_1", "total": 10}]
    assert [data["index"] for data in run.of("iteration.next")] == [0, 1, 2]
    [completed] = run.of("iteration.completed")
    assert (completed["count"], completed["failed_count"]) == (3, 0)
    records = node_runs(run, "variable_assigner_1")
    assert [(r.iteration_node_id, r.iteration_index) for r in records] == [
        ("loop_1", 0),
        ("loop_1", 1),
        ("loop_1", 2),
    ]
    [loop_run] = node_runs(run, "loop_1")
    assert loop_run.outputs == {"n": 3, "loop_count": 3}
    assert loop_run.process["stopped"] == "condition"
    assert loop_run.process["last_check"] == {"loop_1.n": 3}
    assert loop_run.warnings == []
    # loop_start는 실행하지 않고 index만 넣는다.
    assert "loop_start_1" not in run.started()


async def test_max_iterations_ends_the_loop_with_a_warning(kit: Any, make_node: Node) -> None:
    graph = counter_loop(
        kit, make_node, conditions=[{"var": "loop_1.n", "op": ">", "value": 99}], max_iterations=4
    )

    run = await kit.run(graph)

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs == {"n": 4, "count": 4}
    [loop_run] = node_runs(run, "loop_1")
    assert loop_run.process["stopped"] == "limit"
    assert any(warning.startswith("loop_limit_reached") for warning in loop_run.warnings)


async def test_without_conditions_the_loop_runs_max_iterations_quietly(
    kit: Any, make_node: Node
) -> None:
    graph = counter_loop(kit, make_node, max_iterations=2)

    run = await kit.run(graph)

    assert run.outcome.outputs == {"n": 2, "count": 2}
    [loop_run] = node_runs(run, "loop_1")
    assert loop_run.warnings == []


async def test_inner_nodes_read_loop_variables_index_and_outer_values(
    kit: Any, make_node: Node
) -> None:
    template = make_node(
        "template_1",
        "template",
        {
            "template": "{{ i }}:{{ acc }}+{{ limit }}",
            "vars": [
                {"name": "i", "var": "loop_start_1.index"},
                {"name": "acc", "var": "loop_1.text"},
                {"name": "limit", "var": "start.limit"},
            ],
        },
        parent_id="loop_1",
    )
    assigner = make_node(
        "variable_assigner_1",
        "variable_assigner",
        {"items": [{"target": "loop_1.text", "op": "overwrite", "var": "template_1.output"}]},
        parent_id="loop_1",
    )
    graph = counter_loop(
        kit,
        make_node,
        loop_vars=[{"name": "text", "type": "string", "value": "시작"}],
        # 종료 조건이 그 반복의 하위 노드 출력과 loop_start의 index를 읽는다.
        conditions=[
            {"var": "template_1.output", "op": "contains", "value": "1:"},
            {"var": "loop_start_1.index", "op": "≥", "value": 1},
        ],
        inner=[template, assigner],
        inner_edges=["loop_start_1>template_1", "template_1>variable_assigner_1"],
    )
    raw = graph.model_dump()
    raw["nodes"][-1]["config"]["outputs"] = [{"name": "text", "var": "loop_1.text"}]
    graph = type(graph).model_validate(raw)

    assert validate(graph, "workflow") == []
    run = await kit.run(graph, inputs={"limit": 7})

    assert run.outcome.status == "succeeded", run.outcome.error
    assert run.outcome.outputs == {"text": "1:0:시작+7+7"}


async def test_initial_values_come_from_upstream_and_are_coerced(kit: Any, make_node: Node) -> None:
    graph = counter_loop(
        kit,
        make_node,
        loop_vars=[
            {"name": "n", "type": "number", "var": "start.limit"},
            {"name": "label", "type": "string", "value": 5},
        ],
        conditions=[{"var": "loop_1.n", "op": "≥", "value": 12}],
    )
    raw = graph.model_dump()
    raw["nodes"][-1]["config"]["outputs"].append({"name": "label", "var": "loop_1.label"})
    graph = type(graph).model_validate(raw)

    run = await kit.run(graph, inputs={"limit": "10"})

    assert run.outcome.status == "succeeded", run.outcome.error
    assert run.outcome.outputs == {"n": 12, "count": 2, "label": "5"}


async def test_chatflow_answer_reads_the_final_values(kit: Any, make_node: Node) -> None:
    graph = counter_loop(
        kit,
        make_node,
        kind="chatflow",
        conditions=[{"var": "loop_1.n", "op": "=", "value": 2}],
    )

    run = await kit.run(graph, kind="chatflow", system={"query": "hi"})

    assert run.outcome.status == "succeeded", run.outcome.error
    assert run.outcome.answer == "2/2"


def test_validation_rules(kit: Any, make_node: Node) -> None:
    graph = counter_loop(
        kit,
        make_node,
        loop_vars=[
            {"name": "loop_count", "type": "number", "value": 0},
            {"name": "n", "type": "number", "value": "많이"},
            {"name": "m", "type": "number", "var": "start.limit", "value": 1},
        ],
        conditions=[
            {"var": "loop_1.nope", "op": "=", "value": 1},
            {"var": "loop_1.n", "op": "contains", "value": "x"},
        ],
    )
    raw = graph.model_dump()
    raw["nodes"][3]["config"]["items"] = [
        {"target": "loop_1.ghost", "op": "+=", "value": 1},
        {"target": "conv.topic", "op": "overwrite", "value": "x"},
    ]
    graph = type(graph).model_validate(raw)

    found = {(issue.code, issue.node_id, issue.field) for issue in validate(graph, "workflow")}

    assert ("reserved_name", "loop_1", "loop_vars[0].name") in found
    assert ("type_mismatch", "loop_1", "loop_vars[1].value") in found
    assert ("invalid_config", "loop_1", "loop_vars[2]") in found
    assert ("unknown_variable", "loop_1", "break_conditions[0].var") in found
    assert ("invalid_operator", "loop_1", "break_conditions[1].op") in found
    assert ("unknown_variable", "variable_assigner_1", "items[0].target") in found
    # 워크플로우에는 대화 변수가 없다.
    assert ("unknown_variable", "variable_assigner_1", "items[1].target") in found


def test_structure_rules(kit: Any, make_node: Node) -> None:
    graph = counter_loop(kit, make_node)
    raw = graph.model_dump()
    raw["nodes"] += [
        make_node("iteration_1", "iteration", {"input_var": "x.y", "output_var": "x.y"}),
        make_node("loop_2", "loop", {}, parent_id="iteration_1"),
        make_node("loop_start_2", "loop_start", parent_id="iteration_1"),
        make_node("iteration_start_9", "iteration_start", parent_id="loop_1"),
        make_node("loop_start_9", "loop_start"),
    ]
    graph = type(graph).model_validate(raw)

    found = {(issue.code, issue.node_id) for issue in validate(graph, "workflow")}

    assert ("nested_iteration", "loop_2") in found
    assert ("invalid_parent", "loop_start_2") in found
    assert ("invalid_parent", "iteration_start_9") in found
    assert ("invalid_parent", "loop_start_9") in found
    assert ("iteration_start_count", "iteration_1") in found


def test_inner_references_and_assigner_placement(kit: Any, make_node: Node) -> None:
    template = make_node(
        "template_1",
        "template",
        {"template": "{{ c }}", "vars": [{"name": "c", "var": "loop_1.loop_count"}]},
        parent_id="loop_1",
    )
    graph = counter_loop(kit, make_node, inner=[template], inner_edges=["loop_start_1>template_1"])
    found = {(issue.code, issue.node_id, issue.field) for issue in validate(graph, "workflow")}
    assert ("unknown_variable", "template_1", "vars[0].var") in found

    outside = kit.graph(
        [
            make_node("start", "start"),
            make_node(
                "variable_assigner_1",
                "variable_assigner",
                {"items": [{"target": "conv.a", "op": "clear"}]},
            ),
            make_node("end_1", "end"),
        ],
        ["start>variable_assigner_1", "variable_assigner_1>end_1"],
    )
    # loop 밖 워크플로우의 variable_assigner는 예전처럼 둘 수 없다.
    assert ("node_not_allowed", "variable_assigner_1") in {
        (issue.code, issue.node_id) for issue in validate(outside, "workflow")
    }


async def test_a_failing_round_fails_the_loop_and_the_run(kit: Any, make_node: Node) -> None:
    flaky = make_node(
        "test_flaky_1",
        "test_flaky",
        {"failures": 9, "retryable": False, "code": "boom"},
        parent_id="loop_1",
    )
    graph = counter_loop(
        kit,
        make_node,
        inner=[
            flaky,
            make_node(
                "variable_assigner_1",
                "variable_assigner",
                {"items": [{"target": "loop_1.n", "op": "+=", "value": 1}]},
                parent_id="loop_1",
            ),
        ],
        inner_edges=["loop_start_1>variable_assigner_1", "variable_assigner_1>test_flaky_1"],
    )

    run = await kit.run(graph)

    assert run.outcome.status == "failed"
    error = run.outcome.error
    assert error is not None and (error.node_id, error.code) == ("loop_1", "boom")
    assert error.message.startswith("0번째 반복의 test_flaky_1:")
    [completed] = run.of("iteration.completed")
    assert (completed["count"], completed["failed_count"]) == (1, 1)


async def test_a_break_condition_that_cannot_compare_fails_the_loop(
    kit: Any, make_node: Node
) -> None:
    graph = counter_loop(
        kit,
        make_node,
        loop_vars=[{"name": "n", "type": "any", "value": "글"}],
        conditions=[{"var": "loop_1.n", "op": ">", "value": 3}],
    )
    raw = graph.model_dump()
    raw["nodes"][3]["config"]["items"] = [{"target": "loop_1.n", "op": "overwrite", "value": "글"}]
    graph = type(graph).model_validate(raw)

    run = await kit.run(graph)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None and run.outcome.error.code == "type_mismatch"


async def test_stop_cancels_a_running_loop(kit: Any, make_node: Node) -> None:
    gate = make_node("test_gate_1", "test_gate", {"gate": "never"}, parent_id="loop_1")
    graph = counter_loop(
        kit,
        make_node,
        inner=[
            gate,
            make_node(
                "variable_assigner_1",
                "variable_assigner",
                {"items": [{"target": "loop_1.n", "op": "+=", "value": 1}]},
                parent_id="loop_1",
            ),
        ],
        inner_edges=["loop_start_1>test_gate_1", "test_gate_1>variable_assigner_1"],
    )
    cancel = CancelSignal()
    task = asyncio.create_task(kit.run(graph, cancel=cancel))
    async with asyncio.timeout(5):
        while "test_gate_1" not in kit.gate.entered:
            await asyncio.sleep(0.005)
    cancel.set("user")

    run = await asyncio.wait_for(task, 5)

    assert run.outcome.status == "cancelled"
    assert kit.gate.cancelled == ["test_gate_1"]
    assert run.statuses()["loop_1"] == "cancelled"


async def test_waiting_inside_a_loop_keeps_the_variables_and_resumes_the_round(
    kit: Any, make_node: Node
) -> None:
    waiter = make_node("test_wait_1", "test_wait", {"value": "확인"}, parent_id="loop_1")
    graph = counter_loop(
        kit,
        make_node,
        conditions=[{"var": "loop_1.n", "op": "≥", "value": 2}],
        inner=[
            make_node(
                "variable_assigner_1",
                "variable_assigner",
                {"items": [{"target": "loop_1.n", "op": "+=", "value": 1}]},
                parent_id="loop_1",
            ),
            waiter,
        ],
        inner_edges=["loop_start_1>variable_assigner_1", "variable_assigner_1>test_wait_1"],
    )

    first = await kit.run(graph)

    assert first.outcome.status == "waiting_input"
    pending = first.outcome.pending
    assert pending is not None
    assert (pending.node_id, pending.iteration_index, pending.kind) == (
        "test_wait_1",
        0,
        "approval",
    )
    checkpoint = first.outcome.checkpoint
    assert checkpoint is not None
    assert checkpoint.node_state["loop_1"]["round"] == 0
    assert checkpoint.node_states["loop_1"] == "waiting_input"
    assert checkpoint.node_states["test_wait_1@0"] == "waiting_input"
    # 첫 반복의 할당이 반복 변수 자리에 남았다.
    assert checkpoint.pool["loop_1.n"] == 1
    assert checkpoint.frames == [{"iteration_id": "loop_1", "index": 0}]

    second = await kit.run(graph, resume=checkpoint)

    assert second.outcome.status == "waiting_input"
    pending = second.outcome.pending
    assert pending is not None and pending.iteration_index == 1
    assert second.of("workflow.resumed") == [{"node_id": "test_wait_1"}]
    assert [data["index"] for data in second.of("iteration.next")] == [0, 1]
    checkpoint = second.outcome.checkpoint
    assert checkpoint is not None and checkpoint.pool["loop_1.n"] == 2

    third = await kit.run(graph, resume=checkpoint)

    assert third.outcome.status == "succeeded", third.outcome.error
    assert third.outcome.outputs == {"n": 2, "count": 2}


async def test_single_node_run_of_a_loop(kit: Any, make_node: Node) -> None:
    graph = counter_loop(
        kit,
        make_node,
        loop_vars=[{"name": "n", "type": "number", "var": "start.limit"}],
        conditions=[{"var": "loop_1.n", "op": "≥", "value": 5}],
    )

    run = await kit.run(graph, single_node="loop_1", seed={"start.limit": 3})

    assert run.outcome.status == "succeeded", run.outcome.error
    assert run.outcome.outputs == {"n": 5, "loop_count": 2}


def test_the_catalog_shows_loop_kinds_for_the_assigner() -> None:
    from app.workflow.nodes import default_registry

    described = {item.type: item for item in default_registry().describe()}

    assert described["variable_assigner"].kinds == ["chatflow"]
    assert described["variable_assigner"].loop_kinds == ["chatflow", "workflow"]
    assert described["loop"].dynamic_outputs is True
    assert described["loop_start"].outputs[0].name == "index"
    schema = described["loop"].config_schema
    assert schema["properties"]["max_iterations"]["maximum"] == 100
