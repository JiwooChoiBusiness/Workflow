# pyright: strict
"""template 노드(설계 2.3). vars로 고른 변수만 이름으로 넣어 Jinja2 템플릿을 렌더한다.

다른 노드의 텍스트 필드와 달리 변수 풀 전체가 아니라 vars가 컨텍스트다. 출력 상한 1MB는 렌더러가
template_limit으로 막는다.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Issue
from app.workflow.nodes.base import (
    NAME_PATTERN,
    GraphView,
    NodeContext,
    NodeResult,
    NodeRunner,
    OutputSpec,
    TemplateField,
    VarRef,
    duplicate_names,
)


class TemplateVar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    var: str


class TemplateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str = ""
    vars: list[TemplateVar] = Field(default_factory=list[TemplateVar])


class TemplateRunner(NodeRunner[TemplateConfig]):
    type = "template"
    Config = TemplateConfig
    fixed_outputs = (OutputSpec("output", "string"),)

    def var_refs(self, config: TemplateConfig) -> list[VarRef]:
        return [VarRef(f"vars[{i}].var", item.var) for i, item in enumerate(config.vars)]

    def template_fields(self, config: TemplateConfig) -> list[TemplateField]:
        names = frozenset(item.name for item in config.vars)
        return [TemplateField("template", config.template, names)]

    def validate(self, config: TemplateConfig, graph: GraphView) -> list[Issue]:
        return duplicate_names((item.name for item in config.vars), "vars[{}].name")

    async def run(
        self, ctx: NodeContext, config: TemplateConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        values = {item.name: ctx.resolve(item.var) for item in config.vars}
        text = ctx.render(config.template, values)
        return NodeResult(status="succeeded", outputs={"output": text}, inputs=values)
