# pyright: strict
"""loop 노드와 loop_start 자리(2단계 설계 2.4, 3.5).

loop는 하위 그래프(parent_id가 이 노드)를 반복 변수가 바뀌어 가며 차례로 돌린다. 반복
변수(loop_vars)는 처음 값을 고정값이나 상류 변수에서 받아 풀의 "<loop id>" 자리에 두고, 하위 그래프
안의 variable_assigner가 target "<loop id>.<이름>"으로 바꾼다. 하위 그래프 노드는 반복 변수를
"<loop id>.<이름>"으로, 몇 번째 반복인지를 loop_start의 index(0부터)로 읽는다.

반복 하나가 끝날 때마다 break_conditions(if_else 케이스의 조건과 같은 구조)를 보고 참이면 멈춘다.
조건은 반복 변수, 그 반복의 하위 노드 출력, loop 상류 값을 읽는다. 조건이 없거나 끝내 참이 되지
않으면 max_iterations(100까지)번 돌고 멈춘다. 출력은 반복 변수의 마지막 값(이름마다)과 돈 횟수
loop_count다. iteration과 같이 중첩할 수 없고, 반복 하나가 실패하면 loop도 그 오류로 실패한다.

loop_start는 그래프에 저장되지만 실행하지 않는다. 엔진이 반복마다 index를 그 자리에 넣는다.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Issue, VarType
from app.workflow.nodes.base import (
    NAME_PATTERN,
    GraphView,
    LoopPlan,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    VarRef,
    duplicate_names,
)
from app.workflow.nodes.if_else import Condition, condition_issues, evaluate
from app.workflow.nodes.iteration import EmptyConfig
from app.workflow.variables import (
    PoolView,
    SelectorError,
    VariableError,
    coerce,
    parse_selector,
)

MAX_ITERATIONS = 100
COUNT_OUTPUT = "loop_count"


class LoopVar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    type: VarType = "any"
    # 처음 값. var(상류 변수 선택자)와 value(고정값) 중 하나다. 둘 다 없으면 null이다.
    var: str | None = None
    value: Any = None


class LoopConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loop_vars: list[LoopVar] = Field(default_factory=list[LoopVar])
    # 반복 하나가 끝날 때 평가한다. 비어 있으면 max_iterations번 돈다.
    break_conditions: list[Condition] = Field(default_factory=list[Condition])
    logical: Literal["and", "or"] = "and"
    max_iterations: int = Field(default=10, ge=1, le=MAX_ITERATIONS)


def _issue(code: str, message: str, field: str) -> Issue:
    return Issue(level="error", code=code, message=message, field=field)


class LoopRunner(NodeRunner[LoopConfig]):
    type = "loop"
    Config = LoopConfig
    container = True
    dynamic_outputs = True

    def outputs(self, config: LoopConfig) -> list[OutputSpec]:
        return [
            *(OutputSpec(var.name, var.type) for var in config.loop_vars),
            OutputSpec(COUNT_OUTPUT, "number"),
        ]

    def inner_outputs(self, config: LoopConfig) -> list[OutputSpec] | None:
        return [OutputSpec(var.name, var.type) for var in config.loop_vars]

    def var_refs(self, config: LoopConfig) -> list[VarRef]:
        refs = [
            VarRef(f"loop_vars[{i}].var", var.var, var.type)
            for i, var in enumerate(config.loop_vars)
            if var.var is not None
        ]
        refs += [
            VarRef(f"break_conditions[{i}].var", condition.var, inner=True)
            for i, condition in enumerate(config.break_conditions)
        ]
        return refs

    def validate(self, config: LoopConfig, graph: GraphView) -> list[Issue]:
        issues = duplicate_names((var.name for var in config.loop_vars), "loop_vars[{}].name")
        for index, var in enumerate(config.loop_vars):
            field = f"loop_vars[{index}]"
            if var.name == COUNT_OUTPUT:
                message = f"{COUNT_OUTPUT}은(는) 반복 횟수 출력의 이름이라 반복 변수로 쓸 수 없다"
                issues.append(_issue("reserved_name", message, f"{field}.name"))
            has_value = "value" in var.model_fields_set
            if var.var is not None and has_value:
                issues.append(_issue("invalid_config", "var와 value를 함께 쓸 수 없다", field))
            elif has_value and var.value is not None:
                try:
                    coerce(var.value, var.type)
                except VariableError:
                    message = f"처음 값이 {var.type} 타입이 아니다"
                    issues.append(_issue("type_mismatch", message, f"{field}.value"))
        for index, condition in enumerate(config.break_conditions):
            var_type = graph.output_type(condition.var)
            issues += condition_issues(condition, var_type, f"break_conditions[{index}]")
        return issues

    async def run(
        self, ctx: NodeContext, config: LoopConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        variables: dict[str, Any] = {}
        if state is None:
            for var in config.loop_vars:
                raw = ctx.resolve(var.var) if var.var is not None else var.value
                try:
                    variables[var.name] = coerce(raw, var.type)
                except VariableError as exc:
                    raise NodeFailure(exc.code, f"반복 변수 {var.name}: {exc.message}") from None
        checked: dict[str, Any] = {}

        def until(view: PoolView) -> bool:
            values: dict[str, Any] = {}

            def holds(condition: Condition) -> bool:
                if condition.var not in values:
                    try:
                        selector = parse_selector(condition.var)
                    except SelectorError as exc:
                        raise NodeFailure("invalid_selector", str(exc)) from None
                    values[condition.var] = view.lookup(selector).value
                return evaluate(condition.op, values[condition.var], condition.value)

            results = (holds(condition) for condition in config.break_conditions)
            done = all(results) if config.logical == "and" else any(results)
            checked.clear()
            checked.update(values)
            return done

        plan = LoopPlan(
            variables=variables,
            types={var.name: var.type for var in config.loop_vars},
            max_rounds=config.max_iterations,
            until=until if config.break_conditions else None,
        )
        outcome = await ctx.run_loop(plan, state)
        process: dict[str, Any] = {
            "rounds": outcome.rounds,
            "max_iterations": config.max_iterations,
            "stopped": outcome.stopped,
            "last_check": checked,
        }
        if outcome.status == "waiting_input":
            return NodeResult(status="waiting_input", state=outcome.state, process=process)
        if outcome.status == "cancelled":
            return NodeResult(status="cancelled", process=process)
        if outcome.status == "failed":
            first = outcome.failure
            if first is None or first.error is None:
                raise NodeFailure("loop_failed", "반복이 실패했다")
            where = f"{first.index}번째 반복"
            if first.node_id:
                where += f"의 {first.node_id}"
            # 다시 돌리면 이미 바뀐 반복 변수와 끝난 반복의 출력이 풀에 남아 있어 재시도하지 않는다.
            error = first.error.model_copy(
                update={"message": f"{where}: {first.error.message}", "retryable": False}
            )
            return NodeResult(status="failed", error=error, process=process)
        if outcome.stopped == "limit" and config.break_conditions:
            ctx.warn(
                "loop_limit_reached: 종료 조건이 참이 되기 전에 "
                f"{config.max_iterations}번을 다 돌았다"
            )
        return NodeResult(
            status="succeeded",
            outputs={**outcome.variables, COUNT_OUTPUT: outcome.rounds},
            process=process,
        )


class LoopStartRunner(NodeRunner[EmptyConfig]):
    """loop 하위 그래프의 진입 자리. 엔진이 반복마다 index를 넣고 이 노드는 돌리지 않는다."""

    type = "loop_start"
    Config = EmptyConfig
    fixed_outputs = (OutputSpec("index", "number"),)

    async def run(
        self, ctx: NodeContext, config: EmptyConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        raise NodeFailure("not_runnable", "loop_start는 반복마다 엔진이 값을 넣는 자리다")
