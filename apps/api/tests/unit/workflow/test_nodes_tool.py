"""tool 노드와 내장 도구 넷(2단계 설계 2.4, 9.2, 9.5).

current_time(시간대·형식), json_parse(경로·잘못된 JSON), regex_extract(그룹·플래그·상한),
web_fetch(1단계 HttpClient의 허용 목록·가짜 DNS·MockTransport, 실제 로컬 백엔드 런 폴더)를 돌리고,
매개변수 세 방식(template·var·const), 검증 규칙, 오류 정책, 노드 타입 목록의 도구 설명을 본다.
"""

import socket
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from app.config import Settings
from app.sandbox.allowlist import Allowlist
from app.sandbox.http_client import HttpClient
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.workflow.graph import validate
from app.workflow.nodes.base import NodeFailure
from app.workflow.tools import describe_tools
from app.workflow.tools.current_time import TimezoneError, resolve_timezone
from app.workflow.tools.regex_extract import RegexExtractTool

Node = Callable[..., dict[str, Any]]
PUBLIC = "93.184.216.34"
# 2026-10-15 01:02:03 UTC(목요일).
NOW = datetime(2026, 10, 15, 1, 2, 3, tzinfo=UTC)


def tool_graph(kit: Any, make_node: Node, tool: str, params: dict[str, Any], **extra: Any) -> Any:
    from app.workflow.tools import get_tool

    spec = get_tool(tool)
    names = [output.name for output in spec.outputs] if spec is not None else []
    return kit.graph(
        [
            make_node(
                "start",
                "start",
                {"fields": [{"name": "text", "type": "string"}, {"name": "n", "type": "number"}]},
            ),
            make_node("tool_1", "tool", {"tool": tool, "params": params}, **extra),
            make_node(
                "end_1",
                "end",
                {"outputs": [{"name": name, "var": f"tool_1.{name}"} for name in names]},
            ),
        ],
        ["start>tool_1", "tool_1>end_1"],
    )


def fixed_ports(kit: Any, **parts: Any) -> Any:
    ports = kit.ports(**parts)
    ports.now = lambda: NOW
    return ports


def record_of(run: Any) -> Any:
    return next(
        event.node_run
        for event in run.events
        if event.type == "node.completed" and event.data["node_id"] == "tool_1"
    )


@pytest.mark.parametrize(
    ("zone", "fmt", "text", "label"),
    [
        ("UTC", "", "2026-10-15T01:02:03+00:00", "UTC"),
        ("+09:00", "%Y년 %m월 %d일 %H:%M", "2026년 10월 15일 10:02", "+09:00"),
        ("Asia/Seoul", "%A", "Thursday", "Asia/Seoul"),
        ("UTC-5", "%H시", "20시", "-05:00"),
    ],
)
async def test_current_time(
    kit: Any, make_node: Node, zone: str, fmt: str, text: str, label: str
) -> None:
    graph = tool_graph(
        kit,
        make_node,
        "current_time",
        {"timezone": {"mode": "const", "value": zone}, "format": {"mode": "const", "value": fmt}},
    )

    run = await kit.run(graph, ports=fixed_ports(kit))

    assert run.outcome.status == "succeeded", run.outcome.error
    outputs = run.outcome.outputs
    assert outputs["text"] == text
    assert outputs["timezone"] == label
    assert outputs["timestamp"] == int(NOW.timestamp())
    assert outputs["iso"].startswith(outputs["date"])


async def test_current_time_defaults_to_local_time(kit: Any, make_node: Node) -> None:
    graph = tool_graph(kit, make_node, "current_time", {})

    run = await kit.run(graph, ports=fixed_ports(kit))

    local = NOW.astimezone()
    assert run.outcome.outputs["iso"] == local.isoformat(timespec="seconds")


def test_timezone_resolution() -> None:
    assert resolve_timezone("utc")[1] == "UTC"
    assert resolve_timezone("+0930")[1] == "+09:30"
    with pytest.raises(TimezoneError):
        resolve_timezone("Mars/Base")
    with pytest.raises(TimezoneError):
        resolve_timezone("+15:00")


async def test_json_parse_reads_a_path_from_a_variable(kit: Any, make_node: Node) -> None:
    graph = tool_graph(
        kit,
        make_node,
        "json_parse",
        {"text": {"mode": "var", "value": "start.text"}, "path": {"value": "$.items[1].name"}},
    )
    source = '{"items": [{"name": "가"}, {"name": "나", "n": 2}]}'

    run = await kit.run(graph, inputs={"text": source})

    assert run.outcome.outputs == {"value": "나", "text": "나", "type": "string", "found": True}
    record = record_of(run)
    assert record.inputs == {
        "tool": "json_parse",
        "params": {"text": source, "path": "$.items[1].name"},
    }


async def test_json_parse_missing_path_and_whole_value(kit: Any, make_node: Node) -> None:
    graph = tool_graph(
        kit,
        make_node,
        "json_parse",
        {"text": {"mode": "template", "value": "{{ start.text }}"}, "path": {"value": "x.y"}},
    )

    run = await kit.run(graph, inputs={"text": "[1, 2]"})

    assert run.outcome.outputs == {"value": None, "text": "null", "type": "null", "found": False}


async def test_invalid_json_fails_or_falls_back_to_default(kit: Any, make_node: Node) -> None:
    params = {"text": {"mode": "var", "value": "start.text"}}
    graph = tool_graph(kit, make_node, "json_parse", params)

    failed = await kit.run(graph, inputs={"text": "{깨진"})

    assert failed.outcome.status == "failed"
    assert failed.outcome.error is not None and failed.outcome.error.code == "invalid_json"

    fallback = tool_graph(
        kit,
        make_node,
        "json_parse",
        params,
        error={"strategy": "default", "default_outputs": {"text": "없음", "found": False}},
    )
    run = await kit.run(fallback, inputs={"text": "NaN"})

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs["text"] == "없음"


async def test_regex_extract_groups_and_flags(kit: Any, make_node: Node) -> None:
    graph = tool_graph(
        kit,
        make_node,
        "regex_extract",
        {
            "text": {"mode": "var", "value": "start.text"},
            "pattern": {"value": r"주문\s*(?P<no>\d+)"},
            "flags": {"value": "i"},
            "group": {"value": "no"},
        },
    )

    run = await kit.run(graph, inputs={"text": "주문 12, 주문34 그리고 주문 x"})

    assert run.outcome.outputs == {
        "text": "12",
        "matches": ["12", "34"],
        "count": 2,
        "found": True,
    }


async def test_regex_extract_caps_matches_and_rejects_large_input(
    kit: Any, make_node: Node
) -> None:
    params = {"text": {"mode": "var", "value": "start.text"}, "pattern": {"value": "a"}}
    graph = tool_graph(kit, make_node, "regex_extract", params)

    run = await kit.run(graph, inputs={"text": "a" * 1500})

    assert run.outcome.outputs["count"] == 1000
    assert any(warning.startswith("matches_capped") for warning in record_of(run).warnings)
    # 변수는 1MB를 넘지 못하므로 입력 상한은 도구를 바로 불러 본다.
    with pytest.raises(NodeFailure) as caught:
        await RegexExtractTool().run(cast(Any, _Ctx()), {"text": "가" * 400_000, "pattern": "가"})
    assert caught.value.code == "variable_limit"
    with pytest.raises(NodeFailure) as caught:
        await RegexExtractTool().run(
            cast(Any, _Ctx()), {"text": "x", "pattern": "(a)", "group": "2"}
        )
    assert caught.value.code == "invalid_group"


class _Ctx:
    def warn(self, message: str) -> None:
        pass


def test_validation_rules(kit: Any, make_node: Node) -> None:
    unknown = tool_graph(kit, make_node, "translate", {})
    assert [(i.code, i.field) for i in validate(unknown, "workflow")] == [("unknown_tool", "tool")]

    graph = tool_graph(
        kit,
        make_node,
        "regex_extract",
        {
            "pattern": {"value": "(" * 513},
            "flags": {"mode": "const", "value": "q"},
            "group": {"mode": "var", "value": "start.nothing"},
            "extra": {"value": "x"},
        },
    )
    found = {(issue.code, issue.field) for issue in validate(graph, "workflow")}

    assert ("param_required", "params.text.value") in found
    assert ("invalid_config", "params.pattern.value") in found
    assert ("invalid_config", "params.flags.value") in found
    assert ("unknown_variable", "params.group.value") in found
    assert ("unknown_param", "params.extra") in found

    zone = tool_graph(
        kit,
        make_node,
        "current_time",
        {
            "timezone": {"mode": "const", "value": "Mars/Base"},
            "format": {"value": "{{ start.text }}"},
        },
    )
    found = {(issue.code, issue.field) for issue in validate(zone, "workflow")}
    # 변수가 든 템플릿은 실행해야 값을 안다.
    assert found == {("invalid_config", "params.timezone.value")}

    web = tool_graph(kit, make_node, "web_fetch", {"url": {"value": "ftp://example.com"}})
    found = {(issue.code, issue.field) for issue in validate(web, "workflow")}
    assert found == {("invalid_config", "params.url.value")}


def test_catalog_lists_tools_with_param_schemas() -> None:
    tools = {tool.name: tool for tool in describe_tools()}

    assert set(tools) == {"current_time", "json_parse", "regex_extract", "web_fetch"}
    regex = tools["regex_extract"]
    assert regex.params_schema["required"] == ["text", "pattern"]
    assert regex.params_schema["properties"]["pattern"]["maxLength"] == 512
    assert [param.name for param in regex.params] == ["text", "pattern", "flags", "group"]
    assert [output.name for output in tools["web_fetch"].outputs] == [
        "text",
        "title",
        "url",
        "status_code",
        "content_type",
    ]
    assert tools["current_time"].params[0].default == "local"


class Web:
    def __init__(self, tmp_path: Path) -> None:
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda request: httpx.Response(
            200, text="ok"
        )
        allowlist = tmp_path / "allowlist.txt"
        allowlist.write_text("example.com\n", encoding="utf-8")

        async def resolver(host: str, port: int) -> list[str]:
            if host not in ("example.com", "blocked.test"):
                raise socket.gaierror(socket.EAI_NONAME, "not found")
            return [PUBLIC]

        self.http = HttpClient(
            Allowlist(allowlist),
            max_body_bytes=5 * 1024 * 1024,
            resolver=resolver,
            transport=httpx.MockTransport(lambda request: self.handler(request)),
        )
        settings = Settings.model_validate({"data_dir": tmp_path / "data"})
        self.sandbox = LocalWorkspaceBackend(
            settings, workspaces_dir=tmp_path / "data" / "wf-workspaces"
        )

    async def run(self, kit: Any, graph: Any, **options: Any) -> Any:
        ports = kit.ports(sandbox=self.sandbox, http=self.http)
        return await kit.run(graph, ports=ports, run_id=uuid.uuid4(), **options)


@pytest.fixture
async def web(tmp_path: Path) -> AsyncIterator[Web]:
    instance = Web(tmp_path)
    yield instance
    await instance.http.aclose()


PAGE = """<html><head><title> 공지 사항 </title><style>p{color:red}</style>
<script>alert(1)</script></head><body><h1>제목</h1><p>첫 문단<br>둘째 줄</p>
<ul><li>하나</li><li>둘</li></ul><noscript>꺼짐</noscript></body></html>"""


async def test_web_fetch_keeps_only_the_text_of_a_page(kit: Any, make_node: Node, web: Web) -> None:
    web.handler = lambda request: httpx.Response(
        200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"}
    )
    graph = tool_graph(
        kit,
        make_node,
        "web_fetch",
        {"url": {"value": "https://example.com/news?q={{ start.text }}"}},
    )

    run = await web.run(kit, graph, inputs={"text": "공지"})

    assert run.outcome.status == "succeeded", run.outcome.error
    outputs = run.outcome.outputs
    assert outputs["title"] == "공지 사항"
    assert outputs["text"] == "제목\n\n첫 문단\n둘째 줄\n\n- 하나\n- 둘"
    assert outputs["status_code"] == 200
    assert outputs["url"].startswith("https://example.com/news?q=")


async def test_web_fetch_reads_large_pages_from_the_run_folder_and_cuts(
    kit: Any, make_node: Node, web: Web
) -> None:
    body = "<p>" + "가나다라마" * 30_000 + "</p>"
    web.handler = lambda request: httpx.Response(
        404, text=body, headers={"content-type": "text/html"}
    )
    graph = tool_graph(
        kit,
        make_node,
        "web_fetch",
        {"url": {"value": "https://example.com/big"}, "max_length": {"mode": "const", "value": 10}},
    )

    run = await web.run(kit, graph)

    assert run.outcome.outputs["text"] == "가나다라마가나다라마"
    assert run.outcome.outputs["status_code"] == 404
    warnings = record_of(run).warnings
    assert any(warning.startswith("text_truncated") for warning in warnings)
    assert any(warning.startswith("http_status") for warning in warnings)


async def test_web_fetch_refuses_hosts_outside_the_allowlist_and_binary(
    kit: Any, make_node: Node, web: Web
) -> None:
    blocked = tool_graph(kit, make_node, "web_fetch", {"url": {"value": "https://blocked.test/"}})

    run = await web.run(kit, blocked)

    assert run.outcome.error is not None and run.outcome.error.code == "http_host_not_allowed"

    web.handler = lambda request: httpx.Response(
        200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"}
    )
    binary = tool_graph(kit, make_node, "web_fetch", {"url": {"value": "https://example.com/a"}})
    run = await web.run(kit, binary)

    assert run.outcome.error is not None
    assert run.outcome.error.code == "unsupported_content_type"


async def test_web_fetch_connection_errors_can_branch(kit: Any, make_node: Node, web: Web) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    web.handler = broken
    graph = tool_graph(
        kit,
        make_node,
        "web_fetch",
        {"url": {"value": "https://example.com/"}},
        error={"strategy": "default", "default_outputs": {"text": "못 가져옴"}},
    )

    run = await web.run(kit, graph)

    assert run.outcome.status == "succeeded"
    assert run.outcome.outputs["text"] == "못 가져옴"
    assert record_of(run).error.code == "http_failed"
