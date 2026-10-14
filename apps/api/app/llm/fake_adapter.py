import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import TypeAdapter

from app.llm.catalog import ModelCapabilities, get_capabilities
from app.llm.token_estimate import estimate_request, estimate_text
from app.llm.types import (
    CanonicalMessage,
    ChatRequest,
    ErrorEvent,
    MessageEnd,
    ModelEvent,
    StopReason,
    TextBlock,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

FAKE_MODEL = "fake-model"
ERR_FAKE_SCRIPT_EXHAUSTED = "fake_script_exhausted"

# 기본 각본의 접두 표시. e2e가 질문·파일 흐름을 메시지 글만으로 고른다.
QUESTION_PREFIX = "질문:"
FILE_PREFIX = "파일:"
FAKE_OPTIONS = ("예", "아니오")
FAKE_OUTPUT_PATH = "outputs/결과.txt"

# 한 번의 stream() 호출이 낼 이벤트. 요청을 보고 정하는 함수로 줄 수도 있다.
FakeTurn = Sequence[ModelEvent] | Callable[[ChatRequest], Sequence[ModelEvent]]

_SCRIPT = TypeAdapter(list[list[ModelEvent]])


def fake_text(text: str, *, chunk_size: int = 24) -> list[ModelEvent]:
    return [
        TextDelta(text=text[start : start + chunk_size])
        for start in range(0, len(text), chunk_size)
    ]


def fake_tool_call(
    name: str,
    tool_input: Mapping[str, Any],
    *,
    index: int = 0,
    call_id: str | None = None,
    provider_extra: dict[str, Any] | None = None,
) -> list[ModelEvent]:
    call_id = call_id or f"call_fake_{uuid.uuid4().hex[:8]}"
    arguments = json.dumps(tool_input, ensure_ascii=False)
    return [
        ToolCallStart(index=index, id=call_id, name=name),
        ToolCallDelta(index=index, json_fragment=arguments),
        ToolCallEnd(
            index=index,
            id=call_id,
            name=name,
            arguments=arguments,
            provider_extra=provider_extra,
        ),
    ]


def fake_end(
    stop_reason: StopReason = "end_turn",
    *,
    usage: Usage | None = None,
    model: str = FAKE_MODEL,
) -> MessageEnd:
    raw_stop = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}
    return MessageEnd(
        stop_reason=stop_reason,
        usage=usage or Usage(estimated=True),
        raw_stop=raw_stop.get(stop_reason, "content_filter"),
        model=model,
    )


def auto_turn(req: ChatRequest) -> list[ModelEvent]:
    # 각본 없이 띄울 때(LLM_FAKE=true)의 기본 응답. memcheck·스모크가 쓰는 "셸 도구 1회 + 텍스트"
    # 흐름이고, 메시지가 "질문:"이나 "파일:"로 시작할 때만 ask_user나 write_file을 대신 부른다.
    last = req.messages[-1] if req.messages else None
    if last is not None:
        results = [block for block in last.blocks if isinstance(block, ToolResultBlock)]
        if results:
            return fake_text(_after_tool(req.messages, results[-1]))
    tools = {tool.name for tool in req.tools}
    texts = [block.text for block in last.blocks if isinstance(block, TextBlock)] if last else []
    # user 행은 사용자가 쓴 글 뒤에 <context> 블록이 붙는다. 앞 런이 답 없이 끝났으면(shutdown 등)
    # 두 user 행이 한 메시지로 합쳐지므로 맨 뒤에 쓴 글을 본다.
    written = [text for text in texts if not text.startswith("<context>")]
    user_text = written[-1].strip() if written else ""
    question = user_text.removeprefix(QUESTION_PREFIX).strip()
    if user_text.startswith(QUESTION_PREFIX) and question and "ask_user" in tools:
        return fake_tool_call("ask_user", {"question": question, "options": list(FAKE_OPTIONS)})
    if user_text.startswith(FILE_PREFIX) and "write_file" in tools:
        content = user_text.removeprefix(FILE_PREFIX).strip()
        return fake_tool_call("write_file", {"path": FAKE_OUTPUT_PATH, "content": content})
    if "shell" in tools:
        # PowerShell과 bash(CI) 양쪽에서 도는 명령.
        return fake_tool_call("shell", {"command": "echo fake-llm"})
    return fake_text(f"가짜 응답: {' '.join(texts)[:200]}")


def _after_tool(messages: Sequence[CanonicalMessage], result: ToolResultBlock) -> str:
    name = next(
        (
            block.name
            for message in reversed(messages)
            for block in message.blocks
            if isinstance(block, ToolUseBlock) and block.id == result.tool_use_id
        ),
        None,
    )
    if name == "ask_user":
        # ask_user의 결과는 사용자의 답 그대로다(4.9).
        return f"답을 확인했습니다: {result.content}"
    if name == "write_file" and not result.is_error:
        return "파일을 썼습니다."
    return "도구 실행 결과를 확인했습니다."


class FakeAdapter:
    """각본대로 이벤트를 내는 테스트용 어댑터. stream() 한 번이 각본의 턴 하나를 쓴다."""

    name = "fake"

    def __init__(
        self,
        turns: Iterable[FakeTurn] | None = None,
        *,
        model: str = FAKE_MODEL,
        delay_sec: float = 0.0,
        step_delay_sec: float = 0.0,
    ) -> None:
        self._turns = list(turns) if turns is not None else None
        self._cursor = 0
        self.model = model
        # delay_sec는 이벤트마다, step_delay_sec(LLM_FAKE_DELAY_SEC)는 stream() 한 번마다 쉰다.
        self.delay_sec = delay_sec
        self.step_delay_sec = step_delay_sec
        self.requests: list[ChatRequest] = []

    @classmethod
    def from_file(
        cls, path: Path, *, model: str = FAKE_MODEL, step_delay_sec: float = 0.0
    ) -> "FakeAdapter":
        """{"turns": [[이벤트, ...], ...]} JSON. 턴 끝의 message_end는 생략할 수 있다."""
        data: object = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = cast(dict[str, Any], data).get("turns", [])
        return cls(_SCRIPT.validate_python(data), model=model, step_delay_sec=step_delay_sec)

    @property
    def remaining_turns(self) -> int | None:
        return None if self._turns is None else len(self._turns) - self._cursor

    async def stream(self, req: ChatRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(req.model_copy(deep=True))
        if self.step_delay_sec:
            # 모델이 답을 생각하는 시간처럼 첫 이벤트 전에 쉰다. 중지하면 여기서 바로 취소된다.
            await asyncio.sleep(self.step_delay_sec)
        for event in self._complete(req, self._next_events(req)):
            if self.delay_sec:
                await asyncio.sleep(self.delay_sec)
            yield event

    async def list_models(self) -> list[str]:
        return [self.model]

    def capabilities(self, model_id: str) -> ModelCapabilities:
        return get_capabilities(model_id)

    def estimate_tokens(self, req: ChatRequest) -> int:
        return estimate_request(req)

    async def aclose(self) -> None:
        return None

    def _next_events(self, req: ChatRequest) -> list[ModelEvent]:
        if self._turns is None:
            return auto_turn(req)
        if self._cursor >= len(self._turns):
            return [
                ErrorEvent(
                    code=ERR_FAKE_SCRIPT_EXHAUSTED,
                    message=f"가짜 어댑터 각본의 턴 {len(self._turns)}개를 모두 썼다",
                    retryable=False,
                )
            ]
        turn = self._turns[self._cursor]
        self._cursor += 1
        return list(turn(req) if callable(turn) else turn)

    def _complete(self, req: ChatRequest, events: list[ModelEvent]) -> list[ModelEvent]:
        if events and isinstance(events[-1], MessageEnd | ErrorEvent):
            return events
        calls = [event for event in events if isinstance(event, ToolCallEnd)]
        produced = "".join(event.text for event in events if isinstance(event, TextDelta))
        produced += "".join(call.arguments for call in calls)
        usage = Usage(
            input_tokens=estimate_request(req),
            output_tokens=estimate_text(produced),
            estimated=True,
        )
        return [
            *events,
            fake_end(
                "tool_use" if calls else "end_turn", usage=usage, model=req.model or self.model
            ),
        ]
