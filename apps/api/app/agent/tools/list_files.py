from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.sandbox.base import SandboxError

MAX_ENTRIES = 500


class ListFilesTool(Tool):
    name = "list_files"
    group = "files"
    description = (
        "작업공간의 파일과 폴더 목록을 돌려준다. 기본은 작업공간 루트 한 단계이고 '.'으로 시작하는 "
        "숨김 항목은 뺀다. 최대 500개."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "작업공간 기준 상대 경로. 기본 .",
            },
            "recursive": {"type": "boolean", "description": "하위 폴더까지. 기본 false"},
            "max_entries": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_ENTRIES,
                "description": "최대 항목 수. 기본 500",
            },
            "include_hidden": {"type": "boolean", "description": "숨김 항목 포함. 기본 false"},
        },
        "additionalProperties": False,
    }

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        path = str(data.get("path") or ".")
        limit = int(data.get("max_entries") or MAX_ENTRIES)
        try:
            entries = await ctx.backend.list_files(
                ctx.handle,
                path,
                recursive=bool(data.get("recursive", False)),
                max_entries=limit,
                include_hidden=bool(data.get("include_hidden", False)),
            )
        except SandboxError as exc:
            return ToolResult.from_sandbox_error(exc)
        if not entries:
            return ToolResult.ok(f"{path}: 비어 있다")
        lines: list[str] = []
        for entry in entries:
            if entry.type == "dir":
                lines.append(f"{entry.path}/")
            elif entry.type == "link":
                lines.append(f"{entry.path} (링크)")
            else:
                lines.append(f"{entry.path} ({entry.size}바이트)")
        if len(entries) >= limit:
            lines.append(f"[항목이 {limit}개에서 잘렸다. 더 좁은 경로로 다시 본다]")
        return ToolResult.ok("\n".join(lines))
