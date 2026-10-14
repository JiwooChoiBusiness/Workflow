import asyncio
import json
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.llm import make_adapter
from app.llm.base import ModelAdapter
from app.llm.fake_adapter import FakeAdapter, fake_end, fake_text, fake_tool_call
from app.llm.gemini_adapter import GeminiAdapter
from app.llm.types import (
    CanonicalMessage,
    ChatRequest,
    ErrorEvent,
    MessageEnd,
    ModelEvent,
    TextBlock,
    TextDelta,
    ToolCallEnd,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

SHELL = ToolSpec(name="shell", description="run", input_schema={"type": "object"})
ASK_USER = ToolSpec(name="ask_user", description="ask", input_schema={"type": "object"})
WRITE_FILE = ToolSpec(name="write_file", description="write", input_schema={"type": "object"})
ALL_TOOLS = [SHELL, ASK_USER, WRITE_FILE]


def request(text: str = "안녕", **kwargs: object) -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "system": "s",
            "messages": [CanonicalMessage(role="user", blocks=[TextBlock(text=text)])],
            **kwargs,
        }
    )


async def collect(adapter: FakeAdapter, req: ChatRequest) -> list[ModelEvent]:
    return [event async for event in adapter.stream(req)]


def chat_request(text: str, tools: list[ToolSpec]) -> ChatRequest:
    # 루프가 만드는 user 행처럼 사용자 글 뒤에 <context> 블록을 붙인다.
    context = TextBlock(text="<context>\n세션 소스: preview\n</context>")
    message = CanonicalMessage(role="user", blocks=[TextBlock(text=text), context])
    return ChatRequest(system="s", tools=tools, messages=[message])


def answered(req: ChatRequest, call: ToolCallEnd, content: str) -> ChatRequest:
    tool_use = ToolUseBlock(id=call.id, name=call.name, input=json.loads(call.arguments))
    result = ToolResultBlock(tool_use_id=call.id, content=content)
    return req.model_copy(
        update={
            "messages": [
                *req.messages,
                CanonicalMessage(role="assistant", blocks=[tool_use]),
                CanonicalMessage(role="user", blocks=[result]),
            ]
        }
    )


def tool_call(events: list[ModelEvent]) -> ToolCallEnd:
    return next(event for event in events if isinstance(event, ToolCallEnd))


def text_of(events: list[ModelEvent]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDelta))


async def test_scripted_turns_play_in_order_and_requests_are_recorded() -> None:
    adapter = FakeAdapter(
        [
            [*fake_tool_call("shell", {"command": "dir"}, call_id="call_1"), fake_end("tool_use")],
            [*fake_text("끝났습니다."), fake_end()],
        ]
    )
    first_req = request(tools=[SHELL])

    first = await collect(adapter, first_req)
    second = await collect(adapter, request("다음"))

    assert [event.type for event in first] == [
        "tool_call_start",
        "tool_call_delta",
        "tool_call_end",
        "message_end",
    ]
    call = first[2]
    assert isinstance(call, ToolCallEnd)
    assert (call.id, json.loads(call.arguments)) == ("call_1", {"command": "dir"})
    assert "".join(e.text for e in second if isinstance(e, TextDelta)) == "끝났습니다."
    assert adapter.requests == [first_req, request("다음")]
    assert adapter.requests[0] is not first_req
    assert adapter.remaining_turns == 0


async def test_message_end_is_added_when_a_turn_omits_it() -> None:
    adapter = FakeAdapter([fake_tool_call("shell", {"command": "dir"}), fake_text("답")])

    tool_turn = await collect(adapter, request())
    text_turn = await collect(adapter, request())

    tool_end, text_end = tool_turn[-1], text_turn[-1]
    assert isinstance(tool_end, MessageEnd)
    assert isinstance(text_end, MessageEnd)
    assert (tool_end.stop_reason, text_end.stop_reason) == ("tool_use", "end_turn")
    assert tool_end.usage.estimated
    assert tool_end.usage.input_tokens > 0


async def test_explicit_usage_is_kept_for_compaction_scenarios() -> None:
    big = Usage(input_tokens=250_000, output_tokens=10)
    adapter = FakeAdapter([[*fake_text("요약 전"), fake_end(usage=big)]])

    end = (await collect(adapter, request()))[-1]

    assert isinstance(end, MessageEnd)
    assert end.usage == big


async def test_exhausted_script_yields_error_event() -> None:
    adapter = FakeAdapter([fake_text("한 번")])
    await collect(adapter, request())

    events = await collect(adapter, request())

    [error] = events
    assert isinstance(error, ErrorEvent)
    assert error.code == "fake_script_exhausted"


async def test_turn_can_be_a_function_of_the_request() -> None:
    def echo(req: ChatRequest) -> list[ModelEvent]:
        block = req.messages[-1].blocks[0]
        assert isinstance(block, TextBlock)
        return fake_text(f"echo:{block.text}")

    adapter = FakeAdapter([echo])

    events = await collect(adapter, request("핑"))

    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "echo:핑"


async def test_auto_mode_calls_shell_then_answers_after_result() -> None:
    adapter = FakeAdapter()

    first = await collect(adapter, request(tools=[SHELL]))
    call = next(event for event in first if isinstance(event, ToolCallEnd))
    follow_up = ChatRequest(
        system="s",
        tools=[SHELL],
        messages=[
            CanonicalMessage(role="user", blocks=[TextBlock(text="안녕")]),
            CanonicalMessage(
                role="user", blocks=[ToolResultBlock(tool_use_id=call.id, content="ok")]
            ),
        ],
    )
    second = await collect(adapter, follow_up)
    plain = await collect(adapter, request("도구 없이"))

    assert json.loads(call.arguments) == {"command": "echo fake-llm"}
    assert isinstance(first[-1], MessageEnd) and first[-1].stop_reason == "tool_use"
    assert isinstance(second[-1], MessageEnd) and second[-1].stop_reason == "end_turn"
    assert "도구 없이" in "".join(e.text for e in plain if isinstance(e, TextDelta))
    assert adapter.remaining_turns is None


async def test_question_prefix_asks_the_user_and_repeats_the_answer() -> None:
    adapter = FakeAdapter()
    first_req = chat_request("질문: 계속 진행할까요?", ALL_TOOLS)

    first = await collect(adapter, first_req)
    call = tool_call(first)
    second = await collect(adapter, answered(first_req, call, "예"))

    assert call.name == "ask_user"
    assert json.loads(call.arguments) == {
        "question": "계속 진행할까요?",
        "options": ["예", "아니오"],
    }
    assert isinstance(first[-1], MessageEnd) and first[-1].stop_reason == "tool_use"
    assert text_of(second) == "답을 확인했습니다: 예"
    assert isinstance(second[-1], MessageEnd) and second[-1].stop_reason == "end_turn"


async def test_file_prefix_writes_the_rest_to_outputs() -> None:
    adapter = FakeAdapter()
    first_req = chat_request("파일: 보고서 첫 줄", ALL_TOOLS)

    call = tool_call(await collect(adapter, first_req))
    second = await collect(adapter, answered(first_req, call, "outputs/결과.txt에 썼다."))

    assert call.name == "write_file"
    assert json.loads(call.arguments) == {"path": "outputs/결과.txt", "content": "보고서 첫 줄"}
    assert text_of(second) == "파일을 썼습니다."


async def test_prefix_is_read_from_the_latest_user_text() -> None:
    # 앞 런이 답 없이 끝나면 히스토리에서 두 user 행이 한 메시지로 합쳐진다.
    context = TextBlock(text="<context>\n세션 소스: preview\n</context>")
    message = CanonicalMessage(
        role="user",
        blocks=[TextBlock(text="앞 메시지"), context, TextBlock(text="파일: 새 내용"), context],
    )
    req = ChatRequest(system="s", tools=ALL_TOOLS, messages=[message])

    call = tool_call(await collect(FakeAdapter(), req))

    assert call.name == "write_file"
    assert json.loads(call.arguments)["content"] == "새 내용"


@pytest.mark.parametrize(
    ("text", "tools"),
    [
        ("작업공간 파일 목록을 보여줘", ALL_TOOLS),
        ("질문:", ALL_TOOLS),
        ("질문: ask_user가 없다", [SHELL]),
        ("파일: write_file이 없다", [SHELL]),
        ("이 메시지의 질문: 접두는 앞에 있지 않다", ALL_TOOLS),
    ],
)
async def test_other_messages_keep_the_shell_flow(text: str, tools: list[ToolSpec]) -> None:
    adapter = FakeAdapter()
    first_req = chat_request(text, tools)

    call = tool_call(await collect(adapter, first_req))
    second = await collect(adapter, answered(first_req, call, "fake-llm"))

    assert (call.name, json.loads(call.arguments)) == ("shell", {"command": "echo fake-llm"})
    assert text_of(second) == "도구 실행 결과를 확인했습니다."


async def test_script_file_loads_with_or_without_turns_key(tmp_path: Path) -> None:
    turns = [
        [{"type": "text_delta", "text": "파일 각본"}],
        [{"type": "error", "code": "model_unavailable", "message": "x", "retryable": True}],
    ]
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"turns": turns}, ensure_ascii=False), encoding="utf-8")
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(turns, ensure_ascii=False), encoding="utf-8")

    for path in (wrapped, bare):
        adapter = FakeAdapter.from_file(path)
        text_turn = await collect(adapter, request())
        error_turn = await collect(adapter, request())

        assert isinstance(text_turn[0], TextDelta)
        assert isinstance(text_turn[-1], MessageEnd)
        assert len(error_turn) == 1
        assert isinstance(error_turn[0], ErrorEvent)


async def test_fake_adapter_satisfies_protocol() -> None:
    adapter = FakeAdapter(model="fake-x")

    assert isinstance(adapter, ModelAdapter)
    assert adapter.name == "fake"
    assert await adapter.list_models() == ["fake-x"]
    assert adapter.capabilities("gemini-3.8-flash").known
    assert adapter.estimate_tokens(request()) > 0


def test_make_adapter_follows_llm_fake_setting() -> None:
    fake = make_adapter(Settings(llm_fake=True))
    gemini = make_adapter(Settings.model_validate({"gemini_api_keys": "key-one,key-two"}))

    assert isinstance(fake, FakeAdapter)
    assert isinstance(gemini, GeminiAdapter)
    assert len(gemini.key_pool) == 2
    assert gemini.model_fallback == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]


async def test_make_adapter_loads_llm_fake_script(tmp_path: Path) -> None:
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps({"turns": [[{"type": "text_delta", "text": "각본 응답"}]]}, ensure_ascii=False),
        encoding="utf-8",
    )

    scripted = make_adapter(Settings(llm_fake=True, llm_fake_script=script))
    default = make_adapter(Settings(llm_fake=True))

    assert isinstance(scripted, FakeAdapter) and isinstance(default, FakeAdapter)
    assert scripted.remaining_turns == 1
    assert default.remaining_turns is None
    events = await collect(scripted, request())
    assert [event.text for event in events if isinstance(event, TextDelta)] == ["각본 응답"]


async def test_llm_fake_delay_waits_once_per_step(tmp_path: Path) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"turns": [[{"type": "text_delta", "text": "x"}]]}), "utf-8")
    delayed = make_adapter(Settings(llm_fake=True, llm_fake_delay_sec=0.2))
    scripted = make_adapter(Settings(llm_fake=True, llm_fake_script=script, llm_fake_delay_sec=0.1))
    plain = make_adapter(Settings(llm_fake=True))
    assert isinstance(delayed, FakeAdapter) and isinstance(scripted, FakeAdapter)
    assert isinstance(plain, FakeAdapter)
    assert (delayed.step_delay_sec, scripted.step_delay_sec, plain.step_delay_sec) == (0.2, 0.1, 0)

    started = time.monotonic()
    events = await collect(delayed, request("이벤트가 여러 개 나오도록 긴 질문을 보낸다"))
    elapsed = time.monotonic() - started

    # 이벤트 수와 관계없이 스텝마다 한 번 쉰다.
    assert len(events) > 2
    assert 0.18 <= elapsed < 1.0


async def test_step_delay_is_cancellable() -> None:
    adapter = FakeAdapter([fake_text("늦은 답")], step_delay_sec=30)
    task = asyncio.create_task(collect(adapter, request()))
    await asyncio.sleep(0.05)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
