# pyright: strict
"""web_fetch 도구. 웹 페이지를 가져와 글만 남긴다.

요청은 http_request 노드처럼 1단계 HttpClient(허용 목록, 사설·링크 로컬·메타데이터 주소 차단,
리다이렉트 재검사, 본문 상한)로만 나간다. 64KB가 넘는 응답은 HttpClient가 런 작업 폴더의
downloads/에 내려받으므로 그 파일을 5MB까지 읽는다. HTML이면 html_text로 글만 남기고, 글이 아닌
응답(PDF·이미지 등)은 unsupported_content_type이다. 글은 max_length자에서 자르고 경고를 남긴다.
4xx·5xx는 오류가 아니라 출력이고 연결 오류·시간 초과만 재시도할 수 있는 오류다.
"""

import asyncio
import base64
import re
from collections.abc import Mapping
from typing import Any

from app.sandbox.base import FileContent, HttpSpec, SandboxError
from app.sandbox.http_client import is_textual
from app.workflow.html_text import html_to_text
from app.workflow.nodes.base import NodeContext, NodeFailure, OutputSpec
from app.workflow.tools.base import Tool, ToolParam, ToolResult
from app.workflow.variables import MAX_STRING_BYTES

FETCH_TIMEOUT_SEC = 30
MAX_FETCH_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_LENGTH = 100_000
MAX_URL_CHARS = 2048
RETRYABLE_CODES = frozenset({"http_timeout", "http_failed"})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_ACCEPT = "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"


def _media(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _charset(content_type: str | None) -> str:
    match = re.search(r"charset=\"?([A-Za-z0-9._-]+)", content_type or "", re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _decode(content: FileContent, content_type: str | None) -> str:
    if content.encoding == "utf-8":
        return content.content
    # 샌드박스가 UTF-8이 아니라고 본 글(EUC-KR 등)은 base64로 온다. 응답의 charset으로 푼다.
    data = base64.b64decode(content.content)
    try:
        return data.decode(_charset(content_type), errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _cut(text: str, limit: int) -> tuple[str, bool]:
    cut = len(text) > limit
    if cut:
        text = text[:limit]
    data = text.encode("utf-8")
    if len(data) > MAX_STRING_BYTES:
        return data[:MAX_STRING_BYTES].decode("utf-8", errors="ignore"), True
    return text, cut


class WebFetchTool(Tool):
    name = "web_fetch"
    label = "웹 페이지 가져오기"
    description = "허용 목록에 있는 웹 페이지를 가져와 글만 남긴다"
    params = (
        ToolParam(
            "url",
            "string",
            "주소",
            "http·https 주소. 호스트가 허용 목록에 있어야 한다",
            required=True,
            max_length=MAX_URL_CHARS,
        ),
        ToolParam(
            "max_length",
            "number",
            "최대 글자 수",
            "이보다 긴 글은 앞부분만 남긴다",
            default=DEFAULT_MAX_LENGTH,
        ),
    )
    outputs = (
        OutputSpec("text", "string"),
        OutputSpec("title", "string"),
        OutputSpec("url", "string"),
        OutputSpec("status_code", "number"),
        OutputSpec("content_type", "string"),
    )
    timeout_sec = FETCH_TIMEOUT_SEC + 5

    def check(self, name: str, value: Any) -> str | None:
        if name == "url" and isinstance(value, str):
            if not re.match(r"^https?://", value.strip(), re.IGNORECASE):
                return "http:// 또는 https://로 시작해야 한다"
        if name == "max_length" and isinstance(value, int | float) and not isinstance(value, bool):
            if value < 1:
                return "1 이상이어야 한다"
        return None

    async def run(self, ctx: NodeContext, args: Mapping[str, Any]) -> ToolResult:
        http, sandbox = ctx.ports.http, ctx.ports.sandbox
        if http is None or sandbox is None:
            raise NodeFailure("http_unavailable", "HTTP 클라이언트나 런 작업 폴더가 없다")
        url = str(args.get("url") or "").strip()
        problem = self.check("url", url)
        if problem is not None:
            raise NodeFailure("invalid_url", problem)
        raw_limit = args.get("max_length")
        limit = int(raw_limit) if isinstance(raw_limit, int | float) else DEFAULT_MAX_LENGTH
        limit = max(1, limit)
        spec = HttpSpec(
            exec_id=f"fetch-{ctx.node_id}-{ctx.node_run_id.hex[-12:]}",
            method="GET",
            url=url,
            headers={"Accept": _ACCEPT},
            timeout_sec=FETCH_TIMEOUT_SEC,
        )
        try:
            handle = await sandbox.acquire(ctx.run_id)
            result = await http.request(spec, workspace_root=handle.root)
            if not is_textual(result.content_type):
                raise NodeFailure(
                    "unsupported_content_type",
                    f"글이 아닌 응답이다({result.content_type or '형식 없음'}). "
                    "http_request 노드로 받아 document_extractor로 읽는다",
                )
            if result.body_bytes > MAX_FETCH_BYTES:
                message = f"응답이 {MAX_FETCH_BYTES // 2**20}MB를 넘는다"
                raise NodeFailure("web_fetch_too_large", message)
            body = result.body or ""
            if result.saved_path is not None:
                content = await sandbox.read_file(handle, result.saved_path, 0, MAX_FETCH_BYTES)
                body = _decode(content, result.content_type)
        except SandboxError as exc:
            raise NodeFailure(
                exc.code, exc.message, retryable=exc.code in RETRYABLE_CODES
            ) from None
        title = ""
        if _media(result.content_type) in _HTML_TYPES:
            page = await asyncio.to_thread(html_to_text, body)
            title, body = page.title, page.text
        text, cut = _cut(body, limit)
        if cut:
            ctx.warn(f"text_truncated: 글이 길어 앞의 {len(text)}자만 담았다")
        if result.status >= 400:
            ctx.warn(f"http_status: 서버가 {result.status}로 답했다")
        return ToolResult(
            outputs={
                "text": text,
                "title": title,
                "url": result.url,
                "status_code": result.status,
                "content_type": result.content_type or "",
            },
            process={
                "status": result.status,
                "url": result.url,
                "redirects": result.redirects,
                "body_bytes": result.body_bytes,
                "truncated": cut,
            },
        )
