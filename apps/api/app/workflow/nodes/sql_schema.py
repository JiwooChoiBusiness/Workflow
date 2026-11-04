# pyright: strict
"""sql_schema 노드(3단계 설계 10.3). 데이터 소스 스냅샷에서 모델에 줄 DDL 글을 만든다.

DB에 닿지 않는다. 준비 단계가 뜬 스냅샷과 표·열 설명, 샘플값, 용어집으로 `CREATE TABLE …` 글을
만들고(app/knowledge/sql/ddl.py), 숨긴 열·표는 넣지 않는다. 보통 `{{ sql_schema_1.ddl }}`을 SQL을
만드는 llm 노드의 시스템 프롬프트에 넣는다. 토큰 상한을 넘으면 표 목록만 내고 경고를 남긴다.
"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.knowledge.sql.ddl import schema_text
from app.schemas.workflow import Issue
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
)
from app.workflow.ports import SqlPort, SqlPortError, SqlSourceInfo

DEFAULT_MAX_TOKENS = 8000


class SqlSchemaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: uuid.UUID | None = None
    # 비우면 보이는 표 전부.
    tables: list[str] = Field(default_factory=list[str], max_length=200)
    include_samples: bool = True
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=500, le=64_000)


def source_issues(source_id: uuid.UUID | None, graph: GraphView) -> list[Issue]:
    """데이터 소스 선택 검사. sql_query도 쓴다(11장의 sql_source_not_found)."""
    if source_id is None:
        return [
            Issue(
                level="error",
                code="sql_source_required",
                message="데이터 소스를 고른다",
                field="source_id",
            )
        ]
    known = graph.sql_sources()
    if known is None:
        return []
    ref = known.get(source_id)
    if ref is None:
        return [
            Issue(
                level="error",
                code="sql_source_not_found",
                message="데이터 소스를 찾을 수 없다. 지워졌거나 다른 PC에서 가져온 노드다",
                field="source_id",
            )
        ]
    if ref.paid:
        return [
            Issue(
                level="warning",
                code="paid_key_required",
                message=(
                    f"데이터 소스 {ref.name}은(는) 유료 키 전용이다. GEMINI_PAID_API_KEY가 없으면 "
                    "실행·발행할 수 없고, 이 워크플로우의 모델 호출은 모두 유료 키로 간다"
                ),
                field="source_id",
            )
        ]
    return []


def sql_port(ctx: NodeContext) -> SqlPort:
    port = ctx.ports.sql
    if port is None:
        raise NodeFailure("sql_unavailable", "데이터 소스를 쓸 수 없는 실행 환경이다")
    return port


async def source_info(port: SqlPort, source_id: uuid.UUID | None) -> SqlSourceInfo:
    if source_id is None:
        raise NodeFailure("sql_source_required", "데이터 소스가 없다")
    try:
        return await port.describe(source_id)
    except SqlPortError as exc:
        raise NodeFailure(exc.code, exc.message, retryable=exc.retryable) from None


class SqlSchemaRunner(NodeRunner[SqlSchemaConfig]):
    type = "sql_schema"
    Config = SqlSchemaConfig
    error_strategies = ALL_STRATEGIES
    fixed_outputs = (
        OutputSpec("ddl", "string"),
        OutputSpec("tables", "array[object]"),
        OutputSpec("glossary", "string"),
    )

    def validate(self, config: SqlSchemaConfig, graph: GraphView) -> list[Issue]:
        return source_issues(config.source_id, graph)

    async def run(
        self, ctx: NodeContext, config: SqlSchemaConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        info = await source_info(sql_port(ctx), config.source_id)
        text = schema_text(
            info.view,
            name=info.name,
            glossary=info.glossary,
            tables=config.tables,
            include_samples=config.include_samples,
            max_tokens=config.max_tokens,
        )
        warnings: list[str] = []
        if text.truncated:
            warnings.append(
                f"schema_too_large: 스키마가 {config.max_tokens}토큰을 넘어 표 목록만 냈다. 큰 "
                "스키마는 지식 베이스로 내보내 검색한다"
            )
        if text.missing:
            warnings.append(f"table_not_found: 없는 표를 건너뛰었다: {', '.join(text.missing)}")
        return NodeResult(
            status="succeeded",
            outputs={"ddl": text.ddl, "tables": text.tables, "glossary": info.glossary},
            inputs={
                "source_id": str(info.id),
                "tables": list(config.tables),
                "include_samples": config.include_samples,
            },
            process={"tokens": text.tokens, "truncated": text.truncated},
            warnings=warnings,
        )
