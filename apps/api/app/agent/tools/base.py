"""내장 도구의 공통 틀(설계 4.4, 4.6).

도구는 ToolSpec(이름·설명·JSON Schema)과 execute(ctx, input) -> ToolResult를 갖는다. 실행은 전부
SandboxBackend를 거치고 Path·subprocess·httpx를 직접 쓰지 않는다(8.4). 승인 판정은 도구가 아니라
interactions.py가 한다.
"""

import json
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, cast

from app.llm.types import ToolSpec
from app.sandbox.base import (
    ExecEvent,
    ExecExited,
    ExecOutput,
    ExecSpec,
    OutputDecoder,
    SandboxBackend,
    SandboxError,
    WorkspaceHandle,
)
from app.schemas.events import ToolCallStatus

if TYPE_CHECKING:
    from jsonschema.protocols import Validator

# system은 에이전트 설정으로 고르는 그룹이 아니다. 과업 모드의 submit_result처럼 러너가 붙인다.
ToolGroup = Literal["shell", "files", "http", "ask_user", "system"]
OutputStream = Literal["stdout", "stderr"]

# 모델에 보내는 도구 결과 상한과 그 안의 앞·뒤 몫(4.6).
MODEL_OUTPUT_BYTES = 32 * 1024
MODEL_HEAD_BYTES = 24 * 1024
MODEL_TAIL_BYTES = 8 * 1024
# 이만큼은 메모리에 두고 이벤트에 전문을 싣는다. 넘으면 전문을 storage의 tool-outputs로 흘려
# 쓴다(2.7).
EVENT_OUTPUT_BYTES = 256 * 1024
# 전체 출력 상한. 넘으면 프로세스를 끝낸다(4.6 output_limit).
OUTPUT_HARD_LIMIT_BYTES = 50 * 1024 * 1024
DEFAULT_HTTP_TIMEOUT_SEC = 60
MAX_HTTP_TIMEOUT_SEC = 300

# 모델에 보내는 스키마에 남기는 키. 입력 검증은 우리가 전체 스키마로 하므로(설계 T10) Gemini 호환
# 레이어가 받지 않을 수 있는 키(additionalProperties, minLength 등)는 보내지 않는다.
_MODEL_SCHEMA_KEYS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "enum",
        "items",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
    }
)


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _head(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _tail(text: str, limit: int) -> str:
    return text.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


def _omitted(head: str, tail: str, total_bytes: int) -> str:
    omitted = max(total_bytes - utf8_len(head) - utf8_len(tail), 0)
    return f"{head}\n[... {omitted} bytes omitted ...]\n{tail}"


def excerpt(text: str) -> str:
    """32KB를 넘으면 앞 24KB와 뒤 8KB만 남기고 가운데에 생략 표시를 넣는다(4.6)."""
    size = utf8_len(text)
    if size <= MODEL_OUTPUT_BYTES:
        return text
    return _omitted(_head(text, MODEL_HEAD_BYTES), _tail(text, MODEL_TAIL_BYTES), size)


def model_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    # 과업의 출력 스키마처럼 사용자가 쓴 스키마도 들어온다. true 같은 불리언 스키마는 비워 보낸다.
    cleaned: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _MODEL_SCHEMA_KEYS:
            continue
        if key == "properties":
            if not isinstance(value, Mapping):
                continue
            properties = cast(Mapping[str, Any], value)
            cleaned[key] = {
                name: model_schema(cast(Mapping[str, Any], sub)) if isinstance(sub, Mapping) else {}
                for name, sub in properties.items()
            }
        elif key == "items":
            if isinstance(value, Mapping):
                cleaned[key] = model_schema(cast(Mapping[str, Any], value))
        elif key == "type" and isinstance(value, list):
            # 함수 선언의 type은 하나만 받는다. null이 아닌 첫 타입을 보내고 나머지는 검증이 본다.
            types = [item for item in cast(list[Any], value) if item != "null"]
            if types:
                cleaned[key] = types[0]
        else:
            cleaned[key] = value
    return cleaned


@asynccontextmanager
async def closing[T](iterator: AsyncIterator[T]) -> AsyncGenerator[AsyncIterator[T]]:
    """async generator를 끝까지 돌지 않고 나가도 바로 닫는다(백엔드 exec, 12.2 M3)."""
    try:
        yield iterator
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


class OutputSink(Protocol):
    """256KB를 넘는 도구 출력 전문을 받는 곳. 루프 포트가 storage의 tool-outputs에 연다."""

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> str: ...

    async def abort(self) -> None: ...


SinkOpener = Callable[[], Awaitable[OutputSink]]


class OutputCapture:
    """도구 출력을 모은다. stdout·stderr를 도착 순서대로 섞어 하나로 본다(4.4 "합친 출력").

    256KB까지는 메모리에 두고, 넘으면 그때까지의 내용과 이후 출력을 sink에 흘려 쓴 뒤 메모리에는
    앞 24KB와 뒤 8KB만 남긴다. 저장에 실패해도 도구 실행은 계속한다(발췌만 남는다).
    """

    def __init__(self, open_sink: SinkOpener | None = None) -> None:
        self._open_sink = open_sink
        self._sink: OutputSink | None = None
        self._buffer: list[str] = []
        self._head: str | None = None
        self._tail: deque[str] = deque()
        self._tail_bytes = 0
        self.total_bytes = 0
        self.storage_key: str | None = None
        self.spill_failed = False

    @property
    def over_limit(self) -> bool:
        return self.total_bytes > OUTPUT_HARD_LIMIT_BYTES

    async def feed(self, text: str) -> None:
        if not text:
            return
        size = utf8_len(text)
        self.total_bytes += size
        if self._head is None:
            self._buffer.append(text)
            if self.total_bytes > EVENT_OUTPUT_BYTES:
                await self._spill()
            return
        self._push_tail(text, size)
        if not self.over_limit:
            await self._write(text.encode("utf-8"))

    async def _spill(self) -> None:
        whole = "".join(self._buffer)
        self._buffer = []
        self._head = _head(whole, MODEL_HEAD_BYTES)
        self._push_tail(whole, utf8_len(whole))
        if self._open_sink is None:
            return
        try:
            self._sink = await self._open_sink()
        except Exception:
            self.spill_failed = True
            return
        await self._write(whole.encode("utf-8"))

    async def _write(self, data: bytes) -> None:
        if self._sink is None:
            return
        try:
            await self._sink.write(data)
        except Exception:
            self.spill_failed = True
            sink, self._sink = self._sink, None
            await sink.abort()

    def _push_tail(self, text: str, size: int) -> None:
        self._tail.append(text)
        self._tail_bytes += size
        while (
            len(self._tail) > 1 and self._tail_bytes - utf8_len(self._tail[0]) >= MODEL_TAIL_BYTES
        ):
            self._tail_bytes -= utf8_len(self._tail.popleft())

    def model_text(self) -> str:
        if self._head is None:
            return excerpt("".join(self._buffer))
        return _omitted(self._head, _tail("".join(self._tail), MODEL_TAIL_BYTES), self.total_bytes)

    def event_text(self) -> str:
        # 256KB 이하면 전문이 run_event에 남고, 넘으면 모델에 준 발췌와 같다(4.6).
        if self._head is None:
            return "".join(self._buffer)
        return self.model_text()

    async def close(self) -> str | None:
        if self._sink is None:
            return self.storage_key
        sink, self._sink = self._sink, None
        try:
            self.storage_key = await sink.close()
        except Exception:
            self.spill_failed = True
            await sink.abort()
        return self.storage_key


@dataclass(frozen=True)
class ToolDefaults:
    # 에이전트 버전 limits.tool_timeout_sec. 모델이 timeout_sec를 주면 max까지 받는다(4.6).
    timeout_sec: int = 120
    max_timeout_sec: int = 900


@dataclass
class ToolContext:
    backend: SandboxBackend
    handle: WorkspaceHandle
    call_id: str
    env: Mapping[str, str]
    defaults: ToolDefaults
    capture: OutputCapture
    emit_output: Callable[[OutputStream, str], Awaitable[None]]
    memory_limit_mb: int | None = None
    max_processes: int | None = None


@dataclass
class ToolResult:
    status: ToolCallStatus
    # 모델에 보내는 내용. 32KB를 넘지 않게 만든다.
    content: str
    is_error: bool = False
    exit_code: int | None = None
    timed_out: bool = False
    peak_memory_bytes: int | None = None
    # 샌드박스 오류 코드(sandbox_unavailable, workspace_missing, path_outside_workspace 등)나
    # output_limit. 루프가 런 단위 실패 판정에 쓴다.
    error_code: str | None = None
    # 과업 모드의 submit_result처럼 런을 끝내는 도구의 결과(M8).
    output: Any = None
    # True면 결과를 모델에 남긴 뒤 런을 error_code로 실패시킨다(submit_result 검증 실패 한도 등).
    fatal: bool = False
    duration_ms: int = 0
    output_excerpt: str = ""
    output_bytes: int = 0
    output_storage_key: str | None = None

    @classmethod
    def ok(cls, content: str) -> "ToolResult":
        return cls(status="ok", content=excerpt(content))

    @classmethod
    def error(cls, content: str, *, code: str | None = None) -> "ToolResult":
        return cls(status="error", content=excerpt(content), is_error=True, error_code=code)

    @classmethod
    def from_sandbox_error(cls, exc: SandboxError) -> "ToolResult":
        return cls.error(f"{exc.code}: {exc.message}", code=exc.code)

    def with_status(self, status: ToolCallStatus) -> "ToolResult":
        return replace(self, status=status, is_error=self.is_error or status != "ok")


class Tool:
    name: ClassVar[str]
    group: ClassVar[ToolGroup]
    description: ClassVar[str]
    input_schema: ClassVar[dict[str, Any]]
    # 프로세스를 띄우는 도구. 중지할 때 백엔드 kill을 부른다.
    uses_process: ClassVar[bool] = False
    # 실행하지 않고 런을 waiting_input으로 멈추는 도구(ask_user).
    pauses: ClassVar[bool] = False
    # 성공하면 런을 끝내는 도구(과업 모드의 submit_result, M8).
    terminal: ClassVar[bool] = False

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=model_schema(self.input_schema),
        )

    def display_input(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """이벤트·승인 카드에 싣는 입력. 비밀이 들 수 있는 값은 가린다(S8)."""
        return dict(data)

    def workdir(self, data: Mapping[str, Any]) -> str | None:
        return None

    def timeout_for(self, data: Mapping[str, Any], defaults: ToolDefaults) -> int | None:
        return None

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        raise NotImplementedError


def clamp_timeout(requested: object, default: int, maximum: int) -> int:
    value = requested if isinstance(requested, int) and not isinstance(requested, bool) else default
    return max(1, min(value, maximum))


_validators: dict[str, "Validator"] = {}


def _validator(tool: Tool) -> "Validator":
    validator = _validators.get(tool.name)
    if validator is None:
        # jsonschema는 첫 도구 입력 검증 때 불러온다(2.7).
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(tool.input_schema)
        _validators[tool.name] = validator
    return validator


def validate_tool_input(tool: Tool, data: Mapping[str, Any]) -> str | None:
    """스키마에 맞지 않으면 모델에 돌려줄 오류 문장, 맞으면 None."""
    problems: list[str] = []
    for error in _validator(tool).iter_errors(dict(data)):
        location = "/".join(str(part) for part in error.absolute_path) or "(입력)"
        problems.append(f"{location}: {error.message}")
        if len(problems) >= 5:
            break
    if not problems:
        return None
    return "INVALID_INPUT: " + "; ".join(sorted(problems))


def parse_arguments(arguments: str) -> tuple[dict[str, Any], str | None]:
    """모델이 낸 인자 JSON. 실패하면 ({}, 오류 문장)."""
    if not arguments.strip():
        return {}, None
    try:
        parsed: object = json.loads(arguments)
    except ValueError as exc:
        return {}, f"INVALID_JSON: 도구 인자가 올바른 JSON이 아니다({exc})"
    if not isinstance(parsed, dict):
        return {}, "INVALID_JSON: 도구 인자는 JSON 객체여야 한다"
    return cast(dict[str, Any], parsed), None


async def run_process(ctx: ToolContext, spec: ExecSpec) -> ToolResult:
    """셸·파이썬·pip 공통 실행. 출력을 이벤트로 흘리고 종료 정보를 결과로 만든다."""
    decoders: dict[OutputStream, OutputDecoder] = {
        "stdout": OutputDecoder(),
        "stderr": OutputDecoder(),
    }
    exited: ExecExited | None = None
    limit_hit = False

    async def deliver(stream: OutputStream, text: str) -> None:
        nonlocal limit_hit
        if not text:
            return
        await ctx.capture.feed(text)
        await ctx.emit_output(stream, text)
        if ctx.capture.over_limit and not limit_hit:
            limit_hit = True
            await ctx.backend.kill(ctx.handle, spec.exec_id)

    stream: AsyncIterator[ExecEvent] = ctx.backend.exec(ctx.handle, spec)
    try:
        async with closing(stream) as events:
            async for event in events:
                if isinstance(event, ExecOutput):
                    await deliver(event.stream, decoders[event.stream].feed(event.data))
                elif isinstance(event, ExecExited):
                    exited = event
    except SandboxError as exc:
        return ToolResult.from_sandbox_error(exc)
    for name, decoder in decoders.items():
        await deliver(name, decoder.flush())
    return process_result(ctx.capture, exited, limit_hit=limit_hit, timeout_sec=spec.timeout_sec)


def process_result(
    capture: OutputCapture, exited: ExecExited | None, *, limit_hit: bool, timeout_sec: int
) -> ToolResult:
    status: ToolCallStatus = "error"
    code: str | None = None
    if exited is None:
        head = "[프로세스 종료 정보를 받지 못했다]"
    elif limit_hit:
        head = f"[출력이 {OUTPUT_HARD_LIMIT_BYTES // 2**20}MB를 넘어 프로세스를 중지했다]"
        code = "output_limit"
    elif exited.timed_out:
        status = "timeout"
        head = f"[시간 초과: {timeout_sec}초가 지나 프로세스를 중지했다]"
    elif exited.killed:
        head = "[프로세스가 중지되었다]"
    elif exited.code == 0:
        status = "ok"
        head = "[종료 코드 0]"
    else:
        head = f"[종료 코드 {exited.code}]"
    body = capture.model_text() if capture.total_bytes else "(출력 없음)"
    return ToolResult(
        status=status,
        content=f"{head}\n{body}",
        is_error=status != "ok",
        exit_code=exited.code if exited is not None else None,
        timed_out=exited.timed_out if exited is not None else False,
        peak_memory_bytes=exited.peak_memory_bytes if exited is not None else None,
        error_code=code,
    )
