import json
from pathlib import Path

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
    Usage,
)

SHELL = ToolSpec(name="shell", description="run", input_schema={"type": "object"})


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
