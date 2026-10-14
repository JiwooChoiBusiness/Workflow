# pyright: strict
"""code 노드(설계 2.3, 9.1).

사용자가 쓴 파이썬 def main(**inputs) -> dict를 런 작업 폴더에서 python -I로 돌린다(code_runner).
stdlib만 쓸 수 있고 승인은 없다(코드는 설계 시점에 사용자가 쓴 것이다). 상한은 시간(timeout_sec,
없으면 WF_CODE_TIMEOUT_SEC, 60초까지), 메모리 256MB, 프로세스 8개(Job Object), 반환값 JSON
256KB다. 상한마다 오류 코드가 다르다: timeout, memory_limit, output_too_large, stdout_too_large.
반환값이 dict가 아니거나 JSON으로 바꿀 수 없으면 invalid_output이다.

반환 dict에서 선언한 출력만 꺼내 엔진이 선언 타입으로 맞춘다. 선언한 이름이 없으면 invalid_output,
선언하지 않은 이름은 경고만 남긴다. stdout·stderr는 앞 32KB를 노드 런 process에 남긴다.
"""

import ast
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.sandbox.base import SandboxError
from app.schemas.events import NodeError
from app.schemas.workflow import Issue, VarType
from app.workflow.code_runner import MEMORY_LIMIT_MB, CodeRun, run_code
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    NAME_PATTERN,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    VarRef,
    duplicate_names,
)

MAX_TIMEOUT_SEC = 60
# DSL로 가져온 code 노드에 붙는 ui 플래그(설계 5.1, 9.3). 사용자가 코드를 읽고 "검토 완료"를 눌러
# 초안 저장에서 이 키를 지울 때까지 검증 오류로 실행·발행을 막는다.
IMPORTED_CODE_FLAG = "imported_code"
# 프로세스 기동·종료와 결과 파일 읽기에 쓰는 여유. 실제 시간 상한은 샌드박스가 건다.
TIMEOUT_GRACE_SEC = 15
# 반환이 안 남았을 때 최대 메모리가 상한의 이만큼을 넘었으면 메모리 때문에 죽은 것으로 본다.
MEMORY_KILL_RATIO = 0.9


class CodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    var: str


class CodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    type: VarType = "any"


class CodeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = ""
    inputs: list[CodeInput] = Field(default_factory=list[CodeInput])
    outputs: list[CodeOutput] = Field(default_factory=list[CodeOutput])
    # 비우면 WF_CODE_TIMEOUT_SEC(기본 30초).
    timeout_sec: int | None = Field(default=None, ge=1, le=MAX_TIMEOUT_SEC)


def code_issues(code: str) -> list[Issue]:
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError) as exc:
        # 널 문자처럼 행이 없는 오류도 SyntaxError로 온다(3.12). 3.11까지는 ValueError였다.
        line = getattr(exc, "lineno", None)
        detail = exc.msg if isinstance(exc, SyntaxError) else str(exc)
        message = f"{line}행: {detail}" if line else detail
        return [Issue(level="error", code="code_syntax", message=message, field="code")]
    has_main = any(isinstance(item, ast.FunctionDef) and item.name == "main" for item in tree.body)
    if has_main:
        return []
    message = "코드에 def main(...) 함수가 있어야 한다"
    return [Issue(level="error", code="code_main_missing", message=message, field="code")]


def _failed(
    code: str, message: str, process: dict[str, Any], inputs: dict[str, Any], *, retryable: bool
) -> NodeResult:
    return NodeResult(
        status="failed",
        error=NodeError(code=code, message=message, retryable=retryable),
        process=process,
        inputs=inputs,
    )


class CodeRunner(NodeRunner[CodeConfig]):
    type = "code"
    Config = CodeConfig
    error_strategies = ALL_STRATEGIES
    dynamic_outputs = True
    # 샌드박스 프로세스는 PC 메모리를 따로 쓰므로 동시에 하나만 돈다(3.4, 8장).
    concurrency = 1

    def outputs(self, config: CodeConfig) -> list[OutputSpec]:
        return [OutputSpec(item.name, item.type) for item in config.outputs]

    def var_refs(self, config: CodeConfig) -> list[VarRef]:
        return [VarRef(f"inputs[{i}].var", item.var) for i, item in enumerate(config.inputs)]

    def validate(self, config: CodeConfig, graph: GraphView) -> list[Issue]:
        issues = code_issues(config.code)
        issues += duplicate_names((item.name for item in config.inputs), "inputs[{}].name")
        issues += duplicate_names((item.name for item in config.outputs), "outputs[{}].name")
        node = graph.node(graph.node_id)
        if node is not None and node.ui.get(IMPORTED_CODE_FLAG) is True:
            issues.append(
                Issue(
                    level="error",
                    code="imported_code_unreviewed",
                    message="가져온 코드다. 코드를 읽고 검토 완료를 눌러야 실행·발행할 수 있다",
                    field="code",
                )
            )
        return issues

    def timeout_sec(self, config: CodeConfig) -> float | None:
        return (config.timeout_sec or MAX_TIMEOUT_SEC) + TIMEOUT_GRACE_SEC

    async def run(
        self, ctx: NodeContext, config: CodeConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        sandbox = ctx.ports.sandbox
        if sandbox is None:
            raise NodeFailure("sandbox_unavailable", "code 노드를 돌릴 샌드박스가 없다")
        inputs = {item.name: ctx.resolve(item.var) for item in config.inputs}
        timeout = config.timeout_sec or ctx.ports.node_settings.code_timeout_sec
        try:
            handle = await sandbox.acquire(ctx.run_id)
            run = await run_code(
                sandbox,
                handle,
                node_id=ctx.node_id,
                exec_id=f"wf-{ctx.node_id}-{ctx.node_run_id.hex[-8:]}",
                code=config.code,
                inputs=inputs,
                timeout_sec=timeout,
            )
        except SandboxError as exc:
            # 샌드박스 파이썬이 없거나 런 폴더를 못 만든 경우. 코드와 상관없는 환경 문제다.
            raise NodeFailure(exc.code, exc.message) from None
        process = _process(run, timeout)
        failure = code_failure(run, timeout)
        if failure is not None:
            code, message, retryable = failure
            return _failed(code, message, process, inputs, retryable=retryable)
        result = run.result or {}
        declared = [item.name for item in config.outputs]
        missing = [name for name in declared if name not in result]
        if missing:
            message = f"main()이 돌려준 dict에 선언한 출력이 없다: {', '.join(missing)}"
            return _failed("invalid_output", message, process, inputs, retryable=False)
        extra = sorted(set(result) - set(declared))
        if extra:
            ctx.warn(f"undeclared_output: {', '.join(extra)}은(는) 선언하지 않아 버렸다")
        return NodeResult(
            status="succeeded",
            outputs={name: result[name] for name in declared},
            inputs=inputs,
            process=process,
        )


def _process(run: CodeRun, timeout: int) -> dict[str, Any]:
    peak = run.peak_memory_bytes
    process: dict[str, Any] = {
        "exit_code": run.exit_code,
        "duration_ms": run.duration_ms,
        "timeout_sec": timeout,
        "timed_out": run.timed_out,
        "peak_memory_mb": round(peak / 2**20, 1) if peak is not None else None,
        "stdout": run.stdout,
        "stderr": run.stderr,
    }
    if run.error is not None and run.error.traceback:
        process["traceback"] = run.error.traceback
    return process


def code_failure(run: CodeRun, timeout: int) -> tuple[str, str, bool] | None:
    """(오류 코드, 메시지, 재시도 가능) 또는 성공이면 None."""
    if run.timed_out:
        return "timeout", f"코드가 {timeout}초 안에 끝나지 않았다", True
    if run.stdio_limit:
        return "stdout_too_large", "print 출력이 1MB를 넘어 코드를 멈췄다", False
    error = run.error
    if error is not None:
        if error.kind in ("invalid_output", "output_too_large", "memory_limit"):
            return error.kind, error.message, False
        if error.kind in ("main_missing", "syntax"):
            return "invalid_code", error.message, False
        return "code_error", error.message, True
    if run.result is not None:
        return None
    peak = run.peak_memory_bytes
    if peak is not None and peak >= MEMORY_LIMIT_MB * 2**20 * MEMORY_KILL_RATIO:
        return "memory_limit", "메모리 상한을 넘어 프로세스가 끝났다", False
    if run.killed:
        return "code_killed", "코드 프로세스가 중지됐다", True
    message = f"코드 프로세스가 결과 없이 끝났다(종료 코드 {run.exit_code})"
    if run.stderr.strip():
        message += f": {run.stderr.strip().splitlines()[-1][:300]}"
    return "code_crashed", message, True
