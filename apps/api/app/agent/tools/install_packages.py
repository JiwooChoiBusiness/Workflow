from collections.abc import Mapping
from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolDefaults, ToolResult, run_process
from app.sandbox.base import ExecSpec, SandboxError
from app.sandbox.python_runner import PIP_TIMEOUT_SEC, validate_pip_specs


class InstallPackagesTool(Tool):
    # 설계 4.4의 upgrade 인자는 받지 않는다. 백엔드가 언제나 --upgrade로 설치한다(3.8 pip 명령).
    name = "install_packages"
    group = "shell"
    uses_process = True
    description = (
        "작업공간 전용 폴더(.packages)에 파이썬 패키지를 pip로 설치한다. 셸에서 pip을 직접 부르지 "
        "말고 이 도구를 쓴다. 패키지 이름과 버전 조건(예: requests, pandas>=2)만 받는다."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "packages": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "description": "설치할 패키지 스펙 목록",
            },
        },
        "required": ["packages"],
        "additionalProperties": False,
    }

    def timeout_for(self, data: Mapping[str, Any], defaults: ToolDefaults) -> int | None:
        return PIP_TIMEOUT_SEC

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        try:
            # 스펙이 틀리면 프로세스를 띄우지 않고 바로 오류로 돌려준다(3.8).
            specs = validate_pip_specs([str(item) for item in data["packages"]])
        except SandboxError as exc:
            return ToolResult.from_sandbox_error(exc)
        spec = ExecSpec(
            exec_id=ctx.call_id,
            kind="pip",
            packages=specs,
            env=dict(ctx.env),
            timeout_sec=PIP_TIMEOUT_SEC,
            memory_limit_mb=ctx.memory_limit_mb,
            max_processes=ctx.max_processes,
        )
        return await run_process(ctx, spec)
