# pyright: strict
"""document_extractor 노드(2단계 설계 2.4, 8장). 파일이나 파일 배열에서 글을 꺼낸다.

파일은 먼저 런 작업 폴더(DATA_DIR/wf-workspaces/<run_id>/.tmp/extract/)에 저장소에서 흘려 옮긴다.
txt·md·csv·html은 서버 안에서 표준 라이브러리로 읽고(앞부분만 읽어 메모리를 붙잡지 않는다),
pdf·docx는 extract_runner가 샌드박스 파이썬으로 읽는다(서버 RSS가 늘지 않는다).

출력 text는 모든 파일의 글을 이은 것이고(여럿이면 "## 파일 이름" 머리글로 나눈다) texts는 파일마다
글이다. 둘 다 합쳐 변수 상한(1MB) 안에서 앞 파일부터 채우고, 잘렸으면 경고를 남긴다. 파일 하나라도
읽지 못하면 노드가 실패한다.
"""

import asyncio
import codecs
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.schemas.workflow import Issue
from app.workflow.extract_runner import ExtractError, ensure_parsers, extract_document
from app.workflow.html_text import html_to_text
from app.workflow.nodes.base import (
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    VarRef,
)
from app.workflow.ports import FilePortError
from app.workflow.variables import MAX_STRING_BYTES, is_file

FileKind = Literal["pdf", "docx", "text", "markdown", "csv", "html"]
MAX_FILES = 20
MAX_FILE_BYTES = 50 * 1024 * 1024
# 여러 파일의 머리글이 들어갈 자리를 남긴다.
TEXT_BUDGET = MAX_STRING_BYTES - 64 * 1024
# 글 파일은 출력 상한만큼 읽으면 된다. cp949처럼 2바이트 글자가 3바이트 UTF-8이 되는 경우와
# HTML 태그를 떼어 낼 몫을 더 읽는다.
TEXT_READ_BYTES = MAX_STRING_BYTES + 512 * 1024
HTML_READ_BYTES = 4 * 1024 * 1024
STAGE_DIR = ".tmp/extract"
ACCEPTED_TYPES = frozenset({"file", "array[file]", "array[any]", "any"})
_EXTENSIONS: dict[str, FileKind] = {
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
_MIMES: dict[str, FileKind] = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/markdown": "markdown",
    "text/csv": "csv",
    "text/plain": "text",
}
_SUFFIXES: dict[FileKind, str] = {
    "pdf": ".pdf",
    "docx": ".docx",
    "text": ".txt",
    "markdown": ".md",
    "csv": ".csv",
    "html": ".html",
}


class DocumentExtractorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # file 또는 array[file] 변수.
    file_var: str


def file_kind(ref: Mapping[str, Any]) -> FileKind | None:
    name = str(ref.get("filename") or "")
    stem, dot, extension = name.rpartition(".")
    if dot and stem and extension.lower() in _EXTENSIONS:
        return _EXTENSIONS[extension.lower()]
    mime = str(ref.get("mime") or "").split(";", 1)[0].strip().lower()
    if mime in _MIMES:
        return _MIMES[mime]
    return "text" if mime.startswith("text/") else None


def _file_refs(value: object) -> list[dict[str, Any]] | None:
    if value is None:
        return []
    if is_file(value):
        return [cast(dict[str, Any], value)]
    if isinstance(value, list):
        items = cast(list[Any], value)
        if all(is_file(item) for item in items):
            return [cast(dict[str, Any], item) for item in items]
    return None


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


def read_text_file(path: Path, kind: FileKind, budget: int) -> tuple[str, bool]:
    """(글, 잘렸는지). 파일 앞부분만 읽는다."""
    limit = HTML_READ_BYTES if kind == "html" else TEXT_READ_BYTES
    size = path.stat().st_size
    with path.open("rb") as file:
        data = file.read(limit)
    complete = len(data) >= size
    text = decode_text(data, complete=complete)
    if kind == "html":
        text = html_to_text(text).text
    text, cut = cut_utf8(text, budget)
    return text, cut or not complete


def _clear(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


class DocumentExtractorRunner(NodeRunner[DocumentExtractorConfig]):
    type = "document_extractor"
    Config = DocumentExtractorConfig
    # pdf·docx는 샌드박스 프로세스로 PC 메모리를 따로 쓰므로 code 노드처럼 하나씩 돈다.
    concurrency = 1
    fixed_outputs = (OutputSpec("text", "string"), OutputSpec("texts", "array[string]"))

    def var_refs(self, config: DocumentExtractorConfig) -> list[VarRef]:
        return [VarRef("file_var", config.file_var)]

    def validate(self, config: DocumentExtractorConfig, graph: GraphView) -> list[Issue]:
        declared = graph.output_type(config.file_var)
        if declared is not None and declared not in ACCEPTED_TYPES:
            return [
                Issue(
                    level="error",
                    code="type_mismatch",
                    message=f"file_var는 file이나 array[file]이어야 하는데 {declared}다",
                    field="file_var",
                )
            ]
        return []

    def timeout_sec(self, config: DocumentExtractorConfig) -> float | None:
        # 파서 설치(처음 한 번)와 파일마다의 추출 시간 상한이 따로 있다. 이것은 그 합의 바깥
        # 울타리다.
        return 1800

    async def run(
        self, ctx: NodeContext, config: DocumentExtractorConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        refs = _file_refs(ctx.resolve(config.file_var))
        if refs is None:
            raise NodeFailure("type_mismatch", "file_var는 파일이나 파일 배열이어야 한다")
        if len(refs) > MAX_FILES:
            raise NodeFailure("file_limit", f"한 번에 읽을 파일은 {MAX_FILES}개까지다")
        inputs: dict[str, Any] = {
            "file_var": config.file_var,
            "files": [
                {
                    "filename": ref.get("filename"),
                    "mime": ref.get("mime"),
                    "size": ref.get("size_bytes"),
                }
                for ref in refs
            ],
        }
        if not refs:
            return NodeResult(status="succeeded", outputs={"text": "", "texts": []}, inputs=inputs)
        kinds: list[FileKind] = []
        for ref in refs:
            kind = file_kind(ref)
            if kind is None:
                name = ref.get("filename") or "파일"
                raise NodeFailure(
                    "unsupported_file_type",
                    f"{name}은(는) 읽을 수 없는 형식이다. pdf·docx·txt·md·csv·html만 읽는다",
                )
            kinds.append(kind)
        files, sandbox = ctx.ports.files, ctx.ports.sandbox
        if files is None or sandbox is None:
            raise NodeFailure("extractor_unavailable", "파일 저장소나 런 작업 폴더가 없다")
        try:
            handle = await sandbox.acquire(ctx.run_id)
        except SandboxError as exc:
            raise NodeFailure(exc.code, exc.message) from None
        stage = f"{STAGE_DIR}/{ctx.node_run_id.hex}"
        texts: list[str] = []
        details: list[dict[str, Any]] = []
        left = TEXT_BUDGET
        lib: Path | None = None
        try:
            for index, (ref, kind) in enumerate(zip(refs, kinds, strict=True)):
                name = str(ref.get("filename") or f"파일 {index + 1}")
                relative = f"{stage}/{index}{_SUFFIXES[kind]}"
                size = await files.copy_to(ref, handle.root / relative, max_bytes=MAX_FILE_BYTES)
                pages: int | None = None
                if kind in ("pdf", "docx"):
                    if lib is None:
                        lib = await self._parsers(ctx, sandbox, handle)
                    extracted = await extract_document(
                        sandbox,
                        handle,
                        lib=lib,
                        path=relative,
                        kind="pdf" if kind == "pdf" else "docx",
                        budget=left,
                        exec_id=f"wf-ext-{ctx.node_run_id.hex[-8:]}-{index}",
                    )
                    text, cut, pages = extracted.text, extracted.truncated, extracted.pages
                else:
                    text, cut = await asyncio.to_thread(
                        read_text_file, handle.root / relative, kind, left
                    )
                left -= len(text.encode("utf-8"))
                texts.append(text)
                details.append(
                    {
                        "filename": name,
                        "kind": kind,
                        "bytes": size,
                        "pages": pages,
                        "truncated": cut,
                    }
                )
                if cut:
                    ctx.warn(f"text_truncated: {name}의 글이 길어 앞부분만 담았다")
        except FilePortError as exc:
            raise NodeFailure(exc.code, exc.message) from None
        except OSError as exc:
            raise NodeFailure("extract_failed", f"파일을 읽지 못했다: {exc}") from None
        except ExtractError as exc:
            message = exc.message
            if len(details) < len(refs):
                message = f"{refs[len(details)].get('filename') or '파일'}: {message}"
            raise NodeFailure(exc.code, message, retryable=exc.retryable) from None
        finally:
            await asyncio.to_thread(_clear, handle.root / stage)
        if len(texts) == 1:
            joined = texts[0]
        else:
            joined = "\n\n".join(
                f"## {detail['filename']}\n\n{text}"
                for detail, text in zip(details, texts, strict=True)
            )
        joined, cut = cut_utf8(joined, MAX_STRING_BYTES)
        if cut:
            ctx.warn("text_truncated: 이은 글이 1MB를 넘어 앞부분만 담았다")
        return NodeResult(
            status="succeeded",
            outputs={"text": joined, "texts": texts},
            inputs=inputs,
            process={"files": details, "parsers": str(lib) if lib is not None else None},
        )

    async def _parsers(
        self, ctx: NodeContext, sandbox: SandboxBackend, handle: WorkspaceHandle
    ) -> Path:
        cache = ctx.ports.node_settings.extract_cache_dir
        if cache is None:
            raise NodeFailure(
                "extractor_unavailable", "pdf·docx 파서를 둘 캐시 폴더가 정해지지 않았다"
            )
        return await ensure_parsers(
            sandbox, handle, cache, exec_id=f"wf-pip-{ctx.node_run_id.hex[-8:]}"
        )
