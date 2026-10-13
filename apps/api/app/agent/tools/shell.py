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
from app.sandbox.base import ExecSpec

WORKDIR_SCHEMA: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "maxLength": 200,
    "description": "작업 디렉터리. 작업공간 기준 상대 경로이고 기본은 작업공간 루트(.)다",
}


def timeout_schema(default: int) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": 900,
        "description": f"초 단위 제한 시간. 기본 {default}초, 최대 900초",
    }


class ShellTool(Tool):
    name = "shell"
    group = "shell"
    uses_process = True
    description = (
        "작업공간에서 셸 명령 하나를 실행하고 종료 코드와 출력(stdout·stderr)을 돌려준다. "
        "명령은 목적이 분명한 하나의 작업만 담는다. "
        "출력이 길면 파일로 저장하고 필요한 부분만 읽는다."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100_000,
                "description": "실행할 명령",
            },
            "timeout_sec": timeout_schema(120),
            "workdir": WORKDIR_SCHEMA,
        },
        "required": ["command"],
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
            kind="shell",
            script=str(data["command"]),
            cwd=str(data.get("workdir") or "."),
            env=dict(ctx.env),
            timeout_sec=timeout,
            memory_limit_mb=ctx.memory_limit_mb,
            max_processes=ctx.max_processes,
        )
        return await run_process(ctx, spec)
