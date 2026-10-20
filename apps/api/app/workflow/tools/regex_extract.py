# pyright: strict
"""regex_extract 도구. 글에서 정규식에 맞는 부분을 꺼낸다.

표준 re라 백트래킹 상한이 없다. 그래서 if_else의 regex 연산과 같이 패턴 512자, 입력 1MB 상한만 둔다
(2단계 설계 9.5). 결과 목록은 1,000개까지 모은다.
"""

import re
from collections.abc import Mapping
from typing import Any

from app.workflow.nodes.base import NodeContext, NodeFailure, OutputSpec
from app.workflow.nodes.if_else import MAX_REGEX_INPUT_BYTES, MAX_REGEX_PATTERN_CHARS
from app.workflow.tools.base import Tool, ToolParam, ToolResult

MAX_MATCHES = 1000
_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


def _flags(text: str) -> int:
    value = 0
    for letter in text.strip().lower():
        if letter not in _FLAGS:
            raise ValueError(f"플래그는 i·m·s만 쓴다: {letter}")
        value |= _FLAGS[letter]
    return value


def _group(text: str) -> int | str:
    stripped = text.strip()
    if not stripped:
        return 0
    return int(stripped) if stripped.isdigit() else stripped


class RegexExtractTool(Tool):
    name = "regex_extract"
    label = "정규식 추출"
    description = "글에서 정규식에 맞는 부분을 꺼낸다"
    params = (
        ToolParam("text", "string", "글", "찾을 대상 글(1MB까지)", required=True),
        ToolParam(
            "pattern",
            "string",
            "정규식",
            "파이썬 re 문법(512자까지)",
            required=True,
            max_length=MAX_REGEX_PATTERN_CHARS,
        ),
        ToolParam(
            "flags",
            "string",
            "플래그",
            "i(대소문자 무시)·m(여러 줄)·s(.이 줄바꿈 포함)",
            default="",
        ),
        ToolParam(
            "group",
            "string",
            "그룹",
            "꺼낼 그룹 번호나 이름. 비우거나 0이면 맞은 부분 전체",
            default="",
            max_length=64,
        ),
    )
    outputs = (
        OutputSpec("text", "string"),
        OutputSpec("matches", "array[string]"),
        OutputSpec("count", "number"),
        OutputSpec("found", "boolean"),
    )

    def check(self, name: str, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            if name == "pattern":
                if len(value) > MAX_REGEX_PATTERN_CHARS:
                    return f"정규식은 {MAX_REGEX_PATTERN_CHARS}자까지다"
                re.compile(value)
            elif name == "flags":
                _flags(value)
        except (re.error, ValueError) as exc:
            return f"정규식 오류: {exc}" if name == "pattern" else str(exc)
        return None

    async def run(self, ctx: NodeContext, args: Mapping[str, Any]) -> ToolResult:
        text = str(args.get("text") or "")
        pattern = str(args.get("pattern") or "")
        if len(pattern) > MAX_REGEX_PATTERN_CHARS:
            raise NodeFailure("invalid_pattern", f"정규식은 {MAX_REGEX_PATTERN_CHARS}자까지다")
        if len(text.encode("utf-8")) > MAX_REGEX_INPUT_BYTES:
            raise NodeFailure("variable_limit", "정규식으로 찾을 글이 1MB를 넘는다")
        try:
            compiled = re.compile(pattern, _flags(str(args.get("flags") or "")))
        except ValueError as exc:
            raise NodeFailure("invalid_flags", str(exc)) from None
        except re.error as exc:
            raise NodeFailure("invalid_pattern", f"정규식 오류: {exc}") from None
        group = _group(str(args.get("group") or ""))
        if isinstance(group, int) and group > compiled.groups:
            raise NodeFailure("invalid_group", f"정규식에 {group}번 그룹이 없다")
        if isinstance(group, str) and group not in compiled.groupindex:
            raise NodeFailure("invalid_group", f"정규식에 {group} 그룹이 없다")
        matches: list[str] = []
        capped = False
        for match in compiled.finditer(text):
            if len(matches) >= MAX_MATCHES:
                capped = True
                break
            matches.append(match.group(group) or "")
        if capped:
            ctx.warn(f"matches_capped: 맞은 부분이 {MAX_MATCHES}개를 넘어 앞의 것만 담았다")
        return ToolResult(
            outputs={
                "text": matches[0] if matches else "",
                "matches": matches,
                "count": len(matches),
                "found": bool(matches),
            },
            process={"count": len(matches), "capped": capped},
        )
