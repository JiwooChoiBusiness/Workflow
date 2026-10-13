"""내장 도구의 스키마·검증·출력 다듬기(설계 4.4, 4.6)."""

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from app.agent.tools import BUILTIN_TOOLS, tools_for_groups
from app.agent.tools.base import (
    EVENT_OUTPUT_BYTES,
    MODEL_OUTPUT_BYTES,
    OutputCapture,
    ToolDefaults,
    excerpt,
    model_schema,
    parse_arguments,
    validate_tool_input,
)
from app.agent.tools.http_request import HttpRequestTool
from app.agent.tools.shell import ShellTool

DESIGN_TOOLS = {
    "shell": "shell",
    "run_python": "shell",
    "install_packages": "shell",
    "read_file": "files",
    "write_file": "files",
    "edit_file": "files",
    "list_files": "files",
    "http_request": "http",
    "ask_user": "ask_user",
}


def test_builtin_tools_match_the_design_table() -> None:
    assert {tool.name: tool.group for tool in BUILTIN_TOOLS} == DESIGN_TOOLS
    assert [tool.name for tool in tools_for_groups(["files"])] == [
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
    ]
    assert tools_for_groups([]) == []


@pytest.mark.parametrize("tool", BUILTIN_TOOLS, ids=lambda tool: tool.name)
def test_schemas_are_valid_and_closed(tool: Any) -> None:
    Draft202012Validator.check_schema(tool.input_schema)
    assert tool.input_schema["type"] == "object"
    assert tool.input_schema["additionalProperties"] is False
    assert tool.description


@pytest.mark.parametrize("tool", BUILTIN_TOOLS, ids=lambda tool: tool.name)
def test_model_schema_drops_keys_the_compat_layer_may_reject(tool: Any) -> None:
    sent = tool.spec().input_schema

    def keys(node: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            for key, value in node.items():
                found.add(key)
                if key == "properties":
                    for sub in value.values():
                        found |= keys(sub)
                elif key == "items":
                    found |= keys(value)
        return found

    assert not keys(sent) & {"additionalProperties", "minLength", "maxLength", "pattern"}
    assert sent.get("required") == tool.input_schema.get("required")


def test_model_schema_keeps_enum_and_bounds() -> None:
    sent = model_schema(HttpRequestTool.input_schema)

    assert sent["properties"]["method"]["enum"] == [
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    ]
    assert sent["properties"]["timeout_sec"]["maximum"] == 300


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({}, "'command' is a required property"),
        ({"command": ""}, "command"),
        ({"command": "dir", "timeout_sec": 901}, "timeout_sec"),
        ({"command": "dir", "extra": 1}, "Additional properties"),
    ],
)
def test_invalid_input_is_reported(data: dict[str, Any], fragment: str) -> None:
    error = validate_tool_input(ShellTool(), data)

    assert error is not None and error.startswith("INVALID_INPUT")
    assert fragment in error


def test_valid_input_passes() -> None:
    assert validate_tool_input(ShellTool(), {"command": "dir", "timeout_sec": 30}) is None


def test_parse_arguments() -> None:
    assert parse_arguments('{"a": 1}') == ({"a": 1}, None)
    assert parse_arguments("") == ({}, None)
    data, error = parse_arguments('{"a": ')
    assert data == {} and error is not None and error.startswith("INVALID_JSON")
    assert parse_arguments("[1]")[1] is not None


def test_timeouts_for_approval_card() -> None:
    defaults = ToolDefaults(timeout_sec=120, max_timeout_sec=900)

    assert ShellTool().timeout_for({"command": "x"}, defaults) == 120
    assert ShellTool().timeout_for({"command": "x", "timeout_sec": 5000}, defaults) == 900
    assert HttpRequestTool().timeout_for({}, defaults) == 60


def test_http_display_masks_credentials() -> None:
    shown = HttpRequestTool().display_input(
        {
            "method": "POST",
            "url": "https://x",
            "headers": {"Authorization": "Bearer a", "Accept": "json", "Cookie": "c"},
        }
    )

    assert shown["headers"] == {"Authorization": "***", "Accept": "json", "Cookie": "***"}


def test_excerpt_keeps_head_and_tail() -> None:
    text = "앞" * 20_000 + "끝"

    cut = excerpt(text)

    assert cut.startswith("앞") and cut.endswith("끝")
    assert "bytes omitted" in cut
    assert len(cut.encode()) <= MODEL_OUTPUT_BYTES + 64
    assert excerpt("짧다") == "짧다"


class Sink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    async def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def close(self) -> str:
        self.closed = True
        return "tool-outputs/r/c.txt"

    async def abort(self) -> None:
        self.data.clear()


async def test_capture_spills_after_256kb_and_keeps_a_small_window() -> None:
    sink = Sink()

    async def opener() -> Sink:
        return sink

    capture = OutputCapture(opener)
    chunk = "가나다라마바사\n" * 1000
    total = ""
    while len(total.encode()) <= EVENT_OUTPUT_BYTES * 2:
        await capture.feed(chunk)
        total += chunk

    key = await capture.close()

    assert key == "tool-outputs/r/c.txt" and sink.closed
    assert bytes(sink.data) == total.encode()
    assert capture.total_bytes == len(total.encode())
    assert "bytes omitted" in capture.event_text()
    assert len(capture.model_text().encode()) <= MODEL_OUTPUT_BYTES + 64


async def test_small_capture_keeps_everything_for_the_event() -> None:
    capture = OutputCapture()
    await capture.feed("줄1\n")
    await capture.feed("줄2\n")

    assert capture.event_text() == "줄1\n줄2\n"
    assert await capture.close() is None
