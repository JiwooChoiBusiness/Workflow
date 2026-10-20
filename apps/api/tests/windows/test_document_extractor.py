"""document_extractor의 실제 pdf·docx 추출(2단계 설계 2.4, 8장, 11.1 Q8, 10장 M19).

임시 DATA_DIR에서 처음 쓸 때 pypdf·python-docx를 런 작업 폴더의 pip(--target)으로 받아
cache/wf-extract로 옮기고, 샌드박스 파이썬(python -I, Job Object)에서 글을 꺼낸다. 서버 프로세스(이
시험 프로세스)는 파서를 불러오지 않는다. PyPI에 닿지 않으면 건너뛴다. 기본 실행에서 빠지고
`uv run pytest -m windows`로 돈다.
"""

import io
import shutil
import socket
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest

from app.config import Settings
from app.runner.supervisor import CancelSignal
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.schemas.workflow import Graph
from app.workflow.engine import Engine, RunContext, RunOutcome
from app.workflow.extract_runner import READY_MARKER, parser_dir
from app.workflow.frames import Checkpoint
from app.workflow.ports import NodeSettings

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows 샌드박스 파이썬"),
]

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_pdf(pages: list[str]) -> bytes:
    """글자 한 줄씩 든 쪽들로 된 작은 PDF(Helvetica, ASCII)."""
    bodies: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids: list[int] = []
    number = 4
    for text in pages:
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        bodies[number] = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        bodies[number + 1] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % number
        )
        kids.append(number + 1)
        number += 2
    refs = " ".join(f"{kid} 0 R" for kid in kids)
    bodies[2] = f"<< /Type /Pages /Kids [{refs}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for key in sorted(bodies):
        offsets[key] = len(out)
        out += b"%d 0 obj\n" % key + bodies[key] + b"\nendobj\n"
    xref = len(out)
    size = max(bodies) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for key in range(1, size):
        out += b"%010d 00000 n \n" % offsets[key]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def make_docx(paragraphs: list[str], rows: list[list[str]]) -> bytes:
    """문단 사이에 표 하나가 든 작은 DOCX."""

    def para(text: str) -> str:
        return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'

    grid = "".join('<w:gridCol w:w="2000"/>' for _ in rows[0])
    table = (
        f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>'
        + "".join(
            "<w:tr>" + "".join(f"<w:tc>{para(cell)}</w:tc>" for cell in row) + "</w:tr>"
            for row in rows
        )
        + "</w:tbl>"
    )
    body = para(paragraphs[0]) + table + "".join(para(text) for text in paragraphs[1:])
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def pypi_reachable() -> bool:
    try:
        with socket.create_connection(("pypi.org", 443), timeout=5):
            return True
    except OSError:
        return False


class Files:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def add(self, name: str, content: bytes) -> dict[str, Any]:
        file_id = str(uuid.uuid4())
        self.data[file_id] = content
        return {
            "file_id": file_id,
            "filename": name,
            "mime": "application/octet-stream",
            "size_bytes": len(content),
            "storage_key": f"wf-files/w/{file_id}",
            "source": "wf_file",
        }

    async def save_output(self, source: Path, *, filename: str, mime: str | None) -> dict[str, Any]:
        raise AssertionError("쓰지 않는다")

    async def read_bytes(self, ref: Mapping[str, Any], *, max_bytes: int) -> bytes:
        raise AssertionError("쓰지 않는다")

    async def copy_to(self, ref: Mapping[str, Any], dest: Path, *, max_bytes: int) -> int:
        content = self.data[str(ref["file_id"])]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        return len(content)


@dataclass
class Ports:
    sandbox: LocalWorkspaceBackend
    files: Files
    node_settings: NodeSettings
    model: None = None
    http: None = None
    tasks: None = None
    checkpoints: list[Checkpoint] = field(default_factory=list[Checkpoint])

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.perf_counter()

    async def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self.checkpoints.append(checkpoint)


@dataclass
class Env:
    sandbox: LocalWorkspaceBackend
    files: Files
    cache: Path


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    if not pypi_reachable():
        pytest.skip("PyPI에 닿지 않아 파서를 받을 수 없다")
    # pytest의 tmp_path는 길어 pip의 임시 폴더(런 폴더의 .tmp 아래)가 Windows 경로 길이 260자를
    # 넘는다. 실제 DATA_DIR처럼 짧은 경로를 쓴다.
    root = Path(tempfile.mkdtemp(prefix="wfx")).resolve()
    data = root / "data"
    settings = Settings.model_validate({"data_dir": data})
    sandbox = LocalWorkspaceBackend(settings, workspaces_dir=data / "wf-workspaces")
    try:
        yield Env(sandbox, Files(), data / "cache" / "wf-extract")
    finally:
        await sandbox.aclose()
        shutil.rmtree(root, ignore_errors=True)


async def extract(env: Env, refs: list[dict[str, Any]]) -> RunOutcome:
    graph = Graph.model_validate(
        {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "config": {"fields": [{"name": "docs", "type": "file_list"}]},
                },
                {"id": "read", "type": "document_extractor", "config": {"file_var": "start.docs"}},
                {
                    "id": "end_1",
                    "type": "end",
                    "config": {
                        "outputs": [
                            {"name": "text", "var": "read.text"},
                            {"name": "texts", "var": "read.texts"},
                        ]
                    },
                },
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "read"},
                {"id": "e2", "source": "read", "target": "end_1"},
            ],
        }
    )
    ports = Ports(env.sandbox, env.files, NodeSettings(extract_cache_dir=env.cache))
    context = RunContext(
        run_id=uuid.uuid4(),
        graph=graph,
        kind="workflow",
        ports=ports,
        cancel=CancelSignal(),
        inputs={"docs": refs},
    )
    run = Engine().run(context)
    async with run:
        async for _event in run:
            pass
    return run.outcome


async def test_real_pdf_and_docx_are_read_in_the_sandbox(env: Env) -> None:
    pdf = env.files.add("report.pdf", make_pdf(["Hello PDF page one", "Second page here"]))
    docx = env.files.add(
        "계약.docx", make_docx(["첫 문단입니다", "끝 문단"], [["이름", "값"], ["가", "1"]])
    )
    note = env.files.add("메모.txt", "서버에서 읽는다".encode())

    started = time.perf_counter()
    first = await extract(env, [pdf, docx, note])
    install_sec = time.perf_counter() - started

    assert first.status == "succeeded", first.error
    pdf_text, docx_text, note_text = first.outputs["texts"]
    assert "Hello PDF page one" in pdf_text and "Second page here" in pdf_text
    assert docx_text.splitlines() == [
        "첫 문단입니다",
        "| 이름 | 값 |",
        "| --- | --- |",
        "| 가 | 1 |",
        "끝 문단",
    ]
    assert note_text == "서버에서 읽는다"
    target = parser_dir(env.cache)
    marker = target / READY_MARKER
    assert marker.is_file() and (target / "pypdf").is_dir() and (target / "docx").is_dir()
    # 파서는 샌드박스 프로세스에서만 돈다. 서버(이 시험 프로세스)는 불러오지 않는다.
    assert "pypdf" not in sys.modules and "docx" not in sys.modules

    stamp = marker.stat().st_mtime_ns
    started = time.perf_counter()
    second = await extract(env, [pdf])
    cached_sec = time.perf_counter() - started

    assert second.status == "succeeded", second.error
    assert "Hello PDF page one" in second.outputs["text"]
    # 두 번째는 캐시를 그대로 쓴다.
    assert marker.stat().st_mtime_ns == stamp
    assert cached_sec < install_sec


async def test_a_broken_pdf_fails_with_extract_failed(env: Env) -> None:
    broken = env.files.add("깨진.pdf", b"%PDF-1.4\nnot really a pdf")

    outcome = await extract(env, [broken])

    assert outcome.status == "failed"
    assert outcome.error is not None and outcome.error.code == "extract_failed"
    assert outcome.error.message.startswith("깨진.pdf:")
