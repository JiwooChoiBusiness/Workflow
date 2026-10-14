# pyright: strict
"""http_request 노드(설계 2.3, 8, 9.2).

요청은 1단계 HttpClient(허용 목록, 사설·링크 로컬·메타데이터 주소 차단, 리다이렉트 재검사, 본문
상한)로만 나간다. 큰 응답은 HttpClient가 런 작업 폴더(DATA_DIR/wf-workspaces/<run_id>/downloads)에
내려받는다. body 변수에는 텍스트 응답을 1MB까지만 담고, 그보다 크거나 바이너리인 응답은
wf_file(kind=output)로 옮겨 files에 넣는다. 4xx·5xx는 오류가 아니라 출력이고, 연결 오류·시간
초과만 재시도할 수 있는 오류다.

텍스트 필드(URL, 헤더·쿼리·폼 값, 본문, 인증 값)는 템플릿이다. json 본문에 글 변수를 넣을 때는
따옴표가 깨지지 않게 tojson 필터를 쓴다({"q": {{ start.q | tojson }}}). 노드 런 기록의 요청 헤더는
Authorization·Cookie·X-Api-Key와 인증 헤더 값을 가린다.
"""

import base64
import json
import re
from email.message import Message
from pathlib import PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

from app.sandbox.base import (
    FileContent,
    HttpResult,
    HttpSpec,
    SandboxBackend,
    SandboxError,
    WorkspaceHandle,
)
from app.sandbox.http_client import MASK, REQUEST_BODY_LIMIT, is_textual, mask_headers
from app.schemas.events import NodeError
from app.schemas.workflow import Issue
from app.workflow.nodes.base import (
    ALL_STRATEGIES,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    TemplateField,
)
from app.workflow.ports import FilePortError
from app.workflow.variables import MAX_STRING_BYTES, VariableError, measure

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SEC = 60
MAX_TIMEOUT_SEC = 300
# HttpClient가 자기 시간 상한으로 끝내므로 엔진 시간 상한은 그보다 조금 길게 둔다.
TIMEOUT_GRACE_SEC = 5
# 연결 오류와 시간 초과만 재시도한다(2.2).
RETRYABLE_CODES = frozenset({"http_timeout", "http_failed"})
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$")


class KeyValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    # 템플릿(3.3).
    value: str = ""


class HttpBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["none", "json", "form", "raw", "file"] = "none"
    # json·raw는 템플릿 글, form은 이름·값 목록(값은 템플릿, x-www-form-urlencoded로 보낸다).
    content: str | list[KeyValue] = ""


class HttpAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["none", "bearer", "basic", "header"] = "none"
    # 템플릿. {{ env.API_TOKEN }}처럼 비밀 환경 변수를 쓰기를 권한다. basic은 "사용자:비밀번호".
    value: str = ""
    # type=header일 때 값을 실어 보낼 헤더 이름.
    header: str = ""


class HttpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = ""
    headers: list[KeyValue] = Field(default_factory=list[KeyValue])
    params: list[KeyValue] = Field(default_factory=list[KeyValue])
    body: HttpBody = Field(default_factory=HttpBody)
    auth: HttpAuth = Field(default_factory=HttpAuth)
    timeout_sec: int = Field(default=DEFAULT_TIMEOUT_SEC, ge=1, le=MAX_TIMEOUT_SEC)
    max_body_bytes: int = Field(default=MAX_RESPONSE_BYTES, ge=1, le=MAX_RESPONSE_BYTES)


def _error(code: str, message: str, field: str) -> Issue:
    return Issue(level="error", code=code, message=message, field=field)


def _with_params(url: str, params: list[tuple[str, str]]) -> str:
    if not params:
        return url
    parts = urlsplit(url)
    extra = urlencode(params)
    query = f"{parts.query}&{extra}" if parts.query else extra
    return urlunsplit(parts._replace(query=query))


def _charset(content_type: str | None) -> str:
    match = re.search(r"charset=\"?([A-Za-z0-9._-]+)", content_type or "", re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _cut(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def _decode(content: FileContent, content_type: str | None) -> str:
    if content.encoding == "utf-8":
        return content.content
    # 샌드박스가 UTF-8이 아니라고 본 글(EUC-KR 등)은 base64로 온다. 응답의 charset으로 푼다.
    data = base64.b64decode(content.content)
    try:
        return data.decode(_charset(content_type), errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _filename(result: HttpResult, node_id: str) -> str:
    disposition = next(
        (value for name, value in result.headers.items() if name.lower() == "content-disposition"),
        None,
    )
    if disposition:
        message = Message()
        message["content-disposition"] = disposition
        found = message.get_filename()
        if found:
            return found
    # 경로 끝 이름에 확장자가 없으면(/pypi/numpy/json) 내려받을 때 content-type으로 붙인 것을 쓴다.
    suffix = PurePosixPath(result.saved_path or "").suffix
    last = unquote(PurePosixPath(urlsplit(result.url).path).name)
    if last:
        return last if PurePosixPath(last).suffix else f"{last}{suffix}"
    return f"{node_id}{suffix}"


def _media_type(content_type: str | None) -> str | None:
    media = (content_type or "").split(";", 1)[0].strip().lower()
    return media or None


class HttpRequestRunner(NodeRunner[HttpConfig]):
    type = "http_request"
    Config = HttpConfig
    error_strategies = ALL_STRATEGIES
    # 노드 런 기록은 64KB 미리보기라 앞의 값부터 담는다. 1MB까지 커지는 body·json을 뒤에 두어
    # files가 잘려 나가지 않게 한다.
    fixed_outputs = (
        OutputSpec("status_code", "number"),
        OutputSpec("headers", "object"),
        OutputSpec("files", "array[file]"),
        OutputSpec("body", "string"),
        OutputSpec("json", "object"),
    )

    def template_fields(self, config: HttpConfig) -> list[TemplateField]:
        fields = [TemplateField("url", config.url), TemplateField("auth.value", config.auth.value)]
        fields += [
            TemplateField(f"headers[{i}].value", item.value)
            for i, item in enumerate(config.headers)
        ]
        fields += [
            TemplateField(f"params[{i}].value", item.value) for i, item in enumerate(config.params)
        ]
        content = config.body.content
        if isinstance(content, str):
            fields.append(TemplateField("body.content", content))
        else:
            fields += [
                TemplateField(f"body.content[{i}].value", item.value)
                for i, item in enumerate(content)
            ]
        return fields

    def validate(self, config: HttpConfig, graph: GraphView) -> list[Issue]:
        issues: list[Issue] = []
        if not config.url.strip():
            issues.append(_error("invalid_config", "URL이 비어 있다", "url"))
        for index, item in enumerate(config.headers):
            if not _HEADER_NAME.fullmatch(item.name):
                message = "헤더 이름에 쓸 수 없는 글자가 있다"
                issues.append(_error("invalid_config", message, f"headers[{index}].name"))
        body = config.body
        if body.type == "file":
            message = "파일 본문은 아직 보낼 수 없다. raw나 json으로 보낸다"
            issues.append(_error("unsupported_body_type", message, "body.type"))
        elif body.type == "form" and isinstance(body.content, str) and body.content:
            issues.append(_error("invalid_config", "form 본문은 이름·값 목록이다", "body.content"))
        elif body.type in ("json", "raw") and not isinstance(body.content, str):
            issues.append(_error("invalid_config", f"{body.type} 본문은 글이다", "body.content"))
        auth = config.auth
        if auth.type == "header" and not _HEADER_NAME.fullmatch(auth.header):
            issues.append(_error("invalid_config", "인증 헤더 이름이 필요하다", "auth.header"))
        return issues

    def timeout_sec(self, config: HttpConfig) -> float | None:
        return config.timeout_sec + TIMEOUT_GRACE_SEC

    async def run(
        self, ctx: NodeContext, config: HttpConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        http, sandbox = ctx.ports.http, ctx.ports.sandbox
        if http is None or sandbox is None:
            raise NodeFailure("http_unavailable", "HTTP 클라이언트나 런 작업 폴더가 없다")
        url = _with_params(
            ctx.render(config.url).strip(),
            [(item.name, ctx.render(item.value)) for item in config.params],
        )
        headers = {item.name: ctx.render(item.value) for item in config.headers}
        hidden = self._authorize(ctx, config.auth, headers)
        body, content_type = self._body(ctx, config.body)
        if content_type and not any(name.lower() == "content-type" for name in headers):
            headers["Content-Type"] = content_type
        shown_headers = {
            name: MASK if name.lower() in hidden else value
            for name, value in mask_headers(headers).items()
        }
        inputs: dict[str, Any] = {
            "method": config.method,
            "url": url,
            "headers": shown_headers,
            "body": body,
        }
        if body is not None and len(body.encode("utf-8")) > REQUEST_BODY_LIMIT:
            message = f"요청 본문은 {REQUEST_BODY_LIMIT}바이트까지다"
            return _failed("http_request_too_large", message, inputs)
        spec = HttpSpec(
            exec_id=f"{ctx.node_id}-{ctx.node_run_id.hex[-12:]}",
            method=config.method,
            url=url,
            headers=headers,
            body=body,
            timeout_sec=config.timeout_sec,
        )
        try:
            handle = await sandbox.acquire(ctx.run_id)
            result = await http.request(spec, workspace_root=handle.root)
            if result.body_bytes > config.max_body_bytes:
                message = f"응답 본문이 노드 상한({config.max_body_bytes}바이트)을 넘는다"
                return _failed("http_response_too_large", message, inputs)
            return await self._outputs(ctx, sandbox, handle, result, inputs)
        except SandboxError as exc:
            return _failed(exc.code, exc.message, inputs, retryable=exc.code in RETRYABLE_CODES)

    def _authorize(self, ctx: NodeContext, auth: HttpAuth, headers: dict[str, str]) -> set[str]:
        """인증 헤더를 넣고 기록에서 가릴 헤더 이름(소문자)을 돌려준다."""
        if auth.type == "none":
            return set()
        value = ctx.render(auth.value)
        if auth.type == "bearer":
            headers["Authorization"] = f"Bearer {value}"
            return {"authorization"}
        if auth.type == "basic":
            token = base64.b64encode(value.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
            return {"authorization"}
        headers[auth.header] = value
        return {auth.header.lower()}

    def _body(self, ctx: NodeContext, body: HttpBody) -> tuple[str | None, str | None]:
        if body.type == "none":
            return None, None
        if body.type == "form":
            items = body.content if isinstance(body.content, list) else []
            pairs = [(item.name, ctx.render(item.value)) for item in items]
            return urlencode(pairs), "application/x-www-form-urlencoded"
        if body.type == "file":
            raise NodeFailure("unsupported_body_type", "파일 본문은 아직 보낼 수 없다")
        text = ctx.render(body.content if isinstance(body.content, str) else "")
        if body.type == "raw":
            return text, "text/plain; charset=utf-8"
        try:
            json.loads(text)
        except ValueError as exc:
            raise NodeFailure("invalid_body", f"본문이 JSON이 아니다: {exc}") from None
        return text, "application/json"

    async def _outputs(
        self,
        ctx: NodeContext,
        sandbox: SandboxBackend,
        handle: WorkspaceHandle,
        result: HttpResult,
        inputs: dict[str, Any],
    ) -> NodeResult:
        textual = is_textual(result.content_type)
        body = ""
        files: list[dict[str, Any]] = []
        cut = False
        if result.saved_path is None:
            body = result.body or ""
        elif textual:
            content = await sandbox.read_file(handle, result.saved_path, 0, MAX_STRING_BYTES)
            body = _cut(_decode(content, result.content_type), MAX_STRING_BYTES)
            cut = result.body_bytes > MAX_STRING_BYTES
        if result.saved_path is not None and (cut or not textual):
            saved = await self._save(ctx, handle, result)
            if saved is not None:
                files.append(saved)
            if cut:
                ctx.warn("body_truncated: 응답이 1MB를 넘어 body에는 앞부분만 담았다")
        value = self._json(ctx, body, result.content_type) if textual and not cut else None
        return NodeResult(
            status="succeeded",
            outputs={
                "status_code": result.status,
                "headers": result.headers,
                "files": files,
                "body": body,
                "json": value,
            },
            inputs=inputs,
            process={
                "status": result.status,
                "url": result.url,
                "redirects": result.redirects,
                "content_type": result.content_type,
                "body_bytes": result.body_bytes,
                "saved_path": result.saved_path,
                "body_truncated": cut,
                "response_headers": mask_headers(result.headers),
            },
        )

    async def _save(
        self, ctx: NodeContext, handle: WorkspaceHandle, result: HttpResult
    ) -> dict[str, Any] | None:
        files = ctx.ports.files
        if files is None or result.saved_path is None:
            ctx.warn(f"files_unavailable: 응답 파일을 남기지 못했다({result.saved_path})")
            return None
        try:
            return await files.save_output(
                handle.root / result.saved_path,
                filename=_filename(result, ctx.node_id),
                mime=_media_type(result.content_type),
            )
        except FilePortError as exc:
            raise NodeFailure(exc.code, exc.message) from None

    def _json(self, ctx: NodeContext, body: str, content_type: str | None) -> dict[str, Any] | None:
        media = _media_type(content_type) or ""
        declared = media == "application/json" or media.endswith("+json")
        if not declared and not body.lstrip().startswith("{"):
            return None
        try:
            value: object = json.loads(body)
        except ValueError:
            if declared:
                ctx.warn("json_invalid: 응답이 JSON이라고 했지만 읽지 못했다")
            return None
        if not isinstance(value, dict):
            ctx.warn("json_not_object: 응답 JSON의 최상위가 객체가 아니라 json은 null이다")
            return None
        try:
            measure(value)
        except VariableError as exc:
            ctx.warn(f"json_too_large: {exc.message}")
            return None
        return cast(dict[str, Any], value)


def _failed(
    code: str, message: str, inputs: dict[str, Any], *, retryable: bool = False
) -> NodeResult:
    return NodeResult(
        status="failed",
        error=NodeError(code=code, message=message, retryable=retryable),
        inputs=inputs,
    )
