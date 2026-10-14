"""http_request 노드(2단계 설계 2.3, 8, 9.2, M13).

HTTP는 1단계 HttpClient에 가짜 DNS와 httpx.MockTransport를 붙여 네트워크 없이 돌리고, 런 작업 폴더는
실제 로컬 백엔드(임시 DATA_DIR/wf-workspaces)다. 파일 포트는 받은 파일 경로만 기록한다.
"""

import json
import socket
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.sandbox.allowlist import Allowlist
from app.sandbox.http_client import HttpClient
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.workflow.graph import validate

Node = Callable[..., dict[str, Any]]
Handler = Callable[[httpx.Request], httpx.Response]
PUBLIC = "93.184.216.34"
MB = 1024 * 1024


class Files:
    """FilePort. 옮긴 파일의 원래 경로와 크기를 기록하고 wf_file 참조를 돌려준다."""

    def __init__(self) -> None:
        self.saved: list[tuple[Path, str, str | None, int]] = []

    async def save_output(self, source: Path, *, filename: str, mime: str | None) -> dict[str, Any]:
        size = source.stat().st_size
        self.saved.append((source, filename, mime, size))
        return {
            "file_id": str(uuid.uuid4()),
            "filename": filename,
            "mime": mime or "application/octet-stream",
            "size_bytes": size,
            "storage_key": "wf-files/w/x",
            "source": "wf_file",
        }

    async def read_bytes(self, ref: Mapping[str, Any], *, max_bytes: int) -> bytes:
        raise AssertionError("쓰지 않는다")


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.seen: list[httpx.Request] = []
        self.handler: Handler = lambda request: httpx.Response(200, text="ok")
        allowlist = tmp_path / "allowlist.txt"
        allowlist.write_text("example.com\n.example.com\n", encoding="utf-8")

        async def resolver(host: str, port: int) -> list[str]:
            if host not in ("example.com", "api.example.com", "evil.test"):
                raise socket.gaierror(socket.EAI_NONAME, "not found")
            return [PUBLIC]

        def route(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return self.handler(request)

        self.http = HttpClient(
            Allowlist(allowlist),
            max_body_bytes=5 * MB,
            resolver=resolver,
            transport=httpx.MockTransport(route),
        )
        settings = Settings.model_validate({"data_dir": tmp_path / "data"})
        self.workspaces = tmp_path / "data" / "wf-workspaces"
        self.sandbox = LocalWorkspaceBackend(settings, workspaces_dir=self.workspaces)
        self.files = Files()
        self.run_id = uuid.uuid4()

    async def run(self, kit: Any, graph: Any, **options: Any) -> Any:
        ports = kit.ports(sandbox=self.sandbox, http=self.http, files=self.files)
        return await kit.run(graph, ports=ports, run_id=self.run_id, **options)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    instance = Env(tmp_path)
    yield instance
    await instance.http.aclose()


def http_graph(kit: Any, make_node: Node, config: dict[str, Any], **extra: Any) -> Any:
    names = ["status_code", "headers", "body", "json", "files"]
    return kit.graph(
        [
            make_node("start", "start", {"fields": [{"name": "q", "type": "string"}]}),
            make_node("http_request_1", "http_request", config, **extra),
            make_node(
                "end_1",
                "end",
                {"outputs": [{"name": n, "var": f"http_request_1.{n}"} for n in names]},
            ),
        ],
        ["start>http_request_1", "http_request_1>end_1"],
    )


def node_record(run: Any) -> Any:
    record = next(
        e.node_run
        for e in run.events
        if e.type == "node.completed" and e.data["node_id"] == "http_request_1"
    )
    assert record is not None
    return record


async def test_get_with_params_auth_and_json_response(kit: Any, make_node: Node, env: Env) -> None:
    env.handler = lambda request: httpx.Response(
        200, json={"items": [1, 2], "q": request.url.params["q"]}
    )
    graph = http_graph(
        kit,
        make_node,
        {
            "url": "https://api.example.com/search?lang=ko",
            "params": [{"name": "q", "value": "{{ start.q }}"}],
            "headers": [{"name": "X-Trace", "value": "t-1"}],
            "auth": {"type": "bearer", "value": "{{ env.TOKEN }}"},
        },
    )

    run = await env.run(
        kit,
        graph,
        inputs={"q": "부산 날씨"},
        env={"TOKEN": "secret-token-123"},
        secret_env=["TOKEN"],
    )

    assert run.outcome.status == "succeeded"
    [request] = env.seen
    assert request.url.params["q"] == "부산 날씨" and request.url.params["lang"] == "ko"
    assert request.headers["authorization"] == "Bearer secret-token-123"
    assert request.headers["x-trace"] == "t-1"
    outputs = run.outcome.outputs
    assert outputs["status_code"] == 200
    assert outputs["json"] == {"items": [1, 2], "q": "부산 날씨"}
    assert json.loads(outputs["body"]) == outputs["json"] and outputs["files"] == []
    recorded = node_record(run)
    assert recorded.inputs["headers"]["Authorization"] == "***"
    assert "secret-token-123" not in json.dumps(recorded.model_dump(mode="json"))
    assert recorded.process["status"] == 200


async def test_error_statuses_are_outputs_not_failures(kit: Any, make_node: Node, env: Env) -> None:
    env.handler = lambda request: httpx.Response(404, text="없다")
    graph = http_graph(kit, make_node, {"url": "https://example.com/missing"})

    run = await env.run(kit, graph)

    assert run.outcome.status == "succeeded"
    assert (run.outcome.outputs["status_code"], run.outcome.outputs["body"]) == (404, "없다")


async def test_hosts_outside_the_allowlist_are_refused(kit: Any, make_node: Node, env: Env) -> None:
    graph = http_graph(
        kit,
        make_node,
        {"url": "https://evil.test/x"},
        retry={"max_attempts": 2, "interval_ms": 0},
    )

    run = await env.run(kit, graph)

    assert run.outcome.status == "failed"
    assert run.outcome.error is not None
    assert (run.outcome.error.code, run.outcome.error.retryable) == (
        "http_host_not_allowed",
        False,
    )
    assert env.seen == [] and run.started().count("http_request_1") == 1
    assert node_record(run).inputs["url"] == "https://evil.test/x"


async def test_large_text_goes_to_the_run_folder_and_files(
    kit: Any, make_node: Node, env: Env
) -> None:
    text = ("가나다라마바사아자차" * 70_000)[: 2 * MB // 3]
    env.handler = lambda request: httpx.Response(
        200,
        content=text.encode("utf-8"),
        headers={"content-type": "text/plain; charset=utf-8"},
    )
    graph = http_graph(kit, make_node, {"url": "https://example.com/report.txt"})

    run = await env.run(kit, graph)

    assert run.outcome.status == "succeeded"
    body = run.outcome.outputs["body"]
    assert 0 < len(body.encode("utf-8")) <= MB and text.startswith(body)
    [saved] = env.files.saved
    source, filename, mime, size = saved
    assert source.is_relative_to(env.workspaces / str(env.run_id) / "downloads")
    assert (filename, mime, size) == ("report.txt", "text/plain", len(text.encode("utf-8")))
    [file] = run.outcome.outputs["files"]
    assert file["filename"] == "report.txt"
    record = node_record(run)
    assert record.process["body_truncated"] is True
    assert any(w.startswith("body_truncated") for w in record.warnings)
    assert run.outcome.outputs["json"] is None


async def test_file_names_get_the_downloaded_extension(kit: Any, make_node: Node, env: Env) -> None:
    env.handler = lambda request: httpx.Response(
        200,
        content=json.dumps({"releases": ["x" * 1000] * 1500}).encode(),
        headers={"content-type": "application/json"},
    )
    graph = http_graph(kit, make_node, {"url": "https://example.com/pypi/numpy/json"})

    run = await env.run(kit, graph)

    [(source, filename, _, _)] = env.files.saved
    assert source.suffix == ".json" and filename == "json.json"
    assert run.outcome.outputs["files"][0]["filename"] == "json.json"


async def test_mid_sized_text_stays_in_the_body(kit: Any, make_node: Node, env: Env) -> None:
    data = json.dumps({"rows": ["x" * 100] * 3000})
    env.handler = lambda request: httpx.Response(
        200, content=data.encode(), headers={"content-type": "application/json"}
    )
    graph = http_graph(kit, make_node, {"url": "https://example.com/rows"})

    run = await env.run(kit, graph)

    assert len(data) > 64 * 1024
    assert run.outcome.outputs["body"] == data
    assert run.outcome.outputs["json"] == {"rows": ["x" * 100] * 3000}
    assert env.files.saved == [] and run.outcome.outputs["files"] == []


async def test_binary_responses_become_files(kit: Any, make_node: Node, env: Env) -> None:
    env.handler = lambda request: httpx.Response(
        200,
        content=b"\x89PNG" + bytes(5000),
        headers={
            "content-type": "image/png",
            "content-disposition": "attachment; filename*=UTF-8''%EC%A7%80%EB%8F%84.png",
        },
    )
    graph = http_graph(kit, make_node, {"url": "https://example.com/map"})

    run = await env.run(kit, graph)

    assert run.outcome.outputs["body"] == "" and run.outcome.outputs["json"] is None
    [(source, filename, mime, size)] = env.files.saved
    assert (filename, mime, size) == ("지도.png", "image/png", 5004)
    assert source.read_bytes().startswith(b"\x89PNG")


async def test_responses_over_the_limits_fail(kit: Any, make_node: Node, env: Env) -> None:
    env.handler = lambda request: httpx.Response(
        200, content=bytes(6 * MB), headers={"content-type": "application/octet-stream"}
    )
    graph = http_graph(kit, make_node, {"url": "https://example.com/huge"})

    huge = await env.run(kit, graph)

    assert huge.outcome.error is not None
    assert huge.outcome.error.code == "http_response_too_large"
    assert env.files.saved == []

    env.handler = lambda request: httpx.Response(200, text="x" * 3000)
    small_limit = http_graph(
        kit, make_node, {"url": "https://example.com/a", "max_body_bytes": 1000}
    )
    limited = await env.run(kit, small_limit)

    assert limited.outcome.error is not None
    assert limited.outcome.error.code == "http_response_too_large"


async def test_connection_errors_are_retried(kit: Any, make_node: Node, env: Env) -> None:
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, text="다시 됐다")

    env.handler = flaky
    graph = http_graph(
        kit, make_node, {"url": "https://example.com/"}, retry={"max_attempts": 1, "interval_ms": 0}
    )

    run = await env.run(kit, graph)

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs["body"] == "다시 됐다"
    [retrying] = run.of("node.retrying")
    assert retrying["reason"] == "http_failed"


async def test_json_and_form_bodies(kit: Any, make_node: Node, env: Env) -> None:
    env.handler = lambda request: httpx.Response(200, text=request.content.decode())
    as_json = http_graph(
        kit,
        make_node,
        {
            "method": "POST",
            "url": "https://example.com/echo",
            "body": {"type": "json", "content": '{"q": {{ start.q | tojson }}}'},
        },
    )
    as_form = http_graph(
        kit,
        make_node,
        {
            "method": "POST",
            "url": "https://example.com/echo",
            "body": {"type": "form", "content": [{"name": "q", "value": "{{ start.q }}"}]},
        },
    )
    broken = http_graph(
        kit,
        make_node,
        {
            "method": "POST",
            "url": "https://example.com/echo",
            "body": {"type": "json", "content": '{"q": {{ start.q }}}'},
        },
    )

    first = await env.run(kit, as_json, inputs={"q": '따옴표 "포함"'})
    second = await env.run(kit, as_form, inputs={"q": "a&b=c"})
    third = await env.run(kit, broken, inputs={"q": "글"})

    assert json.loads(first.outcome.outputs["body"]) == {"q": '따옴표 "포함"'}
    assert env.seen[0].headers["content-type"] == "application/json"
    assert second.outcome.outputs["body"] == "q=a%26b%3Dc"
    assert env.seen[1].headers["content-type"] == "application/x-www-form-urlencoded"
    assert third.outcome.error is not None and third.outcome.error.code == "invalid_body"
    assert len(env.seen) == 2


def test_validation_rules(kit: Any, make_node: Node) -> None:
    def issues(config: dict[str, Any]) -> list[tuple[str, str | None]]:
        graph = http_graph(kit, make_node, config)
        return [
            (i.code, i.field) for i in validate(graph, "workflow") if i.node_id == "http_request_1"
        ]

    assert issues({"url": "https://example.com/{{ start.q }}"}) == []
    assert issues({"url": " "}) == [("invalid_config", "url")]
    assert issues({"url": "https://x", "body": {"type": "file"}}) == [
        ("unsupported_body_type", "body.type")
    ]
    assert issues({"url": "https://x", "auth": {"type": "header", "value": "k"}}) == [
        ("invalid_config", "auth.header")
    ]
    assert issues({"url": "https://x", "headers": [{"name": "Bad Name", "value": "v"}]}) == [
        ("invalid_config", "headers[0].name")
    ]
    assert issues({"url": "https://x", "timeout_sec": 301})[0] == ("invalid_config", "timeout_sec")
    assert issues({"url": "{{ start.nope }}"}) == [("unknown_variable", "url")]
