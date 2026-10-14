# pyright: strict
"""iteration 노드와 iteration_start 자리(설계 2.3, 3.5).

iteration은 input_var 배열의 원소마다 하위 그래프(parent_id가 이 노드)를 한 번씩 돌리고 output_var가
가리키는 하위 노드 출력을 index 순으로 모은다. 반복을 돌리는 일은 엔진(NodeContext.run_frames)이
하고 여기서는 설정·검증과 결과를 error_mode대로 엮는 일만 한다.

- terminate: 첫 실패에서 남은 반복을 멈추고 iteration이 실패한다.
- continue: 실패한 반복 자리에 null을 둔다.
- remove: 실패한 반복을 결과에서 뺀다.

iteration_start는 그래프에 저장되지만 실행하지 않는다. 엔진이 반복마다 item·index를 그 자리에
넣는다.
"""

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Issue
from app.workflow.nodes.base import (
    FramePlan,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    VarRef,
)
from app.workflow.variables import SelectorError, parse_selector

MAX_ITEMS = 1000
MAX_PARALLEL = 4

ErrorMode = Literal["terminate", "continue", "remove"]


class IterationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_var: str
    # 하위 그래프 안 노드의 출력. 반복마다 이 값을 모아 output 배열을 만든다.
    output_var: str
    parallel: bool = False
    # parallel일 때 동시에 도는 반복 수. 반복 안 노드도 런의 동시 노드 상한(4)을 나눠 쓴다.
    max_parallel: int = Field(default=1, ge=1, le=MAX_PARALLEL)
    error_mode: ErrorMode = "terminate"


class EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IterationRunner(NodeRunner[IterationConfig]):
    type = "iteration"
    Config = IterationConfig
    container = True
    fixed_outputs = (
        OutputSpec("output", "array[any]"),
        OutputSpec("count", "number"),
        OutputSpec("failed_count", "number"),
    )

    def var_refs(self, config: IterationConfig) -> list[VarRef]:
        # output_var는 하위 그래프 안을 가리키므로 상류 검사 대신 validate에서 본다.
        return [VarRef("input_var", config.input_var, "array[any]")]

    def validate(self, config: IterationConfig, graph: GraphView) -> list[Issue]:
        try:
            selector = parse_selector(config.output_var)
        except SelectorError as exc:
            return [
                Issue(level="error", code="invalid_selector", message=str(exc), field="output_var")
            ]
        target = graph.node(selector.scope)
        if target is None or target.parent_id != graph.node_id:
            message = "output_var는 이 iteration 하위 그래프의 노드 출력을 가리켜야 한다"
            return [
                Issue(
                    level="error", code="unreachable_reference", message=message, field="output_var"
                )
            ]
        outputs = graph.outputs_of(target.id)
        if outputs is not None and selector.name not in outputs:
            message = f"{target.id}에 {selector.name} 출력이 없다"
            return [
                Issue(level="error", code="unknown_variable", message=message, field="output_var")
            ]
        issues: list[Issue] = []
        if config.parallel and config.max_parallel == 1:
            issues.append(
                Issue(
                    level="warning",
                    code="parallel_one",
                    message="max_parallel이 1이면 병렬로 켜도 하나씩 돈다",
                    field="max_parallel",
                )
            )
        return issues

    async def run(
        self, ctx: NodeContext, config: IterationConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        value = ctx.resolve(config.input_var, "array[any]")
        # 건너뛴 상류(null)는 빈 배열로 본다. 반복 없이 빈 output으로 끝난다.
        items: list[Any] = list(cast(list[Any], value)) if isinstance(value, list) else []
        if len(items) > MAX_ITEMS:
            raise NodeFailure(
                "iteration_limit", f"반복할 원소는 {MAX_ITEMS}개까지다({len(items)}개)"
            )
        parallel = config.max_parallel if config.parallel else 1
        plan = FramePlan(
            items=items,
            output=config.output_var,
            parallel=parallel,
            stop_on_failure=config.error_mode == "terminate",
        )
        outcome = await ctx.run_frames(plan, state)
        failed = [item for item in outcome.results if item.status == "failed"]
        process: dict[str, Any] = {
            "count": len(items),
            "parallel": parallel,
            "error_mode": config.error_mode,
            "failed_indices": [item.index for item in failed],
        }
        if outcome.status == "waiting_input":
            return NodeResult(status="waiting_input", state=outcome.state, process=process)
        if outcome.status == "cancelled":
            return NodeResult(status="cancelled", process=process)
        if outcome.status == "failed":
            first = outcome.failure
            if first is None or first.error is None:
                raise NodeFailure("iteration_failed", "반복이 실패했다")
            where = f"{first.index}번째 반복"
            if first.node_id:
                where += f"의 {first.node_id}"
            # 하위 노드의 오류를 그대로 올리되 재시도는 하지 않는다. 다시 돌리면 이미 끝난 반복의
            # 출력이 풀에 남아 있어 같은 자리에 쓸 수 없다.
            error = first.error.model_copy(
                update={"message": f"{where}: {first.error.message}", "retryable": False}
            )
            return NodeResult(status="failed", error=error, process=process)
        results = outcome.results
        if config.error_mode == "remove":
            output = [item.value for item in results if item.status == "succeeded"]
        else:
            output = [item.value if item.status == "succeeded" else None for item in results]
        return NodeResult(
            status="succeeded",
            outputs={"output": output, "count": len(items), "failed_count": len(failed)},
            process=process,
        )


class IterationStartRunner(NodeRunner[EmptyConfig]):
    """하위 그래프의 진입 자리. 엔진이 반복마다 item·index를 넣고 이 노드는 돌리지 않는다."""

    type = "iteration_start"
    Config = EmptyConfig
    fixed_outputs = (OutputSpec("item", "any"), OutputSpec("index", "number"))

    async def run(
        self, ctx: NodeContext, config: EmptyConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        raise NodeFailure("not_runnable", "iteration_start는 반복마다 엔진이 값을 넣는 자리다")
