# pyright: strict
"""variable_aggregator 노드(설계 2.3, 3.4).

후보 변수 중 vars 순서상 첫 번째로 "건너뛰지 않은" 노드의 값을 output으로 낸다. 합류 지점에 도착하는
순서는 비결정이라 쓰지 않는다. 후보가 모두 건너뛴 노드면 이 노드도 skipped가 된다.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import VarType
from app.workflow.nodes.base import NodeContext, NodeResult, NodeRunner, OutputSpec, VarRef


class AggregatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vars: list[str] = Field(min_length=1)
    output_type: VarType = "any"


class AggregatorRunner(NodeRunner[AggregatorConfig]):
    type = "variable_aggregator"
    Config = AggregatorConfig
    dynamic_outputs = True

    def outputs(self, config: AggregatorConfig) -> list[OutputSpec]:
        return [OutputSpec("output", config.output_type)]

    def var_refs(self, config: AggregatorConfig) -> list[VarRef]:
        return [
            VarRef(f"vars[{i}]", item, config.output_type) for i, item in enumerate(config.vars)
        ]

    async def run(
        self, ctx: NodeContext, config: AggregatorConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        for selector in config.vars:
            if ctx.is_skipped(selector):
                continue
            value = ctx.resolve(selector, config.output_type)
            return NodeResult(
                status="succeeded",
                outputs={"output": value},
                inputs={selector: value},
                process={"selected": selector},
            )
        return NodeResult(status="skipped", process={"selected": None})
