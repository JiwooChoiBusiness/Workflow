# pyright: strict
"""answer 미리 흘리기(2단계 설계 3.8).

answer 템플릿이 글자와 {{ a.b }}만으로 되어 있으면 조각으로 나눠 앞에서부터 "첫 미해결 변수"까지
흘린다. 변수 조각이 상류 llm의 text이고 그 llm이 돌고 있으면 그 델타를 answer.delta로 바로
흘린다. answer 노드가 ready가 되기 전의 일이라 노드가 아니라 엔진이 이 모듈로 하고, 노드는 엔진이
흘리고 남은 뒷부분만 낸다.

선택되지 않을 가지로 델타가 나가면 안 되므로 미리 흘리는 llm은 두 조건을 다 채워야 한다.
(1) 그 llm에서 answer까지의 모든 경로에 분기 노드(if_else, question_classifier,
error.strategy=branch)가 없다. 그래프만 보고 정하며(prestream_sources) 저장할 때 answer 노드의
ui.prestream_sources로 둔다.
(2) answer의 다른 들어오는 엣지가 이미 해결됐다. 실행 중에 본다.

여러 answer는 완료 순서로 이어 붙는다. 미리 흘리는 동안 다른 answer가 끝나 글이 섞이지 않게, 다른
answer(answer를 품은 iteration 포함)가 모두 끝났거나 이 answer 뒤에 올 때만 연다. 대화 변수 조각도
같은 이유로 대화 변수를 바꾸는 노드가 다 끝났거나 이 answer 뒤에 올 때만 미리 렌더한다. 이미 흘린
델타는 되돌릴 수 없으므로 재시도나 오류 정책(default·branch)이 걸린 llm은 미리 흘리지 않는다.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.workflow.frames import SETTLED, FrameGraph, FrameState
from app.workflow.graph import Analysis
from app.workflow.render import Segment, TemplateError, render, stream_segments
from app.workflow.variables import CONV

log = structlog.get_logger("app.workflow")

Emit = Callable[[str, str], Awaitable[None]]
Context = Callable[[], Mapping[str, Any]]


def _descendants(frame: FrameGraph) -> dict[str, frozenset[str]]:
    """노드 → 그 노드에서 엣지를 따라 닿는 노드(자기 자신 빼고)."""
    found: dict[str, frozenset[str]] = {}
    for node_id in frame.nodes:
        seen: set[str] = set()
        stack = [edge.target for edge in frame.outgoing[node_id]]
        while stack:
            target = stack.pop()
            if target in seen:
                continue
            seen.add(target)
            stack.extend(edge.target for edge in frame.outgoing[target])
        found[node_id] = frozenset(seen)
    return found


def _template(analysis: Analysis, node_id: str) -> str | None:
    runner, config = analysis.runners.get(node_id), analysis.configs.get(node_id)
    if runner is None or config is None:
        return None
    return runner.answer_template(config)


def _stream_output(analysis: Analysis, node_id: str) -> str | None:
    runner, config = analysis.runners.get(node_id), analysis.configs.get(node_id)
    node = analysis.nodes.get(node_id)
    if runner is None or config is None or node is None:
        return None
    if node.retry.max_attempts > 0 or node.error.strategy != "fail":
        return None
    return runner.stream_output(config)


def _branches(analysis: Analysis, node_id: str) -> bool:
    runner = analysis.runners.get(node_id)
    return analysis.nodes[node_id].error.strategy == "branch" or runner is None or runner.branching


def _writes(analysis: Analysis, node_id: str) -> bool:
    runner = analysis.runners.get(node_id)
    return runner is not None and runner.writes_conversation


def _members(analysis: Analysis, frame: FrameGraph, test: Callable[[str], bool]) -> frozenset[str]:
    """루트 노드 중 test에 맞는 것과 그런 노드를 하위 그래프에 품은 iteration."""
    found: set[str] = set()
    for node_id in frame.nodes:
        inner = analysis.frames.get(node_id)
        if test(node_id) or (inner is not None and any(test(item) for item in inner.nodes)):
            found.add(node_id)
    return frozenset(found)


def _live(segment: Segment, sources: Mapping[str, str]) -> bool:
    """델타를 그대로 흘릴 수 있는 조각({{ llm_1.text }} 꼴)인지."""
    scope = segment.scope
    return scope is not None and not segment.deep and sources.get(scope) == segment.attr


def prestream_sources(analysis: Analysis) -> dict[str, list[str]]:
    """루트 그래프의 answer 노드 id → 델타를 미리 흘릴 수 있는 상류 노드 id(템플릿 순서).

    조건 (1)만 본다. 템플릿이 단순하지 않거나 그런 노드가 없으면 빈 목록이다.
    """
    frame = analysis.frames.get(None)
    if analysis.kind != "chatflow" or frame is None:
        return {}
    below = _descendants(frame)
    found: dict[str, list[str]] = {}
    for answer_id in frame.nodes:
        template = _template(analysis, answer_id)
        if template is None:
            continue
        sources: list[str] = []
        for segment in stream_segments(template) or ():
            source = segment.scope
            if (
                source is None
                or source in sources
                or source not in below
                or answer_id not in below[source]
                or segment.deep
                or _stream_output(analysis, source) != segment.attr
            ):
                continue
            between = [item for item in below[source] if answer_id in below[item]]
            if any(_branches(analysis, item) for item in between):
                continue
            sources.append(source)
        found[answer_id] = sources
    return found


@dataclass
class _Stream:
    node_id: str
    segments: tuple[Segment, ...]
    # 미리 흘릴 수 있는 상류 노드 → 델타가 이루는 출력 이름.
    sources: Mapping[str, str]
    cursor: int = 0
    parts: list[str] = field(default_factory=list[str])
    opened: bool = False
    # answer 노드가 시작해 더는 엔진이 흘리지 않는다.
    closed: bool = False
    # 렌더가 실패했거나 흘린 델타가 출력과 어긋나 멈췄다. 나머지는 노드가 정한다.
    broken: bool = False
    # 지금 델타를 그대로 흘리는 상류 노드와 그 노드 델타 중 흘린 개수.
    live: str | None = None
    forwarded: int = 0
    # 노드 시작·완료가 여러 태스크에서 동시에 알려 와도 조각을 한 줄로 흘린다.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def text(self) -> str:
        return "".join(self.parts)


class AnswerStreams:
    """한 런의 answer 미리 흘리기. 엔진이 루트 프레임 노드의 시작·델타·완료를 알려 준다."""

    def __init__(
        self, analysis: Analysis, root: FrameState, *, context: Context, emit: Emit
    ) -> None:
        self._root = root
        self._context = context
        self._emit = emit
        frame = root.graph
        self._below = _descendants(frame)
        self._incoming = frame.incoming
        self._streams: dict[str, _Stream] = {}
        for answer_id, found in prestream_sources(analysis).items():
            if not found:
                continue
            outputs = {source: _stream_output(analysis, source) or "" for source in found}
            segments = stream_segments(_template(analysis, answer_id) or "") or ()
            self._streams[answer_id] = _Stream(answer_id, segments, outputs)
        self._watched = frozenset(
            source for stream in self._streams.values() for source in stream.sources
        )
        self._buffers: dict[str, list[str]] = {}
        self._emitters = _members(
            analysis, frame, lambda node_id: _template(analysis, node_id) is not None
        )
        self._writers = _members(analysis, frame, lambda node_id: _writes(analysis, node_id))

    @property
    def active(self) -> bool:
        return bool(self._streams)

    async def started(self, node_id: str) -> None:
        if node_id in self._watched:
            self._buffers.setdefault(node_id, [])
        await self._advance()

    async def delta(self, node_id: str, text: str) -> None:
        buffer = self._buffers.get(node_id)
        if buffer is None or not text:
            return
        buffer.append(text)
        for stream in self._streams.values():
            if stream.live == node_id and not stream.closed and not stream.broken:
                stream.forwarded = len(buffer)
                await self._send(stream, text)

    async def settled(self) -> None:
        await self._advance()

    def take(self, node_id: str) -> str:
        """answer 노드가 시작할 때 부른다. 더 흘리지 않게 닫고 지금까지 흘린 글을 준다."""
        stream = self._streams.get(node_id)
        if stream is None:
            return ""
        stream.closed = True
        return stream.text

    def pending(self) -> str:
        """열었지만 노드가 아직 돌지 않은 answer가 흘린 글. 런이 멈추거나 실패하면 답 글이 된다."""
        return "".join(
            stream.text for stream in self._streams.values() if stream.opened and not stream.closed
        )

    def cursors(self) -> dict[str, int]:
        """체크포인트에 남길 열린 answer의 조각 위치."""
        return {
            node_id: stream.cursor
            for node_id, stream in self._streams.items()
            if stream.opened and not stream.closed
        }

    def restore(self, cursors: Mapping[str, int]) -> None:
        """재개할 때 멈춘 런이 흘린 앞부분을 다시 만든다. 그 사이 값은 체크포인트의 풀에 있다.

        멈추기 전에 끝난(건너뛴) answer는 노드가 이미 글을 냈으므로 닫는다. 열어 두면 상류 llm이
        끝나 있어 처음부터 다시 흘리고 그 글이 답 끝에 한 번 더 붙는다.
        """
        for node_id, stream in self._streams.items():
            if self._root.nodes[node_id] in SETTLED:
                stream.closed = True
        for node_id, cursor in cursors.items():
            stream = self._streams.get(node_id)
            if stream is None:
                continue
            stream.opened = True
            stream.cursor = max(0, min(cursor, len(stream.segments)))
            stream.parts = [
                part
                for segment in stream.segments[: stream.cursor]
                if (part := self._value(stream, segment))
            ]

    async def _advance(self) -> None:
        for stream in self._streams.values():
            if stream.closed or stream.broken:
                continue
            async with stream.lock:
                if stream.closed or stream.broken:
                    continue
                if not stream.opened:
                    if not self._can_open(stream):
                        continue
                    stream.opened = True
                await self._pump(stream)

    def _can_open(self, stream: _Stream) -> bool:
        nodes, edges = self._root.nodes, self._root.edges
        later = self._below[stream.node_id]
        for other in self._emitters:
            if other != stream.node_id and nodes[other] not in SETTLED and other not in later:
                return False
        for source in stream.sources:
            if nodes[source] not in ("running", "succeeded"):
                continue
            around = self._below[source]
            if all(
                edges[edge.id] != "pending"
                for edge in self._incoming[stream.node_id]
                if edge.source != source and edge.source not in around
            ):
                return True
        return False

    async def _pump(self, stream: _Stream) -> None:
        nodes = self._root.nodes
        while not stream.closed and not stream.broken and stream.cursor < len(stream.segments):
            segment = stream.segments[stream.cursor]
            scope = segment.scope
            if scope is None:
                stream.cursor += 1
                await self._send(stream, segment.text)
                continue
            if scope in nodes:
                state = nodes[scope]
                if state == "running" and _live(segment, stream.sources):
                    # 밀린 델타를 다 흘린 뒤에야 live로 둔다. 그 사이 온 델타는 버퍼에 쌓여 이
                    # 반복이 이어 흘리고, delta()가 순서를 앞질러 보내지 않는다.
                    buffer = self._buffers.setdefault(scope, [])
                    while stream.forwarded < len(buffer) and not stream.closed:
                        piece = buffer[stream.forwarded]
                        stream.forwarded += 1
                        await self._send(stream, piece)
                    stream.live = scope
                    return
                if state not in SETTLED:
                    return
                text = self._value(stream, segment)
                if stream.live == scope:
                    sent = "".join(self._buffers.get(scope, [])[: stream.forwarded])
                    stream.live, stream.forwarded = None, 0
                    if not text.startswith(sent):
                        stream.broken = True
                        log.warning(
                            "workflow.answer_stream_diverged", node_id=stream.node_id, source=scope
                        )
                        return
                    text = text[len(sent) :]
                stream.cursor += 1
                await self._send(stream, text)
                continue
            if scope == CONV and not self._conversation_settled(stream):
                return
            # sys·env 값은 런 동안 바뀌지 않는다. 없는 이름은 빈칸이다.
            stream.cursor += 1
            await self._send(stream, self._value(stream, segment))

    def _conversation_settled(self, stream: _Stream) -> bool:
        nodes = self._root.nodes
        later = self._below[stream.node_id]
        return all(nodes[writer] in SETTLED or writer in later for writer in self._writers)

    def _value(self, stream: _Stream, segment: Segment) -> str:
        if segment.expr is None:
            return segment.text
        try:
            return render(segment.expr, self._context()).text
        except TemplateError as exc:
            stream.broken = True
            log.warning(
                "workflow.answer_stream_render_failed", node_id=stream.node_id, code=exc.code
            )
            return ""

    async def _send(self, stream: _Stream, text: str) -> None:
        if not text:
            return
        # 기다리는 동안 노드가 시작해 take()로 닫아도 이 조각까지는 글에 들어가야 한다.
        stream.parts.append(text)
        await self._emit(stream.node_id, text)
