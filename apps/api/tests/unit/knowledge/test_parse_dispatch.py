"""인제스트 파싱의 판별과 갈래(3단계 설계 6.2). txt·md·csv·html은 서버 스레드에서, pdf·docx는
샌드박스 exec로 읽는다. 2단계 document_extractor와 같은 판별 표·읽기 함수를 쓴다.

샌드박스는 실제 로컬 백엔드를 감싸 pip·추출 exec만 흉내 낸다. 실제 pypdf 설치와 추출은
tests/windows/test_kb_ingest_pdf.py(-m windows)가 본다.
"""

import ast
import json
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.knowledge.ingest.parse import (
    NO_TEXT_LAYER_CHARS_PER_PAGE,
    SourceText,
    extract_in_sandbox,
    file_kind,
    read_local_text,
    read_text_file,
    text_read_limit,
)
from app.sandbox.base import ExecEvent, ExecExited, ExecSpec, WorkspaceHandle
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.workflow.extract_runner import ExtractError
from app.workflow.nodes import document_extractor

_VALUE = re.compile(r"^_(PATH|KIND|BUDGET) = (.+)$", re.MULTILINE)
_OUTPUT = re.compile(r"_OUTPUT = os\.path\.join\(.+?, '([^']+)'\)")


class Sandbox:
    def __init__(self, inner: LocalWorkspaceBackend) -> None:
        self.inner = inner
        self.kinds: list[str] = []

    async def acquire(self, session_id: uuid.UUID) -> WorkspaceHandle:
        return await self.inner.acquire(session_id)

    async def read_file(
        self, h: WorkspaceHandle, path: str, offset: int = 0, limit: int = 65536
    ) -> Any:
        return await self.inner.read_file(h, path, offset, limit)

    async def exec(self, h: WorkspaceHandle, spec: ExecSpec) -> AsyncIterator[ExecEvent]:
        self.kinds.append(spec.kind)
        if spec.kind == "pip":
            (h.root / ".packages" / "pypdf").mkdir(parents=True, exist_ok=True)
        else:
            values = {name: ast.literal_eval(raw) for name, raw in _VALUE.findall(spec.script)}
            match = _OUTPUT.search(spec.script)
            assert match is not None
            content = (h.root / values["PATH"]).read_bytes()
            if content.startswith(b"BROKEN"):
                payload: dict[str, Any] = {"ok": False, "kind": "parse", "error": "EOF"}
            else:
                payload = {
                    "ok": True,
                    "text": f"{values['KIND']}:{content.decode()}",
                    "pages": 3,
                    "truncated": False,
                }
            (h.root / ".run" / match.group(1)).write_text(json.dumps(payload), encoding="utf-8")
        yield ExecExited(
            exec_id=spec.exec_id,
            code=0,
            timed_out=False,
            killed=False,
            duration_ms=1,
            peak_memory_bytes=None,
        )


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    settings = Settings.model_validate({"data_dir": tmp_path / "data"})
    return Sandbox(LocalWorkspaceBackend(settings, workspaces_dir=tmp_path / "kb-work"))


def test_the_node_and_the_lane_share_one_table() -> None:
    # 노드가 같은 함수를 쓴다(표를 따로 두지 않는다).
    assert vars(document_extractor)["file_kind"] is file_kind
    cases = {
        ("보고서.PDF", ""): "pdf",
        ("a.docx", ""): "docx",
        ("메모.txt", ""): "text",
        ("README.md", ""): "markdown",
        ("표.tsv", ""): "csv",
        ("page.htm", ""): "html",
        ("noext", "text/csv"): "csv",
        ("data.bin", "text/x-log"): "text",
        ("그림.png", "image/png"): None,
        ("시트.xlsx", ""): None,
    }
    for (name, mime), kind in cases.items():
        assert file_kind({"filename": name, "mime": mime}) == kind, name


def test_text_files_read_only_the_head_within_the_budget(tmp_path: Path) -> None:
    korean = tmp_path / "cp949.txt"
    korean.write_bytes("한글 문서입니다".encode("cp949"))
    long = tmp_path / "long.txt"
    long.write_text("가" * 1000, encoding="utf-8")
    page = tmp_path / "page.html"
    page.write_text("<p>보이는 글</p><script>숨은()</script>", encoding="utf-8")

    assert read_text_file(korean, "text", 100, read_limit=1000) == ("한글 문서입니다", False)
    text, cut = read_text_file(long, "text", 30, read_limit=3000)
    assert (text, cut) == ("가" * 10, True)
    # 앞부분만 읽었으면 예산 안이어도 잘렸다고 알린다.
    assert read_text_file(long, "text", 9000, read_limit=300)[1] is True
    assert read_text_file(page, "html", 1000, read_limit=1000)[0] == "보이는 글"
    assert text_read_limit("html", 8) == 16
    assert text_read_limit("text", 8) == 8 + 512 * 1024


async def test_text_kinds_are_read_on_the_server(tmp_path: Path, sandbox: Sandbox) -> None:
    path = tmp_path / "표.csv"
    path.write_bytes("이름,값\n가,1\n".encode())

    source = await read_local_text(path, "csv", 1024)

    assert source == SourceText(text="이름,값\n가,1\n", kind="csv")
    assert sandbox.kinds == []


async def test_pdf_and_docx_go_through_the_sandbox(sandbox: Sandbox, tmp_path: Path) -> None:
    handle = await sandbox.acquire(uuid.uuid4())
    (handle.root / "source.pdf").write_bytes(b"pdf body")
    (handle.root / "source.docx").write_bytes(b"BROKEN")

    source = await extract_in_sandbox(
        sandbox,  # type: ignore[arg-type]
        handle,
        relative="source.pdf",
        kind="pdf",
        budget=4096,
        cache_dir=tmp_path / "cache",
        exec_prefix="kb-test",
    )
    with pytest.raises(ExtractError) as broken:
        await extract_in_sandbox(
            sandbox,  # type: ignore[arg-type]
            handle,
            relative="source.docx",
            kind="docx",
            budget=4096,
            cache_dir=tmp_path / "cache",
            exec_prefix="kb-test",
        )

    assert (source.text, source.kind, source.pages) == ("pdf:pdf body", "pdf", 3)
    # 파서는 한 번만 받는다(캐시 폴더에 표시 파일이 생긴다).
    assert sandbox.kinds == ["pip", "python", "python"]
    assert broken.value.code == "extract_failed"


def test_a_pdf_with_almost_no_text_per_page_is_scanned() -> None:
    pages = 10
    sparse = SourceText(
        text="x" * (NO_TEXT_LAYER_CHARS_PER_PAGE * pages - 1), kind="pdf", pages=pages
    )
    dense = SourceText(text="x" * NO_TEXT_LAYER_CHARS_PER_PAGE * pages, kind="pdf", pages=pages)
    model = SourceText(text="", kind="pdf", pages=pages, mode="model")
    docx = SourceText(text="", kind="docx", pages=None)

    assert sparse.no_text_layer
    assert not dense.no_text_layer and not model.no_text_layer and not docx.no_text_layer
    info = SourceText(text="본문", kind="pdf", pages=1, warnings=("model_read_truncated",))
    assert info.extract_info(["text_truncated", "model_read_truncated"])["warnings"] == [
        "model_read_truncated",
        "text_truncated",
    ]
