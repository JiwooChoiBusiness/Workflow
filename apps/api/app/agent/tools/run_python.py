from collections.abc import Mapping
from typing import Any

from app.agent.tools.base import (
    Tool,
    ToolContext,
    ToolDefaults,
    ToolResult,
    clamp_timeout,
    run_process,
)
from app.agent.tools.shell import WORKDIR_SCHEMA, timeout_schema
from app.sandbox.base import ExecSpec


class RunPythonTool(Tool):
    name = "run_python"
    group = "shell"
    uses_process = True
    description = (
        "작업공간에서 파이썬 코드를 실행하고 종료 코드와 출력을 돌려준다. "
        "install_packages로 설치한 패키지를 import할 수 있다. 결과를 보려면 print로 출력한다."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200_000,
                "description": "실행할 파이썬 코드",
            },
            "timeout_sec": timeout_schema(120),
            "workdir": WORKDIR_SCHEMA,
        },
        "required": ["code"],
        "additionalProperties": False,
    }

    def workdir(self, data: Mapping[str, Any]) -> str | None:
        return str(data.get("workdir") or ".")

    def timeout_for(self, data: Mapping[str, Any], defaults: ToolDefaults) -> int | None:
        return clamp_timeout(
            data.get("timeout_sec"), defaults.timeout_sec, defaults.max_timeout_sec
        )

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        timeout = clamp_timeout(
            data.get("timeout_sec"), ctx.defaults.timeout_sec, ctx.defaults.max_timeout_sec
        )
        spec = ExecSpec(
            exec_id=ctx.call_id,
            kind="python",
            script=str(data["code"]),
            cwd=str(data.get("workdir") or "."),
            env=dict(ctx.env),
            timeout_sec=timeout,
            memory_limit_mb=ctx.memory_limit_mb,
            max_processes=ctx.max_processes,
        )
        return await run_process(ctx, spec)
