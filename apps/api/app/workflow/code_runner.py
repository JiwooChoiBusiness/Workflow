# pyright: strict
"""code 노드의 샌드박스 실행(설계 9.1).

런 작업 폴더(DATA_DIR/wf-workspaces/<run_id>, 두 번째 로컬 백엔드)에서 백엔드 exec로 래퍼 스크립트를
1단계 파이썬 실행과 같은 방식(.run/<exec_id>.py + python -I -X utf8 + 부트스트랩)으로 돌린다. 래퍼는
main(**inputs)를 불러 결과를 .run/<exec_id>.output.json에 JSON으로 쓰고, 여기서 그 파일을 읽는다.

사용자 코드와 입력은 따로 파일로 두지 않고 래퍼 소스에 문자열로 넣는다. 백엔드 write_file은
.run을 플랫폼 영역으로 막고(1단계 3.5), 샌드박스 프로토콜은 바꾸지 않기로 했기 때문이다. 래퍼 파일은
실행 뒤 백엔드가 지운다(KEEP_RUN_SCRIPTS=false). 출력 파일은 exec_id마다 이름이 달라 반복이나
재시도에서 지난 결과를 읽지 않으며, 런 폴더와 함께 지워진다(4.4).
"""

import json
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

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

MEMORY_LIMIT_MB = 256
MAX_PROCESSES = 8
MAX_RESULT_BYTES = 256 * 1024
STDIO_EXCERPT_BYTES = 32 * 1024
# print가 끝없이 도는 코드를 시간 상한까지 기다리지 않고 멈춘다.
STDIO_HARD_LIMIT_BYTES = 1024 * 1024
RUN_DIR = ".run"

ErrorKind = Literal[
    "exception", "invalid_output", "output_too_large", "memory_limit", "main_missing", "syntax"
]
_KINDS: dict[str, ErrorKind] = {
    kind: kind
    for kind in (
        "exception",
        "invalid_output",
        "output_too_large",
        "memory_limit",
        "main_missing",
        "syntax",
    )
}

# compile 파일 이름을 node_<id>.py로 두고 linecache에 소스를 넣어 traceback의 행 번호와 내용이
# 사용자가 쓴 코드와 맞게 한다. 래퍼 프레임은 traceback에서 뺀다.
WRAPPER_TEMPLATE = """\
# 플랫폼이 만든 code 노드 래퍼다. main(**inputs)를 부르고 결과를 JSON 파일로 남긴다.
import json
import linecache
import os
import traceback

_NAME = {name!r}
_SOURCE = {source!r}
_INPUTS = {inputs!r}
_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), {output!r})
_LIMIT = {limit}


def _write(text):
    with open(_OUTPUT, "w", encoding="utf-8") as file:
        file.write(text)


def _error(kind, message, trace=""):
    payload = {{"ok": False, "error": {{"kind": kind, "message": message, "traceback": trace}}}}
    _write(json.dumps(payload, ensure_ascii=False))


def _user_trace(exc):
    tb = exc.__traceback__
    while tb is not None and tb.tb_frame.f_code.co_filename != _NAME:
        tb = tb.tb_next
    return "".join(traceback.format_exception(type(exc), exc, tb))


def _main():
    inputs = json.loads(_INPUTS)
    linecache.cache[_NAME] = (len(_SOURCE), None, _SOURCE.splitlines(True), _NAME)
    try:
        compiled = compile(_SOURCE, _NAME, "exec")
    except SyntaxError as exc:
        _error("syntax", "%s행: %s" % (exc.lineno, exc.msg))
        return
    namespace = {{"__name__": "__workflow_code__"}}
    exec(compiled, namespace)
    main = namespace.get("main")
    if not callable(main):
        _error("main_missing", "main 함수가 없다")
        return
    result = main(**inputs)
    if not isinstance(result, dict):
        kind = type(result).__name__
        _error("invalid_output", "main()은 dict를 돌려줘야 하는데 %s을(를) 돌려줬다" % kind)
        return
    try:
        text = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        _error("invalid_output", "main()이 돌려준 dict를 JSON으로 바꾸지 못했다: %s" % exc)
        return
    if len(text.encode("utf-8")) > _LIMIT:
        _error("output_too_large", "main()이 돌려준 값이 %dKB를 넘는다" % (_LIMIT // 1024))
        return
    _write('{{"ok": true, "result": ' + text + "}}")


try:
    _main()
except MemoryError:
    _error("memory_limit", "메모리 상한을 넘었다")
except BaseException as exc:
    _error("exception", "%s: %s" % (type(exc).__name__, exc), _user_trace(exc))
"""


def code_name(node_id: str) -> str:
    return f"node_{node_id}.py"


def output_name(exec_id: str) -> str:
    return f"{exec_id}.output.json"


def wrapper_source(
    *,
    node_id: str,
    exec_id: str,
    code: str,
    inputs: Mapping[str, Any],
    limit: int = MAX_RESULT_BYTES,
) -> str:
    return WRAPPER_TEMPLATE.format(
        name=code_name(node_id),
        source=code,
        inputs=json.dumps(dict(inputs), ensure_ascii=False, default=str),
        output=output_name(exec_id),
        limit=limit,
    )


@dataclass(frozen=True, slots=True)
class CodeError:
    kind: ErrorKind
    message: str
    traceback: str = ""


@dataclass
class CodeRun:
    result: dict[str, Any] | None = None
    error: CodeError | None = None
    exit_code: int | None = None
    timed_out: bool = False
    killed: bool = False
    # print 출력이 STDIO_HARD_LIMIT_BYTES를 넘어 멈췄다.
    stdio_limit: bool = False
    peak_memory_bytes: int | None = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    stdio_bytes: int = 0


class _Excerpt:
    """앞 STDIO_EXCERPT_BYTES만 남긴다. 조각 경계에서 잘린 UTF-8은 디코더가 잇는다."""

    def __init__(self) -> None:
        self._decoder = OutputDecoder()
        self._parts: list[str] = []
        self._size = 0

    def feed(self, data: bytes) -> None:
        if self._size >= STDIO_EXCERPT_BYTES:
            return
        text = self._decoder.feed(data)
        self._parts.append(text)
        self._size += len(text.encode("utf-8"))

    def text(self) -> str:
        if self._size < STDIO_EXCERPT_BYTES:
            self._parts.append(self._decoder.flush())
        joined = "".join(self._parts)
        data = joined.encode("utf-8")
        if len(data) <= STDIO_EXCERPT_BYTES:
            return joined
        return data[:STDIO_EXCERPT_BYTES].decode("utf-8", errors="ignore")


async def _close(events: AsyncIterator[ExecEvent]) -> None:
    # 끝까지 돌지 않고 나가면(취소) 백엔드 exec의 finally가 프로세스 트리를 바로 끝내게 한다.
    if isinstance(events, AsyncGenerator):
        await cast(AsyncGenerator[ExecEvent, None], events).aclose()


def _parse_output(text: str) -> tuple[dict[str, Any] | None, CodeError | None]:
    try:
        payload: object = json.loads(text)
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    data = cast(dict[str, Any], payload)
    if data.get("ok") is True and isinstance(data.get("result"), dict):
        return cast(dict[str, Any], data["result"]), None
    error = data.get("error")
    if isinstance(error, dict):
        info = cast(dict[str, Any], error)
        return None, CodeError(
            kind=_KINDS.get(str(info.get("kind")), "exception"),
            message=str(info.get("message") or ""),
            traceback=str(info.get("traceback") or ""),
        )
    return None, None


async def run_code(
    sandbox: SandboxBackend,
    handle: WorkspaceHandle,
    *,
    node_id: str,
    exec_id: str,
    code: str,
    inputs: Mapping[str, Any],
    timeout_sec: int,
    memory_limit_mb: int = MEMORY_LIMIT_MB,
    max_processes: int = MAX_PROCESSES,
) -> CodeRun:
    """코드를 한 번 돌린다. 샌드박스 오류(작업 폴더 없음 등)는 SandboxError로 그대로 나간다."""
    spec = ExecSpec(
        exec_id=exec_id,
        kind="python",
        script=wrapper_source(node_id=node_id, exec_id=exec_id, code=code, inputs=inputs),
        timeout_sec=timeout_sec,
        memory_limit_mb=memory_limit_mb,
        max_processes=max_processes,
    )
    run = CodeRun()
    stdout, stderr = _Excerpt(), _Excerpt()
    exited: ExecExited | None = None
    events = sandbox.exec(handle, spec)
    try:
        async for event in events:
            if isinstance(event, ExecOutput):
                run.stdio_bytes += len(event.data)
                (stdout if event.stream == "stdout" else stderr).feed(event.data)
                if run.stdio_bytes > STDIO_HARD_LIMIT_BYTES and not run.stdio_limit:
                    run.stdio_limit = True
                    await sandbox.kill(handle, exec_id)
            elif isinstance(event, ExecExited):
                exited = event
    finally:
        await _close(events)
    run.stdout, run.stderr = stdout.text(), stderr.text()
    if exited is not None:
        run.exit_code = exited.code
        run.timed_out = exited.timed_out
        run.killed = exited.killed
        run.peak_memory_bytes = exited.peak_memory_bytes
        run.duration_ms = exited.duration_ms
    try:
        content = await sandbox.read_file(
            handle, f"{RUN_DIR}/{output_name(exec_id)}", 0, MAX_RESULT_BYTES + 4096
        )
    except SandboxError:
        return run
    if content.encoding == "utf-8" and not content.truncated and content.content:
        run.result, run.error = _parse_output(content.content)
    return run
