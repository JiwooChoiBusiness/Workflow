# pyright: strict
"""sql_query 노드(3단계 설계 10.3~10.6). SQL을 검증해 데이터 소스에서 읽기 전용으로 실행한다.

SQL은 템플릿(보통 `{{ llm_1.text }}`) 또는 변수에서 받고, 첫 ```sql 펜스 안을 쓴다(펜스가 없으면
전체). 검증·실행·기록은 SqlPort가 콘솔 SQL 시험·query_sql 도구와 같은 경로로 한다.

approval=always(노드 또는 데이터 소스 기본값)면 검증을 통과한 SQL로 human_input과 같은 폼 대기를
만든다. 폼은 {sql: 고칠 수 있는 SQL, decision: approve·reject}이고, 재개하면 받은 SQL을 다시 검증해
실행한다. 거절은 sql_rejected다(오류 정책을 탄다).

실패하면 출력 error에 "<코드>: <문장>"을, sql_executed에 시도한 SQL(승인 대기였으면 카드에 보인
SQL)을 싣는다. 검증·실행 실패뿐 아니라 데이터 소스 상태, 거절, 폼 응답 오류, 대기 만료도 같다.
error.strategy=default면 엔진이 이 값을 기본값보다 먼저 써서 뒤 노드가 오류와 SQL로 고쳐 다시
돌리거나 거절을 가를 수 있다(10.3 템플릿). 성공하면 error는 빈 글이다. 결과가 max_rows를 넘으면
truncated이고 export_file이면 CSV를 파일로 낸다.
"""

import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.knowledge.sql.validate import extract_sql
from app.schemas.citations import one_line
from app.schemas.events import NodeError, WorkflowFormData, WorkflowFormField
from app.schemas.sql_source import LLM_BYTES_CAP, SQL_TIMEOUT_CAP
from app.schemas.workflow import Issue
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    TemplateField,
    VarRef,
)
from app.workflow.nodes.human_input import FormWaiting, open_form, take_reply
from app.workflow.nodes.sql_schema import source_info, source_issues, sql_port
from app.workflow.nodes.start import InputError, StartField, coerce_inputs
from app.workflow.ports import FilePortError, SqlPortError, SqlSourceInfo
from app.workflow.render import TemplateError
from app.workflow.variables import VariableError

APPROVE = "approve"
REJECT = "reject"
MAX_SQL_CHARS = 32 * 1024
# 프로세스 시작·종료와 실행 슬롯을 기다리는 몫까지 넉넉히 둔다. 실제 질의 시간은 timeout_sec가
# 막는다.
NODE_TIMEOUT_SEC = SQL_TIMEOUT_CAP * 2 + 30


class SqlQueryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: uuid.UUID | None = None
    # SQL 템플릿. sql_var를 주면 그 변수(string)를 쓴다.
    sql: str = ""
    sql_var: str | None = None
    # 비우면 데이터 소스의 limits. max_rows는 SQL_MAX_ROWS까지다.
    max_rows: int | None = Field(default=None, ge=1, le=10_000)
    timeout_sec: int | None = Field(default=None, ge=1, le=SQL_TIMEOUT_CAP)
    # 비우면 데이터 소스의 approval.
    approval: Literal["none", "always"] | None = None
    llm_rows: int | None = Field(default=None, ge=1, le=10_000)
    llm_bytes: int | None = Field(default=None, ge=1024, le=LLM_BYTES_CAP)
    # 결과가 잘리면 받은 행(max_rows행까지)을 CSV 파일로 낸다.
    export_file: bool = True


class SqlWaiting(FormWaiting):
    # 폼에 채운 검증된 SQL. 같은 폼을 다시 띄울 때 쓴다.
    sql: str


def approval_fields(sql: str) -> list[StartField]:
    return [
        StartField(
            name="sql",
            type="paragraph",
            label="SQL",
            required=True,
            default=sql,
            max_length=MAX_SQL_CHARS,
        ),
        StartField(
            name="decision",
            type="select",
            label="실행",
            required=True,
            default=APPROVE,
            options=[APPROVE, REJECT],
        ),
    ]


def approval_form(waiting: SqlWaiting) -> WorkflowFormData:
    return WorkflowFormData(
        form_id=waiting.form_id,
        description=waiting.description,
        fields=[
            WorkflowFormField.model_validate(field.model_dump())
            for field in approval_fields(waiting.sql)
        ],
        created_at=waiting.created_at,
    )


def approval_text(info: SqlSourceInfo) -> str:
    return (
        f"**SQL 승인** · 데이터 소스 {one_line(info.name)}\n\n"
        "아래 SQL을 실행할지 정한다. 고쳐서 승인하면 다시 검증한 뒤 실행하고, 거절하면 이 노드가 "
        "sql_rejected로 실패한다."
    )


class SqlQueryRunner(NodeRunner[SqlQueryConfig]):
    type = "sql_query"
    Config = SqlQueryConfig
    error_strategies = ALL_STRATEGIES
    # 같은 런 안에서도 하나씩 돈다(code 노드와 같다). 프로세스 전체는 SqlRuntime이 하나로 묶는다.
    concurrency = 1
    fixed_outputs = (
        OutputSpec("columns", "array[string]"),
        OutputSpec("rows", "array[object]"),
        OutputSpec("row_count", "number"),
        OutputSpec("truncated", "boolean"),
        OutputSpec("sql_executed", "string"),
        OutputSpec("result_text", "string"),
        OutputSpec("file", "file"),
        OutputSpec("error", "string"),
    )

    def template_fields(self, config: SqlQueryConfig) -> list[TemplateField]:
        return [] if config.sql_var else [TemplateField("sql", config.sql)]

    def var_refs(self, config: SqlQueryConfig) -> list[VarRef]:
        return [VarRef("sql_var", config.sql_var, "string")] if config.sql_var else []

    def validate(self, config: SqlQueryConfig, graph: GraphView) -> list[Issue]:
        issues = source_issues(config.source_id, graph)
        if not config.sql_var and not config.sql.strip():
            issues.append(
                Issue(
                    level="error",
                    code="sql_required",
                    message="실행할 SQL(템플릿)이나 SQL이 든 변수를 정한다",
                    field="sql",
                )
            )
        return issues

    def timeout_sec(self, config: SqlQueryConfig) -> float | None:
        return NODE_TIMEOUT_SEC

    def _sql(self, ctx: NodeContext, config: SqlQueryConfig) -> str:
        if config.sql_var:
            value = ctx.resolve(config.sql_var, "string")
            raw = "" if value is None else str(value)
        else:
            raw = ctx.render(config.sql)
        return extract_sql(raw)

    async def run(
        self, ctx: NodeContext, config: SqlQueryConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        shown: dict[str, Any] = {}
        try:
            return await self._run(ctx, config, state, shown)
        except (NodeFailure, TemplateError, VariableError) as exc:
            # 출력 없이 실패하면 error.strategy=default에서 error가 비어 뒤 노드가 성공으로 본다.
            retryable = isinstance(exc, NodeFailure) and exc.retryable
            retry_after = exc.retry_after if isinstance(exc, NodeFailure) else None
            return NodeResult(
                status="failed",
                error=NodeError(
                    code=exc.code, message=exc.message, retryable=retryable, retry_after=retry_after
                ),
                outputs={
                    "error": f"{exc.code}: {exc.message}",
                    "sql_executed": shown.get("sql", ""),
                },
                # 데이터 소스를 읽기 전에 실패했으면 엔진이 이 노드가 읽은 변수를 입력으로 남긴다.
                inputs=shown or None,
            )

    async def _run(
        self,
        ctx: NodeContext,
        config: SqlQueryConfig,
        state: dict[str, Any] | None,
        shown: dict[str, Any],
    ) -> NodeResult:
        port = sql_port(ctx)
        info = await source_info(port, config.source_id)
        max_rows = min(config.max_rows or info.max_rows, info.max_rows_cap)
        approval = config.approval or info.approval
        shown.update({"source_id": str(info.id), "max_rows": max_rows, "approval": approval})
        if approval == "always":
            # 대기가 만료돼 take_reply가 input_timeout으로 끝나도 카드에 보인 SQL을 출력에 싣는다.
            saved_sql = (state or {}).get("sql")
            if isinstance(saved_sql, str):
                shown["sql"] = saved_sql
            waiting, given = take_reply(ctx, state, SqlWaiting)
            if waiting is None:
                sql = self._sql(ctx, config)
                shown["sql"] = sql
                try:
                    validated = await port.validate(info.id, sql, max_rows=max_rows)
                except SqlPortError as exc:
                    return _failed(exc, sql, shown)
                opened = open_form(ctx, approval_text(info))
                waiting = SqlWaiting(**opened.model_dump(), sql=validated)
            shown.update({"form_id": waiting.form_id, "sql": waiting.sql})
            if given is None:
                return NodeResult(
                    status="waiting_input",
                    pending=approval_form(waiting),
                    state=waiting.model_dump(mode="json"),
                    inputs=dict(shown),
                )
            try:
                values = coerce_inputs(approval_fields(waiting.sql), given)
            except InputError as exc:
                raise NodeFailure(
                    "invalid_inputs", f"승인 폼 응답이 올바르지 않다: {exc}"
                ) from None
            if values.get("decision") != APPROVE:
                raise NodeFailure("sql_rejected", "사용자가 SQL 실행을 거절했다")
            sql = str(values.get("sql") or "")
            shown["approved"] = True
        else:
            sql = self._sql(ctx, config)
        shown["sql"] = sql
        files = ctx.ports.files
        file_warnings: list[str] = []

        async def save_csv(path: Path) -> dict[str, Any] | None:
            if files is None:
                return None
            try:
                return await files.save_output(
                    path, filename=f"{ctx.node_id}-result.csv", mime="text/csv"
                )
            except FilePortError as exc:
                file_warnings.append(
                    f"file_not_saved: 잘린 결과 CSV를 남기지 못했다: {exc.message}"
                )
                return None

        try:
            run = await port.execute(
                info.id,
                sql,
                max_rows=max_rows,
                timeout_sec=config.timeout_sec or info.timeout_sec,
                llm_rows=config.llm_rows or info.llm_rows,
                llm_bytes=config.llm_bytes or info.llm_bytes,
                save_csv=save_csv if config.export_file else None,
            )
        except SqlPortError as exc:
            return _failed(exc, sql, shown)
        warnings = list(file_warnings)
        if run.truncated:
            warnings.append(f"truncated: 결과가 {max_rows}행을 넘어 잘렸다")
        if run.rows_cut:
            warnings.append(
                f"rows_cut: 행이 1MB를 넘어 rows에는 {len(run.rows)}행만 담았다"
                + (" (받은 행은 CSV 파일)" if run.file else "")
            )
        return NodeResult(
            status="succeeded",
            outputs={
                "columns": run.columns,
                "rows": run.rows,
                "row_count": run.row_count,
                "truncated": run.truncated,
                "sql_executed": run.sql_executed,
                "result_text": run.result_text,
                "file": run.file,
                "error": "",
            },
            inputs=shown,
            process={"elapsed_ms": run.elapsed_ms, "row_count": run.row_count},
            warnings=warnings,
        )


def _failed(exc: SqlPortError, sql: str, inputs: dict[str, Any]) -> NodeResult:
    # 출력 error·sql_executed는 error.strategy=default일 때 엔진이 기본값 대신 쓴다.
    return NodeResult(
        status="failed",
        error=NodeError(code=exc.code, message=exc.message, retryable=exc.retryable),
        outputs={"error": f"{exc.code}: {exc.message}", "sql_executed": sql},
        inputs=inputs,
        process={"stage": exc.stage},
    )
