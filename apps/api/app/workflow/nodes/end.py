# pyright: strict
"""end 노드(설계 2.3, 3.4). 워크플로우 전용.

닿아도 런을 멈추지 않는다. 엔진이 실행된 end 노드들의 출력을 완료 순서로 합치고 같은 이름은 나중
값이 덮어쓴다(Dify 문서의 Output 노드와 같다). 나가는 핸들이 없다.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Issue
from app.workflow.nodes.base import (
    NAME_PATTERN,
    WORKFLOW_ONLY,
    GraphView,
    NodeContext,
    NodeResult,
    NodeRunner,
    VarRef,
    duplicate_names,
)


class EndOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    var: str


class EndConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outputs: list[EndOutput] = Field(default_factory=list[EndOutput])


class EndRunner(NodeRunner[EndConfig]):
    type = "end"
    Config = EndConfig
    kinds = WORKFLOW_ONLY

    def handles(self, config: EndConfig) -> list[str]:
        return []

    def var_refs(self, config: EndConfig) -> list[VarRef]:
        return [VarRef(f"outputs[{i}].var", item.var) for i, item in enumerate(config.outputs)]

    def validate(self, config: EndConfig, graph: GraphView) -> list[Issue]:
        return duplicate_names((item.name for item in config.outputs), "outputs[{}].name")

    async def run(
        self, ctx: NodeContext, config: EndConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        values = {item.name: ctx.resolve(item.var) for item in config.outputs}
        return NodeResult(status="succeeded", run_outputs=values, inputs=values)
