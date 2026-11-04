"""describe_sql_source: 데이터 소스의 표 정의(DDL)·설명·샘플값·용어집을 보는 내장 도구(3단계 10.3).

query_sql과 함께 붙고(그룹 sql), sql_schema 노드와 같은 DDL 글을 돌려준다. 숨긴 열·표는 없다. 열
이름·설명·샘플값은 개행을 접은 데이터라 지시로 읽히지 않는다(14.1). 승인 등급은 read다.
"""

from collections.abc import Sequence
from typing import Any

from app.agent.tools.base import SqlToolError, ToolContext, ToolResult
from app.agent.tools.query_sql import SourceTool, SqlSourceChoice, source_property

DESCRIBE_SQL_SOURCE = "describe_sql_source"

_GUIDE = (
    "이 에이전트에 붙은 데이터 소스의 표·열 정의(DDL)와 설명, 샘플값, 용어집을 본다. query_sql로 "
    "SQL을 쓰기 전에 먼저 부르고, 표·열 이름은 여기 나온 것만 쓴다."
)


def describe_schema(sources: Sequence[SqlSourceChoice]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"source": source_property(sources)},
        "required": ["source"],
        "additionalProperties": False,
    }


class DescribeSqlSourceTool(SourceTool):
    name = DESCRIBE_SQL_SOURCE
    group = "sql"
    description = _GUIDE
    guide = _GUIDE
    input_schema = describe_schema(())

    def build_schema(self, sources: Sequence[SqlSourceChoice]) -> dict[str, Any]:
        return describe_schema(sources)

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        chosen = self.chosen(ctx, data)
        if isinstance(chosen, ToolResult):
            return chosen
        source, port = chosen
        try:
            ddl = await port.describe(source.source_id)
        except SqlToolError as exc:
            return ToolResult.error(f"{exc.code}: {exc.message}", code=exc.code)
        return ToolResult.ok(ddl)
