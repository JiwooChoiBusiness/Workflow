from collections.abc import Mapping
from typing import Any, cast

from app.agent.tools.base import (
    DEFAULT_HTTP_TIMEOUT_SEC,
    MAX_HTTP_TIMEOUT_SEC,
    Tool,
    ToolContext,
    ToolDefaults,
    ToolResult,
    clamp_timeout,
)
from app.sandbox.base import HttpSpec, SandboxError
from app.sandbox.http_client import REQUEST_BODY_LIMIT, mask_headers

MAX_RESULT_HEADERS = 20
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")


def _headers(data: Mapping[str, Any]) -> dict[str, str]:
    raw = data.get("headers")
    if not isinstance(raw, Mapping):
        return {}
    headers = cast(Mapping[str, Any], raw)
    return {str(name): str(value) for name, value in headers.items()}


class HttpRequestTool(Tool):
    name = "http_request"
    group = "http"
    description = (
        "허용 목록에 있는 도메인에 HTTP 요청을 보내고 상태 코드·헤더·본문을 돌려준다. "
        "텍스트 본문은 64KB까지 보여 주고, 더 크거나 바이너리면 downloads/에 저장한 "
        "경로를 알려 준다. 사설 주소와 localhost에는 보낼 수 없다."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": list(METHODS), "description": "HTTP 메서드"},
            "url": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2048,
                "description": "http 또는 https URL",
            },
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "보낼 헤더(이름: 값)",
            },
            "body": {
                "type": "string",
                "maxLength": REQUEST_BODY_LIMIT,
                "description": "요청 본문(텍스트, 1MB까지)",
            },
            "timeout_sec": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_HTTP_TIMEOUT_SEC,
                "description": f"초 단위 제한 시간. 기본 {DEFAULT_HTTP_TIMEOUT_SEC}초",
            },
        },
        "required": ["method", "url"],
        "additionalProperties": False,
    }

    def display_input(self, data: Mapping[str, Any]) -> dict[str, Any]:
        shown = dict(data)
        if "headers" in shown:
            shown["headers"] = mask_headers(_headers(data))
        return shown

    def timeout_for(self, data: Mapping[str, Any], defaults: ToolDefaults) -> int | None:
        return clamp_timeout(
            data.get("timeout_sec"), DEFAULT_HTTP_TIMEOUT_SEC, MAX_HTTP_TIMEOUT_SEC
        )

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        body = data.get("body")
        spec = HttpSpec(
            exec_id=ctx.call_id,
            method=cast(Any, str(data["method"]).upper()),
            url=str(data["url"]),
            headers=_headers(data),
            body=None if body is None else str(body),
            timeout_sec=clamp_timeout(
                data.get("timeout_sec"), DEFAULT_HTTP_TIMEOUT_SEC, MAX_HTTP_TIMEOUT_SEC
            ),
        )
        try:
            result = await ctx.backend.http_request(ctx.handle, spec)
        except SandboxError as exc:
            return ToolResult.from_sandbox_error(exc)
        lines = [f"HTTP {result.status}", f"URL: {result.url}"]
        if result.redirects:
            lines.append("리다이렉트: " + " -> ".join(result.redirects))
        headers = mask_headers(result.headers)
        lines.append("헤더:")
        lines.extend(
            f"  {name}: {value}" for name, value in list(headers.items())[:MAX_RESULT_HEADERS]
        )
        if len(headers) > MAX_RESULT_HEADERS:
            lines.append(f"  (헤더 {len(headers) - MAX_RESULT_HEADERS}개 생략)")
        if result.body is not None:
            lines.append("본문:")
            lines.append(result.body)
            if result.truncated and result.saved_path:
                lines.append(
                    f"[본문 {result.body_bytes}바이트 중 앞부분이다. 전체는 {result.saved_path}에 "
                    "저장했다]"
                )
        elif result.saved_path:
            lines.append(f"본문 {result.body_bytes}바이트를 {result.saved_path}에 저장했다.")
        else:
            lines.append(f"본문 {result.body_bytes}바이트")
        return ToolResult.ok("\n".join(lines))
