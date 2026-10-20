"""document_extractor 노드와 추출 러너(2단계 설계 2.4, 8장, 11.1 Q8).

txt·md·csv·html은 서버 안에서 읽고, pdf·docx는 샌드박스 exec로 읽는다. 여기서는 실제 로컬
백엔드(임시 DATA_DIR/wf-workspaces)를 감싸 pip·추출 exec만 흉내 내어 노드와 러너의 약속(파서 설치
한 번과 잠금, 캐시 폴더 표시, 결과 JSON, 오류 코드, 1MB 예산)을 본다. 실제 pypdf·python-docx 설치와
추출은 tests/windows/test_document_extractor.py(-m windows)가 본다.
"""

import ast
import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from app.config import Settings
from app.sandbox.base import ExecEvent, ExecExited, ExecOutput, ExecSpec, WorkspaceHandle
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.workflow.extract_runner import READY_MARKER, ExtractError, ensure_parsers, parser_dir
from app.workflow.graph import validate
from app.workflow.nodes.document_extractor import TEXT_BUDGET, decode_text, file_kind
from app.workflow.ports import FilePortError, NodeSettings

Node = Callable[..., dict[str, Any]]


def ref(name: str, mime: str = "application/octet-stream") -> dict[str, Any]:
    return {
        "file_id": str(uuid.uuid4()),
        "filename": name,
        "mime": mime,
        "size_bytes": 0,
        "storage_key": f"wf-files/w/{name}",
        "source": "wf_file",
    }


class Files:
    """FilePort. file_id → 내용을 메모리에 두고 copy_to로 런 폴더에 쓴다."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.copied: list[Path] = []

    def add(
        self, name: str, content: bytes, mime: str = "application/octet-stream"
    ) -> dict[str, Any]:
        item = ref(name, mime)
        item["size_bytes"] = len(content)
        self.data[item["file_id"]] = content
        return item

    async def save_output(self, source: Path, *, filename: str, mime: str | None) -> dict[str, Any]:
        raise AssertionError("쓰지 않는다")

    async def read_bytes(self, ref: Mapping[str, Any], *, max_bytes: int) -> bytes:
        raise AssertionError("쓰지 않는다")

    async def copy_to(self, ref: Mapping[str, Any], dest: Path, *, max_bytes: int) -> int:
        content = self.data.get(str(ref["file_id"]))
        if content is None:
            raise FilePortError("file_not_found", f"파일을 찾지 못했다: {ref['filename']}")
        if len(content) > max_bytes:
            raise FilePortError("file_too_large", "너무 크다")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        self.copied.append(dest)
        return len(content)


_SCRIPT_VALUE = re.compile(r"^_(PATH|KIND|BUDGET) = (.+)$", re.MULTILINE)
_OUTPUT = re.compile(r"_OUTPUT = os\.path\.join\(.+?, '([^']+)'\)")


class Sandbox:
    """실제 로컬 백엔드를 감싸 pip·추출 exec만 흉내 낸다.

    pip은 .packages에 빈 패키지 폴더를 만들고, 추출은 런 폴더의 파일 내용을 "<kind>:<내용>" 글로
    돌려준다. 내용이 BROKEN으로 시작하면 파서 오류를 낸다.
    """

    def __init__(self, inner: LocalWorkspaceBackend) -> None:
        self.inner = inner
        self.pip_runs = 0
        self.pip_code = 0
        self.extracts: list[dict[str, Any]] = []

    async def acquire(self, session_id: uuid.UUID) -> WorkspaceHandle:
        return await self.inner.acquire(session_id)

    async def read_file(
        self, h: WorkspaceHandle, path: str, offset: int = 0, limit: int = 65536
    ) -> Any:
        return await self.inner.read_file(h, path, offset, limit)

    async def exec(self, h: WorkspaceHandle, spec: ExecSpec) -> AsyncIterator[ExecEvent]:
        if spec.kind == "pip":
            self.pip_runs += 1
            assert spec.env["PIP_RETRIES"] == "1"
            await asyncio.sleep(0.02)
            if self.pip_code == 0:
                package = h.root / ".packages" / "pypdf"
                package.mkdir(parents=True, exist_ok=True)
                (package / "__init__.py").write_text("", encoding="utf-8")
            else:
                yield ExecOutput(
                    exec_id=spec.exec_id,
                    stream="stderr",
                    data=b"ERROR: Could not find a version that satisfies pypdf\n",
                )
            yield ExecExited(
                exec_id=spec.exec_id,
                code=self.pip_code,
                timed_out=False,
                killed=False,
                duration_ms=20,
                peak_memory_bytes=None,
            )
            return
        values = {name: ast.literal_eval(raw) for name, raw in _SCRIPT_VALUE.findall(spec.script)}
        match = _OUTPUT.search(spec.script)
        assert match is not None and "site.addsitedir(" in spec.script
        self.extracts.append(values)
        content = (h.root / values["PATH"]).read_bytes()
        if content.startswith(b"BROKEN"):
            payload: dict[str, Any] = {"ok": False, "kind": "parse", "error": "PdfReadError: EOF"}
        else:
            text = f"{values['KIND']}:{content.decode('utf-8')}"
            data = text.encode("utf-8")
            cut = len(data) > values["BUDGET"]
            text = data[: values["BUDGET"]].decode("utf-8", "ignore")
            payload = {"ok": True, "text": text, "pages": 2, "truncated": cut}
        (h.root / ".run" / match.group(1)).write_text(json.dumps(payload), encoding="utf-8")
        yield ExecExited(
            exec_id=spec.exec_id,
            code=0,
            timed_out=False,
            killed=False,
            duration_ms=5,
            peak_memory_bytes=None,
        )


class Env:
    def __init__(self, tmp_path: Path) -> None:
        settings = Settings.model_validate({"data_dir": tmp_path / "data"})
        self.inner = LocalWorkspaceBackend(
            settings, workspaces_dir=tmp_path / "data" / "wf-workspaces"
        )
        self.sandbox = Sandbox(self.inner)
        self.files = Files()
        self.cache = tmp_path / "data" / "cache" / "wf-extract"

    async def run(self, kit: Any, graph: Any, *, cache: bool = True, **options: Any) -> Any:
        settings = NodeSettings(extract_cache_dir=self.cache if cache else None)
        ports = kit.ports(sandbox=self.sandbox, files=self.files, node_settings=settings)
        return await kit.run(graph, ports=ports, run_id=uuid.uuid4(), **options)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    instance = Env(tmp_path)
    yield instance
    await instance.inner.aclose()


def extractor_graph(kit: Any, make_node: Node, files: Any) -> Any:
    return kit.graph(
        [
            make_node("start", "start"),
            make_node("files_1", "test_flaky", {"value": files}),
            make_node("document_extractor_1", "document_extractor", {"file_var": "files_1.value"}),
            make_node(
                "end_1",
                "end",
                {
                    "outputs": [
                        {"name": "text", "var": "document_extractor_1.text"},
                        {"name": "texts", "var": "document_extractor_1.texts"},
                    ]
                },
            ),
        ],
        ["start>files_1", "files_1>document_extractor_1", "document_extractor_1>end_1"],
    )


def record_of(run: Any) -> Any:
    return next(
        event.node_run
        for event in run.events
        if event.type == "node.completed" and event.data["node_id"] == "document_extractor_1"
    )


async def test_text_formats_are_read_in_the_server(kit: Any, make_node: Node, env: Env) -> None:
    files = [
        env.files.add("메모.txt", "첫 줄\n둘째 줄".encode()),
        env.files.add("정리.md", "# 제목\n- 항목".encode()),
        env.files.add("표.csv", "이름,값\n가,1\n".encode("cp949")),
        env.files.add(
            "page.htm",
            "<html><head><title>t</title><script>x()</script></head><p>본문</p></html>".encode(),
        ),
    ]

    run = await env.run(kit, extractor_graph(kit, make_node, files))

    assert run.outcome.status == "succeeded", run.outcome.error
    assert run.outcome.outputs["texts"] == [
        "첫 줄\n둘째 줄",
        "# 제목\n- 항목",
        "이름,값\n가,1\n",
        "본문",
    ]
    text = run.outcome.outputs["text"]
    assert text.startswith("## 메모.txt\n\n첫 줄\n둘째 줄\n\n## 정리.md\n\n# 제목")
    assert text.endswith("## page.htm\n\n본문")
    record = record_of(run)
    assert [item["kind"] for item in record.process["files"]] == ["text", "markdown", "csv", "html"]
    assert record.process["parsers"] is None
    # pdf·docx가 없으면 파서를 설치하지 않는다.
    assert env.sandbox.pip_runs == 0
    # 런 폴더에 옮긴 파일은 끝나면 지운다.
    assert all(not path.exists() for path in env.files.copied)


async def test_a_single_file_has_no_header_and_long_text_is_cut(
    kit: Any, make_node: Node, env: Env
) -> None:
    single = env.files.add("긴글.txt", ("가" * 400_000).encode(), "text/plain")

    run = await env.run(kit, extractor_graph(kit, make_node, single))

    text = run.outcome.outputs["text"]
    assert text == run.outcome.outputs["texts"][0]
    assert len(text.encode("utf-8")) <= TEXT_BUDGET
    assert any(warning.startswith("text_truncated") for warning in record_of(run).warnings)


async def test_pdf_and_docx_go_through_the_sandbox_and_install_once(
    kit: Any, make_node: Node, env: Env
) -> None:
    files = [
        env.files.add("보고서.pdf", b"pdf body", "application/pdf"),
        env.files.add("메모.txt", "서버에서".encode()),
        env.files.add("계약.docx", "docx 본문".encode()),
    ]

    first = await env.run(kit, extractor_graph(kit, make_node, files))
    second = await env.run(kit, extractor_graph(kit, make_node, files[:1]))

    assert first.outcome.status == "succeeded", first.outcome.error
    assert first.outcome.outputs["texts"] == ["pdf:pdf body", "서버에서", "docx:docx 본문"]
    assert second.outcome.outputs["text"] == "pdf:pdf body"
    # 파서는 처음 한 번만 받아 캐시로 옮긴다.
    assert env.sandbox.pip_runs == 1
    target = parser_dir(env.cache)
    assert (target / READY_MARKER).is_file() and (target / "pypdf" / "__init__.py").is_file()
    # 추출 스크립트는 런 폴더 상대 경로와 남은 예산을 받는다.
    budgets = [values["BUDGET"] for values in env.sandbox.extracts]
    assert budgets[0] == TEXT_BUDGET
    assert budgets[1] < TEXT_BUDGET
    assert all(values["PATH"].startswith(".tmp/extract/") for values in env.sandbox.extracts)
    record = record_of(first)
    assert record.process["files"][0]["pages"] == 2
    assert record.process["parsers"] == str(target)


async def test_concurrent_installs_share_one_pip_run(env: Env) -> None:
    handles = [await env.sandbox.acquire(uuid.uuid4()) for _ in range(3)]

    found = await asyncio.gather(
        *(
            ensure_parsers(cast(Any, env.sandbox), h, env.cache, exec_id=f"pip-{i}")
            for i, h in enumerate(handles)
        )
    )

    assert env.sandbox.pip_runs == 1
    assert len(set(found)) == 1


async def test_install_failure_is_a_retryable_node_error(
    kit: Any, make_node: Node, env: Env
) -> None:
    env.sandbox.pip_code = 1
    files = [env.files.add("a.pdf", b"x", "application/pdf")]

    run = await env.run(kit, extractor_graph(kit, make_node, files))

    error = run.outcome.error
    assert error is not None and error.code == "parser_install_failed"
    assert error.retryable is True
    assert "Could not find a version" in error.message
    assert not (parser_dir(env.cache) / READY_MARKER).exists()

    with pytest.raises(ExtractError):
        handle = await env.sandbox.acquire(uuid.uuid4())
        await ensure_parsers(cast(Any, env.sandbox), handle, env.cache, exec_id="pip-x")


async def test_parse_errors_name_the_file(kit: Any, make_node: Node, env: Env) -> None:
    files = [
        env.files.add("좋은.pdf", b"fine", "application/pdf"),
        env.files.add("깨진.pdf", b"BROKEN pdf", "application/pdf"),
    ]

    run = await env.run(kit, extractor_graph(kit, make_node, files))

    error = run.outcome.error
    assert error is not None and error.code == "extract_failed"
    assert error.message.startswith("깨진.pdf: PdfReadError")


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ([ref("사진.png", "image/png")], "unsupported_file_type"),
        ([ref(f"{index}.txt", "text/plain") for index in range(21)], "file_limit"),
        ("파일 아님", "type_mismatch"),
    ],
)
async def test_input_errors(kit: Any, make_node: Node, env: Env, files: Any, code: str) -> None:
    run = await env.run(kit, extractor_graph(kit, make_node, files))

    assert run.outcome.error is not None and run.outcome.error.code == code


async def test_pdf_without_a_cache_folder_is_unavailable(
    kit: Any, make_node: Node, env: Env
) -> None:
    files = [env.files.add("a.pdf", b"x", "application/pdf")]

    run = await env.run(kit, extractor_graph(kit, make_node, files), cache=False)

    assert run.outcome.error is not None and run.outcome.error.code == "extractor_unavailable"


async def test_null_input_gives_empty_text(kit: Any, make_node: Node, env: Env) -> None:
    run = await env.run(kit, extractor_graph(kit, make_node, None))

    assert run.outcome.outputs == {"text": "", "texts": []}


def test_validation_requires_a_file_variable(kit: Any, make_node: Node) -> None:
    graph = kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            make_node("document_extractor_1", "document_extractor", {"file_var": "start.q"}),
            make_node("end_1", "end"),
        ],
        ["start>document_extractor_1", "document_extractor_1>end_1"],
    )

    found = [(issue.code, issue.field) for issue in validate(graph, "workflow")]

    assert found == [("type_mismatch", "file_var")]


def test_kinds_and_decoding() -> None:
    assert file_kind(ref("A.DOCX")) == "docx"
    assert file_kind(ref("noext", "text/csv")) == "csv"
    assert file_kind(ref("data.bin", "text/x-log")) == "text"
    assert file_kind(ref(".pdf", "image/png")) is None
    assert decode_text("한글".encode("cp949"), complete=True) == "한글"
    assert decode_text("﻿머리".encode(), complete=True) == "머리"
    # 읽다 만 끝 글자는 버린다.
    assert decode_text("가나".encode()[:-1], complete=False) == "가"
