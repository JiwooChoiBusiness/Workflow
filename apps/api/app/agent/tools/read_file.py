from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.sandbox.base import SandboxError

PAGE_BYTES = 64 * 1024


class ReadFileTool(Tool):
    name = "read_file"
    group = "files"
    description = (
        "작업공간의 텍스트 파일을 읽는다. 한 번에 64KB까지 돌려주고, 더 있으면 다음 "
        "offset을 알려 준다. 바이너리 파일은 크기만 알려 준다."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "작업공간 기준 상대 경로",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "description": "읽기 시작할 바이트 위치. 기본 0",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": PAGE_BYTES,
                "description": "읽을 최대 바이트 수. 기본·최대 65536",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        path = str(data["path"])
        try:
            content = await ctx.backend.read_file(
                ctx.handle,
                path,
                offset=int(data.get("offset") or 0),
                limit=int(data.get("limit") or PAGE_BYTES),
            )
        except SandboxError as exc:
            return ToolResult.from_sandbox_error(exc)
        if content.encoding == "base64":
            return ToolResult.ok(
                f"바이너리 파일이라 내용을 보여주지 않는다. 경로 {path}, 크기 {content.size}바이트."
            )
        text = content.content
        if content.truncated and content.next_offset is not None:
            text += (
                f"\n[파일 크기 {content.size}바이트 중 {content.next_offset}바이트까지 읽었다. "
                f"이어서 읽으려면 offset={content.next_offset}]"
            )
        return ToolResult.ok(text)
