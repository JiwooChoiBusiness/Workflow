from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult, utf8_len
from app.sandbox.base import SandboxError

MAX_WRITE_BYTES = 2 * 1024 * 1024


class WriteFileTool(Tool):
    name = "write_file"
    group = "files"
    description = (
        "작업공간에 텍스트 파일을 UTF-8(BOM 없음)로 쓴다. 상위 폴더는 자동으로 만든다. "
        "append가 true면 파일 끝에 덧붙인다. 한 번에 2MB까지."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "작업공간 기준 상대 경로. 사용자에게 줄 결과는 outputs/ 아래에 둔다",
            },
            "content": {"type": "string", "description": "파일 내용"},
            "append": {"type": "boolean", "description": "true면 덧붙인다. 기본 false"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        path = str(data["path"])
        content = str(data["content"])
        size = utf8_len(content)
        if size > MAX_WRITE_BYTES:
            return ToolResult.error(
                f"too_large: 내용이 {size}바이트다. write_file은 한 번에 "
                f"{MAX_WRITE_BYTES // 2**20}MB까지 쓴다. 나눠서 append로 쓴다",
                code="too_large",
            )
        append = bool(data.get("append", False))
        try:
            written = await ctx.backend.write_file(
                ctx.handle, path, content.encode("utf-8"), append=append
            )
        except SandboxError as exc:
            return ToolResult.from_sandbox_error(exc)
        verb = "덧붙였다" if append else "썼다"
        return ToolResult.ok(f"{path}에 {written}바이트를 {verb}.")
