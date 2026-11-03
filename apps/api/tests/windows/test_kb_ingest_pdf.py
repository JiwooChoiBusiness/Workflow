"""지식 베이스 인제스트의 실제 pdf·docx 파싱과 스캔 PDF 모델 읽기(3단계 설계 6.2, M22).

세 번째 로컬 백엔드(DATA_DIR/kb-work)에서 처음 쓸 때 pypdf·python-docx를 받아 cache/wf-extract에
두고(워크플로우 노드와 같은 캐시), 샌드박스 파이썬(python -I, Job Object)에서 글을 꺼낸다. 모델
읽기는 같은 샌드박스의 pypdf로 PDF를 조각내고 가짜 어댑터 각본으로 옮겨 적는다. PyPI에 닿지 않으면
건너뛴다. 기본 실행에서 빠지고 `uv run pytest -m windows`로 돈다.
"""

import asyncio
import io
import shutil
import socket
import sys
import tempfile
import time
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import sysinfo
from app.config import Settings
from app.llm.fake_adapter import FakeAdapter, FakeEmbedder, fake_end, fake_text
from app.logsetup import configure_logging
from app.main import create_app
from app.workflow.extract_runner import READY_MARKER, parser_dir

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows 샌드박스 파이썬"),
]

BASE = "/api/console/knowledge"
FETCH = {"X-Requested-With": "fetch"}
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
FIRST = "First page text about the quarterly meeting"
SECOND = "Second page text about the travel expense rules"


def make_pdf(pages: list[str]) -> bytes:
    """글자 한 줄씩 든 쪽들로 된 작은 PDF(Helvetica, ASCII). 빈 글이면 글자 층이 없는 쪽이다."""
    bodies: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids: list[int] = []
    number = 4
    for text in pages:
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1") if text else b""
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


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
        for text in paragraphs
    )
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


@asynccontextmanager
async def running(adapter: FakeAdapter) -> AsyncIterator[tuple[FastAPI, AsyncClient, Path]]:
    if not pypi_reachable():
        pytest.skip("PyPI에 닿지 않아 파서를 받을 수 없다")
    # pip 임시 폴더가 경로 길이 260자를 넘지 않게 실제 DATA_DIR처럼 짧은 경로를 쓴다.
    root = Path(tempfile.mkdtemp(prefix="wfk")).resolve()
    settings = Settings.model_validate(
        {"data_dir": root / "data", "web_dist_dir": root / "no-dist", "kb_embed_rpm": 60_000}
    )
    sysinfo_checks = sysinfo.startup_checks
    sysinfo.startup_checks = lambda _settings: []  # type: ignore[assignment]
    application = create_app(settings)
    application.state.llm = adapter
    try:
        async with (
            application.router.lifespan_context(application),
            AsyncClient(
                transport=ASGITransport(app=application), base_url="http://127.0.0.1:8500"
            ) as client,
        ):
            await client.post(
                "/api/console/setup",
                json={"email": "owner@example.com", "password": "correct-horse-battery"},
            )
            await client.post(
                "/api/console/auth/login",
                json={"email": "owner@example.com", "password": "correct-horse-battery"},
            )
            yield application, client, root / "data"
    finally:
        sysinfo.startup_checks = sysinfo_checks  # type: ignore[assignment]
        configure_logging("INFO")
        shutil.rmtree(root, ignore_errors=True)


async def wait_done(client: AsyncClient, kb_id: str, document_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 600
    while True:
        document = (await client.get(f"{BASE}/{kb_id}/documents/{document_id}")).json()
        if document["indexing_status"] in ("completed", "error"):
            return document
        assert time.monotonic() < deadline, document
        await asyncio.sleep(0.2)


async def create_kb(client: AsyncClient) -> str:
    response = await client.post(BASE, json={"name": "pdf", "data_policy": {"acknowledged": True}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_pdf_and_docx_are_parsed_in_the_sandbox() -> None:
    async with running(FakeAdapter(embedder=FakeEmbedder())) as (application, client, data):
        kb_id = await create_kb(client)
        uploaded = await client.post(
            f"{BASE}/{kb_id}/documents",
            files=[
                ("files", ("report.pdf", make_pdf([FIRST, SECOND]), "application/pdf")),
                (
                    "files",
                    ("memo.docx", make_docx(["Hello from docx", "Another paragraph"]), DOCX),
                ),
            ],
            headers=FETCH,
        )
        assert uploaded.status_code == 201, uploaded.text
        documents = uploaded.json()["documents"]
        results = [await wait_done(client, kb_id, document["id"]) for document in documents]
        texts = [
            " ".join(
                item["content"]
                for item in (
                    await client.get(f"{BASE}/{kb_id}/documents/{result['id']}/segments")
                ).json()["items"]
            )
            for result in results
        ]

        assert [result["indexing_status"] for result in results] == ["completed", "completed"]
        assert results[0]["extract"]["pages"] == 2
        # 쪽당 20자가 넘어야 글자 층이 있는 PDF로 본다(스캔 PDF 판별, 6.2).
        assert FIRST in texts[0] and SECOND in texts[0]
        assert "Hello from docx" in texts[1]
        # 파서는 워크플로우 노드와 같은 캐시에 한 번만 받고, 작업 폴더는 끝나면 지운다.
        assert (parser_dir(data / "cache" / "wf-extract") / READY_MARKER).is_file()
        assert not any((data / "kb-work").iterdir())


async def test_a_scanned_pdf_is_read_by_the_model_after_it_ends_with_no_text_layer() -> None:
    # 첫 조각(3쪽)은 출력 상한에서 잘려 반으로 나뉜다(1쪽 + 2쪽).
    adapter = FakeAdapter(
        [
            [*fake_text("잘린 응답"), fake_end("max_tokens")],
            [*fake_text("첫 쪽을 모델이 옮겨 적은 글")],
            [*fake_text("둘째와 셋째 쪽을 옮겨 적은 글")],
        ],
        embedder=FakeEmbedder(),
    )
    async with running(adapter) as (application, client, data):
        kb_id = await create_kb(client)
        uploaded = await client.post(
            f"{BASE}/{kb_id}/documents",
            files=[("files", ("scan.pdf", make_pdf(["", "", ""]), "application/pdf"))],
            headers=FETCH,
        )
        document_id = uploaded.json()["documents"][0]["id"]
        scanned = await wait_done(client, kb_id, document_id)

        assert scanned["indexing_status"] == "completed"
        assert scanned["total_segments"] == 0
        assert scanned["extract"]["warnings"] == ["no_text_layer"]

        reindex = await client.post(
            f"{BASE}/{kb_id}/documents/{document_id}/reindex", json={"extract_mode": "model"}
        )
        assert reindex.status_code == 200, reindex.text
        done = await wait_done(client, kb_id, document_id)
        items = (await client.get(f"{BASE}/{kb_id}/documents/{document_id}/segments")).json()[
            "items"
        ]

        assert done["indexing_status"] == "completed", done
        assert done["extract"]["mode"] == "model" and done["extract"]["pages"] == 3
        text = " ".join(item["content"] for item in items)
        assert text.index("첫 쪽을") < text.index("둘째와 셋째")
        assert "잘린 응답" not in text
        assert [request.messages[0].blocks[0].type for request in adapter.requests] == ["image"] * 3
