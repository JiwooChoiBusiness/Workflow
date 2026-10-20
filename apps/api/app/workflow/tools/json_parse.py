# pyright: strict
"""json_parse 도구. JSON 글을 값으로 바꾸고, 경로를 주면 그 안의 값만 꺼낸다.

경로는 변수 선택자와 같은 꼴이다(items[0].name). 앞의 $·$.는 떼고 읽는다. 경로에 값이 없으면 오류가
아니라 found=false, value=null이다. JSON이 아니면 invalid_json으로 실패한다.
"""

import json
import math
import re
from collections.abc import Mapping
from typing import Any, cast

from app.workflow.nodes.base import NodeContext, NodeFailure, OutputSpec
from app.workflow.tools.base import Tool, ToolParam, ToolResult
from app.workflow.variables import describe

MAX_PATH_CHARS = 512
_STEP = re.compile(r"\.?([^.\[\]]+)|\[(-?\d+)\]")


def parse_path(path: str) -> list[str | int]:
    """빈 경로는 값 전체다. 읽지 못하면 ValueError."""
    text = path.strip()
    if text.startswith("$"):
        text = text[1:]
    steps: list[str | int] = []
    position = 0
    while position < len(text):
        match = _STEP.match(text, position)
        if match is None or match.end() == position:
            raise ValueError(f"경로를 읽지 못했다: {path!r}")
        key, index = match.groups()
        steps.append(int(index) if index is not None else key)
        position = match.end()
    return steps


def _reject_constant(name: str) -> Any:
    # NaN·Infinity는 표준 JSON이 아니고 이벤트로 내보내면 브라우저 JSON.parse가 깨진다.
    raise ValueError(f"{name}은(는) JSON 값이 아니다")


def _finite_float(text: str) -> float:
    number = float(text)
    if math.isinf(number):
        raise ValueError(f"{text[:40]}은(는) 너무 큰 수다")
    return number


def _walk(value: Any, steps: list[str | int]) -> tuple[Any, bool]:
    for step in steps:
        if isinstance(step, int):
            if not isinstance(value, list):
                return None, False
            items = cast(list[Any], value)
            if not -len(items) <= step < len(items):
                return None, False
            value = items[step]
        elif isinstance(value, dict) and step in value:
            value = cast(dict[str, Any], value)[step]
        else:
            return None, False
    return value, True


class JsonParseTool(Tool):
    name = "json_parse"
    label = "JSON 읽기"
    description = "JSON 글을 값으로 바꾼다. 경로를 주면 그 안의 값만 꺼낸다"
    params = (
        ToolParam("text", "string", "JSON 글", "읽을 JSON 글", required=True),
        ToolParam(
            "path",
            "string",
            "경로",
            "꺼낼 값의 경로(예: items[0].name). 비우면 전체",
            default="",
            max_length=MAX_PATH_CHARS,
        ),
    )
    outputs = (
        OutputSpec("value", "any"),
        OutputSpec("text", "string"),
        OutputSpec("type", "string"),
        OutputSpec("found", "boolean"),
    )

    def check(self, name: str, value: Any) -> str | None:
        if name == "path" and isinstance(value, str):
            try:
                parse_path(value)
            except ValueError as exc:
                return str(exc)
        return None

    async def run(self, ctx: NodeContext, args: Mapping[str, Any]) -> ToolResult:
        source = str(args.get("text") or "")
        try:
            steps = parse_path(str(args.get("path") or ""))
        except ValueError as exc:
            raise NodeFailure("invalid_path", str(exc)) from None
        try:
            parsed: Any = json.loads(
                source, parse_constant=_reject_constant, parse_float=_finite_float
            )
        except ValueError as exc:
            raise NodeFailure("invalid_json", f"JSON이 아니다: {exc}") from None
        value, found = _walk(parsed, steps)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return ToolResult(
            outputs={"value": value, "text": text, "type": describe(value), "found": found},
            process={"path": steps, "found": found},
        )
