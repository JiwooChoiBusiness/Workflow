# pyright: strict
"""document_extractor의 pdf·docx 추출(2단계 설계 2.4, 8장, 11.1 Q8).

pypdf·python-docx는 서버 의존성에 넣지 않고 샌드박스 파이썬(두 번째 로컬 백엔드의 런 작업 폴더)에서
돌린다. 서버 프로세스는 파서를 불러오지 않고 결과 JSON만 읽으므로 추출해도 RSS가 늘지 않는다.

파서는 처음 쓸 때 설치한다. 런 작업 폴더에서 백엔드의 pip exec(1단계와 같은 pip --target 경로,
pip 캐시, Job Object)로 받은 .packages를 DATA_DIR/cache/wf-extract/<스펙 해시>/로 옮겨 두고, 그 뒤
추출 스크립트는 그 폴더를 sys.path에 붙여 쓴다. 같은 프로세스 안의 동시 설치는 잠금으로 하나만
돌고, 옮기기를 마친 폴더에만 표시 파일을 둬서 받다 만 폴더를 쓰지 않는다.
"""

import asyncio
import hashlib
import json
import os
import shutil
import uuid
import weakref
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from pathlib import Path
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

DocumentKind = Literal["pdf", "docx"]
# 버전을 바꾸면 해시가 바뀌어 새 폴더에 다시 설치한다.
PARSER_SPECS: tuple[str, ...] = ("pypdf>=6,<7", "python-docx>=1.1,<2")
INSTALL_TIMEOUT_SEC = 300
EXTRACT_TIMEOUT_SEC = 60
INSTALL_MEMORY_MB = 1024
EXTRACT_MEMORY_MB = 512
EXTRACT_MAX_PROCESSES = 4
READY_MARKER = ".installed"
# 오프라인에서 pip이 재시도를 거듭하며 오래 붙잡지 않게 한다. pip은 -I에서도 PIP_* 환경 변수를
# 읽는다.
PIP_ENV = {"PIP_RETRIES": "1", "PIP_DEFAULT_TIMEOUT": "20"}
STDERR_TAIL_CHARS = 600
RUN_DIR = ".run"

EXTRACT_TEMPLATE = """\
# 플랫폼이 만든 문서 추출 스크립트다. pdf·docx의 글을 꺼내 JSON 파일로 남긴다.
import json
import os
import site

site.addsitedir({lib!r})
_PATH = {path!r}
_KIND = {kind!r}
_BUDGET = {budget}
_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), {output!r})


def _pdf(path):
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("암호가 걸린 PDF다")
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\\n\\n".join(text.strip() for text in pages if text.strip()), len(pages)


def _cells(row):
    return [" ".join(cell.text.split()) for cell in row.cells]


def _docx(path):
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(path)
    parts = []
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}}", 1)[-1]
        if tag == "p":
            parts.append(Paragraph(child, document).text)
        elif tag == "tbl":
            rows = [_cells(row) for row in Table(child, document).rows]
            if rows:
                lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * len(rows[0])]
                lines += ["| " + " | ".join(row) + " |" for row in rows[1:]]
                parts.append("\\n".join(lines))
    return "\\n".join(parts).strip(), None


def _write(payload):
    with open(_OUTPUT, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False)


try:
    text, pages = (_pdf if _KIND == "pdf" else _docx)(_PATH)
    data = text.encode("utf-8")
    cut = len(data) > _BUDGET
    if cut:
        text = data[:_BUDGET].decode("utf-8", "ignore")
    _write({{"ok": True, "text": text, "pages": pages, "truncated": cut}})
except MemoryError:
    _write({{"ok": False, "kind": "memory_limit", "error": "메모리 상한을 넘었다"}})
except ImportError as exc:
    _write({{"ok": False, "kind": "parser_missing", "error": "%s: %s" % (type(exc).__name__, exc)}})
except Exception as exc:
    _write({{"ok": False, "kind": "parse", "error": "%s: %s" % (type(exc).__name__, exc)}})
"""


class ExtractError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Extracted:
    text: str
    truncated: bool
    pages: int | None = None


@dataclass
class _ExecRun:
    exit_code: int | None = None
    timed_out: bool = False
    killed: bool = False
    stderr: str = ""


# 이벤트 루프마다 잠금을 따로 둔다. 시험은 루프를 바꿔 가며 돌고 잠금은 만든 루프에 묶인다.
_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _install_lock(target: Path) -> asyncio.Lock:
    per_loop = _locks.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault(str(target), asyncio.Lock())


def parser_dir(cache_dir: Path) -> Path:
    digest = hashlib.sha256("\n".join(PARSER_SPECS).encode("utf-8")).hexdigest()[:12]
    return cache_dir / f"parsers-{digest}"


def _ready(target: Path) -> bool:
    return (target / READY_MARKER).is_file()


def _clear(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _promote(staging: Path, target: Path) -> None:
    """런 폴더에 받은 패키지를 캐시로 옮긴다. 다 옮긴 뒤에만 표시 파일이 생긴다."""
    if not staging.is_dir():
        raise ExtractError("parser_install_failed", "pip이 끝났는데 받은 패키지가 없다")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.partial-{uuid.uuid4().hex[:8]}")
    shutil.move(str(staging), str(partial))
    (partial / READY_MARKER).write_text("\n".join(PARSER_SPECS) + "\n", encoding="utf-8")
    # 표시 없이 남은 폴더(옮기다 죽은 흔적)는 지우고 바꾼다.
    _clear(target)
    os.replace(partial, target)


async def _close(events: AsyncIterator[ExecEvent]) -> None:
    if isinstance(events, AsyncGenerator):
        await cast(AsyncGenerator[ExecEvent, None], events).aclose()


async def _run(sandbox: SandboxBackend, handle: WorkspaceHandle, spec: ExecSpec) -> _ExecRun:
    run = _ExecRun()
    decoder = OutputDecoder()
    stderr: list[str] = []
    events = sandbox.exec(handle, spec)
    try:
        async for event in events:
            if isinstance(event, ExecOutput) and event.stream == "stderr":
                stderr.append(decoder.feed(event.data))
                # 끝부분만 쓰므로 오래 쌓지 않는다.
                if sum(len(part) for part in stderr) > STDERR_TAIL_CHARS * 4:
                    stderr = ["".join(stderr)[-STDERR_TAIL_CHARS * 2 :]]
            elif isinstance(event, ExecExited):
                run.exit_code = event.code
                run.timed_out = event.timed_out
                run.killed = event.killed
    finally:
        await _close(events)
    stderr.append(decoder.flush())
    run.stderr = "".join(stderr).strip()[-STDERR_TAIL_CHARS:]
    return run


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


async def ensure_parsers(
    sandbox: SandboxBackend, handle: WorkspaceHandle, cache_dir: Path, *, exec_id: str
) -> Path:
    """pypdf·python-docx가 설치된 폴더. 없으면 이 런 폴더에서 받아 캐시로 옮긴다."""
    target = parser_dir(cache_dir)
    if await asyncio.to_thread(_ready, target):
        return target
    async with _install_lock(target):
        if await asyncio.to_thread(_ready, target):
            return target
        staging = handle.root / ".packages"
        await asyncio.to_thread(_clear, staging)
        spec = ExecSpec(
            exec_id=exec_id,
            kind="pip",
            packages=list(PARSER_SPECS),
            env=dict(PIP_ENV),
            timeout_sec=INSTALL_TIMEOUT_SEC,
            memory_limit_mb=INSTALL_MEMORY_MB,
        )
        try:
            run = await _run(sandbox, handle, spec)
        except SandboxError as exc:
            raise ExtractError("parser_install_failed", exc.message, retryable=True) from None
        if run.timed_out or run.exit_code != 0:
            reason = (
                f"{INSTALL_TIMEOUT_SEC}초 안에 끝나지 않았다"
                if run.timed_out
                else _last_line(run.stderr) or f"pip 종료 코드 {run.exit_code}"
            )
            await asyncio.to_thread(_clear, staging)
            raise ExtractError(
                "parser_install_failed",
                f"pdf·docx 파서({', '.join(PARSER_SPECS)})를 설치하지 못했다. 인터넷 연결과 "
                f"DATA_DIR 경로 길이(Windows 260자)를 확인한다: {reason}",
                retryable=True,
            )
        await asyncio.to_thread(_promote, staging, target)
    return target


def _parse_output(raw: str) -> dict[str, Any] | None:
    try:
        payload: object = json.loads(raw)
    except ValueError:
        return None
    return cast(dict[str, Any], payload) if isinstance(payload, dict) else None


async def extract_document(
    sandbox: SandboxBackend,
    handle: WorkspaceHandle,
    *,
    lib: Path,
    path: str,
    kind: DocumentKind,
    budget: int,
    exec_id: str,
) -> Extracted:
    """런 폴더 안의 path(작업공간 상대 경로) 파일에서 글을 꺼낸다. 결과는 budget 바이트까지다."""
    output = f"{exec_id}.extract.json"
    script = EXTRACT_TEMPLATE.format(
        lib=str(lib), path=path, kind=kind, budget=max(budget, 0), output=output
    )
    spec = ExecSpec(
        exec_id=exec_id,
        kind="python",
        script=script,
        timeout_sec=EXTRACT_TIMEOUT_SEC,
        memory_limit_mb=EXTRACT_MEMORY_MB,
        max_processes=EXTRACT_MAX_PROCESSES,
    )
    try:
        run = await _run(sandbox, handle, spec)
    except SandboxError as exc:
        raise ExtractError(exc.code, exc.message) from None
    if run.timed_out:
        raise ExtractError(
            "extract_timeout", f"{EXTRACT_TIMEOUT_SEC}초 안에 글을 꺼내지 못했다", retryable=True
        )
    payload: dict[str, Any] | None = None
    try:
        # JSON으로 바꾸면 줄바꿈·따옴표가 두 글자가 되어 글보다 커질 수 있다.
        limit = max(budget, 0) * 2 + 64 * 1024
        content = await sandbox.read_file(handle, f"{RUN_DIR}/{output}", 0, limit)
        if content.encoding == "utf-8" and not content.truncated:
            payload = _parse_output(content.content)
    except SandboxError:
        payload = None
    if payload is None:
        detail = _last_line(run.stderr) or f"종료 코드 {run.exit_code}"
        raise ExtractError("extract_crashed", f"추출 프로세스가 결과 없이 끝났다: {detail}")
    if payload.get("ok") is not True:
        failure = str(payload.get("kind") or "parse")
        message = str(payload.get("error") or "글을 꺼내지 못했다")
        if failure == "memory_limit":
            raise ExtractError("memory_limit", message)
        if failure == "parser_missing":
            raise ExtractError("parser_install_failed", f"파서를 불러오지 못했다: {message}")
        raise ExtractError("extract_failed", message)
    pages = payload.get("pages")
    return Extracted(
        text=str(payload.get("text") or ""),
        truncated=payload.get("truncated") is True,
        pages=pages if isinstance(pages, int) else None,
    )
