from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.agent.tools.write_file import MAX_WRITE_BYTES
from app.sandbox.base import SandboxError


class EditFileTool(Tool):
    name = "edit_file"
    group = "files"
    description = (
        "작업공간의 텍스트 파일에서 old_text를 new_text로 한 번 바꾼다. old_text는 파일에 정확히 "
        "한 번 나와야 한다. 여러 군데라면 앞뒤 내용을 더 넣어 하나로 좁힌다."
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
            "old_text": {"type": "string", "minLength": 1, "description": "바꿀 원래 내용"},
            "new_text": {"type": "string", "description": "새 내용"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        path = str(data["path"])
        old_text = str(data["old_text"])
        new_text = str(data["new_text"])
        try:
            content = await ctx.backend.read_file(ctx.handle, path, 0, MAX_WRITE_BYTES + 1)
        except SandboxError as exc:
            return ToolResult.from_sandbox_error(exc)
        if content.encoding == "base64":
            return ToolResult.error(f"binary_file: {path}은(는) 텍스트 파일이 아니다")
        if content.truncated:
            return ToolResult.error(
                f"too_large: {path}이(가) {MAX_WRITE_BYTES // 2**20}MB보다 커서 고칠 수 없다"
            )
        count = content.content.count(old_text)
        if count == 0:
            return ToolResult.error(f"not_found: {path}에 old_text가 없다")
        if count > 1:
            return ToolResult.error(
                f"ambiguous: {path}에 old_text가 {count}번 나온다. "
                "앞뒤 내용을 더 넣어 하나로 좁힌다"
            )
        updated = content.content.replace(old_text, new_text, 1)
        try:
            written = await ctx.backend.write_file(ctx.handle, path, updated.encode("utf-8"))
        except SandboxError as exc:
            return ToolResult.from_sandbox_error(exc)
        return ToolResult.ok(f"{path}을(를) 고쳤다({written}바이트).")
