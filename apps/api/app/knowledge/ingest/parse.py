"""문서 형식 판별과 글 꺼내기(3단계 설계 6.2). 2단계 document_extractor 노드와 같은 표·함수를 쓴다.

txt·md·csv·html은 서버 안에서 표준 라이브러리로 읽되(UTF-8 → cp949) 앞부분만 읽고, pdf·docx는
extract_runner가 샌드박스 파이썬으로 읽는다. 서버는 pypdf·python-docx를 import하지 않는다.
"""

import asyncio
import codecs
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.sandbox.base import SandboxBackend, WorkspaceHandle
from app.workflow.extract_runner import ensure_parsers, extract_document
from app.workflow.html_text import html_to_text

FileKind = Literal["pdf", "docx", "text", "markdown", "csv", "html"]
ExtractMode = Literal["text", "model"]
SANDBOX_KINDS: frozenset[FileKind] = frozenset({"pdf", "docx"})
# 쪽당 글자가 이보다 적으면 글자 층이 없는(스캔) PDF로 본다(6.2).
NO_TEXT_LAYER_CHARS_PER_PAGE = 20
WARN_NO_TEXT_LAYER = "no_text_layer"
WARN_TEXT_TRUNCATED = "text_truncated"
# cp949처럼 2바이트 글자가 3바이트 UTF-8이 되는 몫을 더 읽는다.
TEXT_READ_SLACK = 512 * 1024

EXTENSIONS: dict[str, FileKind] = {
    "pdf": "pdf",
    "docx": "docx",
    "txt": "text",
    "text": "text",
    "log": "text",
    "md": "markdown",
    "markdown": "markdown",
    "csv": "csv",
    "tsv": "csv",
    "html": "html",
    "htm": "html",
    "xhtml": "html",
}
MIMES: dict[str, FileKind] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/markdown": "markdown",
    "text/csv": "csv",
    "text/plain": "text",
}
SUFFIXES: dict[FileKind, str] = {
    "pdf": ".pdf",
    "docx": ".docx",
    "text": ".txt",
    "markdown": ".md",
    "csv": ".csv",
    "html": ".html",
}


def file_kind(ref: Mapping[str, Any]) -> FileKind | None:
    name = str(ref.get("filename") or "")
    stem, dot, extension = name.rpartition(".")
    if dot and stem and extension.lower() in EXTENSIONS:
        return EXTENSIONS[extension.lower()]
    mime = str(ref.get("mime") or "").split(";", 1)[0].strip().lower()
    if mime in MIMES:
        return MIMES[mime]
    return "text" if mime.startswith("text/") else None


def decode_text(data: bytes, *, complete: bool) -> str:
    """UTF-8(BOM 포함), 안 되면 cp949로 푼다. 읽다 만 끝 글자는 버린다."""
    for encoding in ("utf-8-sig", "cp949"):
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        try:
            return decoder.decode(data, final=complete)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def cut_utf8(text: str, limit: int) -> tuple[str, bool]:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[: max(limit, 0)].decode("utf-8", errors="ignore"), True


def read_text_file(path: Path, kind: FileKind, budget: int, *, read_limit: int) -> tuple[str, bool]:
    """(글, 잘렸는지). 파일 앞 read_limit 바이트만 읽고 글은 budget 바이트(UTF-8)까지 둔다."""
    size = path.stat().st_size
    with path.open("rb") as file:
        data = file.read(read_limit)
    complete = len(data) >= size
    text = decode_text(data, complete=complete)
    if kind == "html":
        text = html_to_text(text).text
    text, cut = cut_utf8(text, budget)
    return text, cut or not complete


@dataclass(frozen=True)
class SourceText:
    text: str
    kind: FileKind
    mode: ExtractMode = "text"
    pages: int | None = None
    truncated: bool = False
    # 꺼내면서 생긴 문서 경고(모델 읽기의 model_read_truncated 등).
    warnings: tuple[str, ...] = ()

    @property
    def no_text_layer(self) -> bool:
        if self.kind != "pdf" or self.mode != "text" or not self.pages:
            return False
        return len(self.text.strip()) < NO_TEXT_LAYER_CHARS_PER_PAGE * self.pages

    def extract_info(self, warnings: list[str]) -> dict[str, Any]:
        # kb_document.extract(4.1). warnings는 no_text_layer·text_truncated 같은 문서 경고다.
        return {
            "mode": self.mode,
            "pages": self.pages,
            "chars": len(self.text),
            "truncated": self.truncated,
            "warnings": [*self.warnings, *(item for item in warnings if item not in self.warnings)],
        }


def text_read_limit(kind: FileKind, budget: int) -> int:
    # HTML은 태그를 떼면 글이 크게 준다. 글 상한의 두 배까지 읽는다.
    return budget * 2 if kind == "html" else budget + TEXT_READ_SLACK


async def read_local_text(path: Path, kind: FileKind, budget: int) -> SourceText:
    # 8MB 글을 읽고 HTML 태그를 떼는 일이 이벤트 루프를 막지 않게 스레드에서 한다.
    text, cut = await asyncio.to_thread(
        read_text_file, path, kind, budget, read_limit=text_read_limit(kind, budget)
    )
    return SourceText(text=text, kind=kind, truncated=cut)


async def extract_in_sandbox(
    sandbox: SandboxBackend,
    handle: WorkspaceHandle,
    *,
    relative: str,
    kind: FileKind,
    budget: int,
    cache_dir: Path,
    exec_prefix: str,
) -> SourceText:
    """작업 폴더 안 relative 파일(pdf·docx)의 글. ExtractError는 그대로 올린다."""
    lib = await ensure_parsers(sandbox, handle, cache_dir, exec_id=f"{exec_prefix}-pip")
    extracted = await extract_document(
        sandbox,
        handle,
        lib=lib,
        path=relative,
        kind="pdf" if kind == "pdf" else "docx",
        budget=budget,
        exec_id=f"{exec_prefix}-ext",
    )
    return SourceText(
        text=extracted.text, kind=kind, pages=extracted.pages, truncated=extracted.truncated
    )
