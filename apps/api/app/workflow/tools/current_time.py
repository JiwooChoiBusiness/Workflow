# pyright: strict
"""current_time 도구. 지금 시각을 정한 시간대와 형식으로 낸다.

시간대는 local(이 PC), UTC, +09:00 같은 오프셋, Asia/Seoul 같은 IANA 이름을 받는다. Windows
파이썬에는 IANA 시간대 자료(tzdata)가 없고 서버 의존성을 늘리지 않기로 했으므로, zoneinfo가 못
찾으면 서머타임이 없는 몇몇 지역만 고정 오프셋으로 푼다. 그 밖의 이름은 오프셋으로 쓰라고 알린다.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.workflow.nodes.base import NodeContext, NodeFailure, OutputSpec
from app.workflow.tools.base import Tool, ToolParam, ToolResult

LOCAL = "local"
_OFFSET = re.compile(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", re.IGNORECASE)
# 서머타임이 없어 오프셋이 늘 같은 지역(분).
_FIXED_ZONES = {
    "asia/seoul": 540,
    "asia/tokyo": 540,
    "asia/pyongyang": 540,
    "asia/shanghai": 480,
    "asia/hong_kong": 480,
    "asia/taipei": 480,
    "asia/singapore": 480,
    "asia/manila": 480,
    "asia/kuala_lumpur": 480,
    "asia/bangkok": 420,
    "asia/ho_chi_minh": 420,
    "asia/jakarta": 420,
    "asia/kolkata": 330,
    "asia/dubai": 240,
}
_UTC_NAMES = frozenset({"utc", "z", "gmt", "etc/utc", "etc/gmt"})


class TimezoneError(ValueError):
    pass


def _offset_label(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def resolve_timezone(name: str) -> tuple[tzinfo, str]:
    """(시간대, 출력에 쓸 이름). 모르는 이름이면 TimezoneError."""
    text = name.strip()
    if not text or text.lower() == LOCAL:
        local = datetime.now().astimezone().tzinfo or UTC
        offset = datetime.now(local).utcoffset() or timedelta(0)
        return local, _offset_label(offset)
    if text.lower() in _UTC_NAMES:
        return UTC, "UTC"
    match = _OFFSET.fullmatch(text)
    if match is not None:
        hours, minutes = int(match.group(2)), int(match.group(3) or 0)
        if hours > 14 or minutes > 59:
            raise TimezoneError(f"오프셋 {text}은(는) 쓸 수 없다")
        offset = timedelta(hours=hours, minutes=minutes)
        offset = -offset if match.group(1) == "-" else offset
        return timezone(offset), _offset_label(offset)
    try:
        return ZoneInfo(text), text
    except (ZoneInfoNotFoundError, ValueError):
        pass
    fixed = _FIXED_ZONES.get(text.lower())
    if fixed is not None:
        return timezone(timedelta(minutes=fixed), text), text
    raise TimezoneError(f"시간대 {text}을(를) 찾지 못했다. local, UTC, +09:00 같은 오프셋으로 쓴다")


def _check_format(value: str) -> str | None:
    try:
        datetime(2000, 1, 2, 3, 4, 5, tzinfo=UTC).strftime(value)
    except ValueError as exc:
        return f"시각 형식이 올바르지 않다: {exc}"
    return None


class CurrentTimeTool(Tool):
    name = "current_time"
    label = "현재 시각"
    description = "지금 날짜와 시각을 정한 시간대와 형식으로 낸다"
    params = (
        ToolParam(
            "timezone",
            "string",
            "시간대",
            "local(이 PC의 시간대), UTC, +09:00 같은 오프셋, Asia/Seoul 같은 이름",
            default=LOCAL,
            max_length=64,
        ),
        ToolParam(
            "format",
            "string",
            "형식",
            "strftime 형식(예: %Y-%m-%d %H:%M). 비우면 ISO 8601",
            default="",
            max_length=100,
        ),
    )
    outputs = (
        OutputSpec("text", "string"),
        OutputSpec("iso", "string"),
        OutputSpec("date", "string"),
        OutputSpec("timestamp", "number"),
        OutputSpec("timezone", "string"),
    )

    def check(self, name: str, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        if name == "timezone":
            try:
                resolve_timezone(value)
            except TimezoneError as exc:
                return str(exc)
        elif name == "format" and value:
            return _check_format(value)
        return None

    async def run(self, ctx: NodeContext, args: Mapping[str, Any]) -> ToolResult:
        try:
            zone, label = resolve_timezone(str(args.get("timezone") or LOCAL))
        except TimezoneError as exc:
            raise NodeFailure("invalid_timezone", str(exc)) from None
        now = ctx.ports.now().astimezone(zone)
        iso = now.isoformat(timespec="seconds")
        pattern = str(args.get("format") or "")
        if pattern:
            problem = _check_format(pattern)
            if problem is not None:
                raise NodeFailure("invalid_format", problem)
        text = now.strftime(pattern) if pattern else iso
        return ToolResult(
            outputs={
                "text": text,
                "iso": iso,
                "date": now.strftime("%Y-%m-%d"),
                "timestamp": int(now.timestamp()),
                "timezone": label,
            },
            process={"timezone": label},
        )
