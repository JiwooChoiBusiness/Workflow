import json
from collections.abc import AsyncGenerator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest

from app.llm.base import ModelAdapter
from app.llm.catalog import get_capabilities
from app.llm.gemini_adapter import (
    SKIP_SIGNATURE,
    GeminiAdapter,
    OpenAICompatTransport,
    TransportError,
    build_request_body,
    thought_signature,
)
from app.llm.token_estimate import estimate_request
from app.llm.types import (
    CanonicalMessage,
    ChatRequest,
    ErrorEvent,
    ForcedTool,
    LLMError,
    MessageEnd,
    ModelEvent,
    ModelParams,
    TextBlock,
    TextDelta,
    ToolCallEnd,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)

FIXTURES = Path(__file__).parent / "fixtures"
KEYS = ["test-key-0000-aaaa", "test-key-1111-bbbb"]
MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
SHELL_SCHEMA = {
    "type": "object",
    "properties": {"command": {"type": "string"}},
    "required": ["command"],
}
# 픽스처의 expected_events를 만들 때 쓴 요청. no_usage의 추정 usage가 이 요청에서 나온다.
SNAPSHOT_REQUEST = ChatRequest(
    system="You are a concise assistant.",
    messages=[CanonicalMessage(role="user", blocks=[TextBlock(text="fixture replay")])],
)
SIGNATURE = {"extra_content": {"google": {"thought_signature": "c2lnbmF0dXJl"}}}
TEXT_SIGNATURE = {"extra_content": {"google": {"thought_signature": "dGV4dC1zaWc="}}}

Chunks = list[dict[str, Any]]
Step = Chunks | TransportError | tuple[Chunks, TransportError]


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def text_chunks(text: str = "ok", finish: str | None = "stop", prompt_tokens: int = 10) -> Chunks:
    last: dict[str, Any] = {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
    last["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": 2,
        "total_tokens": prompt_tokens + 2,
    }
    return [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}]}, last]


def http_error(status: int, message: str = "error") -> TransportError:
    return TransportError(message, status=status, kind="http")


def user_request(text: str = "hello", **kwargs: Any) -> ChatRequest:
    return ChatRequest(
        system="system prompt",
        messages=[CanonicalMessage(role="user", blocks=[TextBlock(text=text)])],
        **kwargs,
    )


class ScriptedTransport:
    def __init__(self, steps: Sequence[Step] = ()) -> None:
        self.steps = list(steps)
        self.calls: list[tuple[str, int, dict[str, Any]]] = []
        self.closed_streams = 0
        self.list_steps: list[list[str] | TransportError] = []
        self.list_calls: list[int] = []

    @property
    def models(self) -> list[str]:
        return [model for model, _, _ in self.calls]

    @property
    def key_indexes(self) -> list[int]:
        return [index for _, index, _ in self.calls]

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [body for _, _, body in self.calls]

    async def stream_chat(
        self, *, api_key: str, body: Mapping[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        self.calls.append((str(body["model"]), KEYS.index(api_key), dict(body)))
        step = self.steps.pop(0)
        try:
            if isinstance(step, TransportError):
                raise step
            chunks, error = step if isinstance(step, tuple) else (step, None)
            for chunk in chunks:
                yield chunk
            if error is not None:
                raise error
        finally:
            self.closed_streams += 1

    async def list_models(self, *, api_key: str) -> list[str]:
        self.list_calls.append(KEYS.index(api_key))
        step = self.list_steps.pop(0)
        if isinstance(step, TransportError):
            raise step
        return step

    async def aclose(self) -> None:
        return None


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_adapter(
    transport: ScriptedTransport,
    *,
    models: Sequence[str] = MODELS,
    sleep: FakeSleep | None = None,
) -> GeminiAdapter:
    return GeminiAdapter(
        api_keys=KEYS,
        model_fallback=models,
        transport=transport,
        sleep=sleep or FakeSleep(),
    )


async def collect(adapter: GeminiAdapter, req: ChatRequest) -> list[ModelEvent]:
    return [event async for event in adapter.stream(req)]


def last_end(events: list[ModelEvent]) -> MessageEnd:
    end = events[-1]
    assert isinstance(end, MessageEnd), end
    return end


def last_error(events: list[ModelEvent]) -> ErrorEvent:
    error = events[-1]
    assert isinstance(error, ErrorEvent), error
    return error


@pytest.mark.parametrize("name", sorted(path.stem for path in FIXTURES.glob("*.json")))
async def test_fixture_replay_matches_snapshot(name: str) -> None:
    fixture = load_fixture(name)
    # 빈 응답 픽스처는 재시도에서도 같은 응답을 받는다고 본다.
    transport = ScriptedTransport([fixture["chunks"], fixture["chunks"]])
    adapter = make_adapter(transport, models=[fixture["model"]])

    events = await collect(adapter, SNAPSHOT_REQUEST)

    assert [event.model_dump(mode="json") for event in events] == fixture["expected_events"]


async def test_recorded_tool_call_keeps_signature_at_fixed_path() -> None:
    fixture = load_fixture("tool_call_single")
    raw_call = fixture["chunks"][0]["choices"][0]["delta"]["tool_calls"][0]
    adapter = make_adapter(ScriptedTransport([fixture["chunks"]]), models=[fixture["model"]])

    events = await collect(adapter, SNAPSHOT_REQUEST)

    call = next(event for event in events if isinstance(event, ToolCallEnd))
    assert call.name == "shell"
    assert json.loads(call.arguments) == {"command": "Get-ChildItem"}
    assert call.provider_extra == {"extra_content": raw_call["extra_content"]}
    assert (
        thought_signature(call.provider_extra)
        == (raw_call["extra_content"]["google"]["thought_signature"])
    )
    end = last_end(events)
    # 호환 레이어는 도구 호출에도 finish_reason="stop"을 준다.
    assert (end.stop_reason, end.raw_stop) == ("tool_use", "stop")
    assert end.usage.input_tokens > 0
    assert not end.usage.estimated


async def test_recorded_parallel_calls_have_signature_only_on_first() -> None:
    fixture = load_fixture("tool_calls_parallel")
    adapter = make_adapter(ScriptedTransport([fixture["chunks"]]), models=[fixture["model"]])

    events = await collect(adapter, SNAPSHOT_REQUEST)

    calls = [event for event in events if isinstance(event, ToolCallEnd)]
    assert [call.index for call in calls] == [0, 1]
    assert [json.loads(call.arguments)["city"] for call in calls] == ["서울", "부산"]
    assert thought_signature(calls[0].provider_extra) is not None
    assert calls[1].provider_extra is None
    assert last_end(events).stop_reason == "tool_use"


async def test_recorded_text_reply_keeps_message_level_signature() -> None:
    fixture = load_fixture("text_only")
    adapter = make_adapter(ScriptedTransport([fixture["chunks"]]), models=[fixture["model"]])

    events = await collect(adapter, SNAPSHOT_REQUEST)

    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert text.startswith("안녕하세요")
    end = last_end(events)
    assert end.stop_reason == "end_turn"
    assert thought_signature(end.provider_extra) is not None


async def test_empty_length_response_retries_once_with_double_limit() -> None:
    empty = load_fixture("empty_response")["chunks"]
    transport = ScriptedTransport([empty, text_chunks("다시 받은 답")])
    adapter = make_adapter(transport, models=["gemini-3.7-flash"])

    events = await collect(adapter, user_request(params=ModelParams(max_output_tokens=8)))

    assert [body["max_completion_tokens"] for body in transport.bodies] == [8, 16]
    assert [event.text for event in events if isinstance(event, TextDelta)] == ["다시 받은 답"]
    assert last_end(events).stop_reason == "end_turn"


async def test_empty_response_is_retried_only_once() -> None:
    empty = load_fixture("empty_response")["chunks"]
    transport = ScriptedTransport([empty, empty])
    adapter = make_adapter(transport, models=["gemini-3.7-flash"])

    events = await collect(adapter, user_request())

    assert [body["max_completion_tokens"] for body in transport.bodies] == [16000, 32000]
    assert len(events) == 1
    assert last_end(events).stop_reason == "max_tokens"


async def test_empty_response_at_model_limit_is_not_retried() -> None:
    transport = ScriptedTransport([load_fixture("empty_response")["chunks"]])
    adapter = make_adapter(transport, models=["gemini-3.7-flash"])

    events = await collect(adapter, user_request(params=ModelParams(max_output_tokens=65_536)))

    assert len(transport.calls) == 1
    assert last_end(events).stop_reason == "max_tokens"


async def test_empty_stop_response_ends_without_retry() -> None:
    transport = ScriptedTransport(
        [[{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]]
    )
    adapter = make_adapter(transport)

    events = await collect(adapter, user_request())

    assert len(transport.calls) == 1
    assert last_end(events).stop_reason == "end_turn"


def test_request_body_shape() -> None:
    tool = ToolSpec(name="shell", description="run", input_schema=SHELL_SCHEMA)
    req = user_request(
        tools=[tool], params=ModelParams(temperature=0.2, reasoning_effort="minimal")
    )

    body = build_request_body(
        req, model="gemini-3.8-flash", caps=get_capabilities("gemini-3.8-flash"), max_tokens=900
    )

    assert body["model"] == "gemini-3.8-flash"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_completion_tokens"] == 900
    assert body["temperature"] == 0.2
    assert "top_p" not in body
    assert body["tools"] == [
        {
            "type": "function",
            "function": {"name": "shell", "description": "run", "parameters": SHELL_SCHEMA},
        }
    ]
    assert body["tool_choice"] == "auto"
    # 3.8은 minimal을 400으로 거부하므로 low로 올려 보낸다.
    assert body["reasoning_effort"] == "low"
    assert "parallel_tool_calls" not in body
    assert body["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
    ]


def test_request_body_forced_tool_minimal_effort_and_no_tools() -> None:
    tool = ToolSpec(name="submit_result", description="submit", input_schema={"type": "object"})
    forced = user_request(
        tools=[tool],
        params=ModelParams(
            tool_choice=ForcedTool(name="submit_result"), reasoning_effort="minimal"
        ),
    )
    plain = user_request()

    forced_body = build_request_body(
        forced, model="gemini-3.6-flash", caps=get_capabilities("gemini-3.6-flash"), max_tokens=10
    )
    plain_body = build_request_body(
        plain, model="gemini-3.6-flash", caps=get_capabilities("gemini-3.6-flash"), max_tokens=10
    )

    assert forced_body["tool_choice"] == {"type": "function", "function": {"name": "submit_result"}}
    assert forced_body["reasoning_effort"] == "minimal"
    assert "tools" not in plain_body
    assert "tool_choice" not in plain_body
    assert "reasoning_effort" not in plain_body
    assert "temperature" not in plain_body


async def test_max_output_tokens_is_capped_at_model_limit() -> None:
    transport = ScriptedTransport([text_chunks()])
    adapter = make_adapter(transport)

    await collect(adapter, user_request(params=ModelParams(max_output_tokens=100_000)))

    assert transport.bodies[0]["max_completion_tokens"] == 65_536


async def test_server_error_retries_same_key_once_then_next_model() -> None:
    transport = ScriptedTransport([http_error(503), http_error(503), text_chunks("from 3.7")])
    sleep = FakeSleep()
    adapter = make_adapter(transport, sleep=sleep)

    events = await collect(adapter, user_request())

    assert transport.models == ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.7-flash"]
    assert transport.key_indexes == [0, 0, 0]
    assert sleep.calls == [2.0]
    end = last_end(events)
    assert (end.model, end.key_index) == ("gemini-3.7-flash", 0)


async def test_connection_error_is_treated_like_server_error() -> None:
    lost = TransportError("connection reset", kind="connection")
    transport = ScriptedTransport([lost, lost, text_chunks()])
    adapter = make_adapter(transport)

    events = await collect(adapter, user_request())

    assert transport.models == ["gemini-3.8-flash", "gemini-3.8-flash", "gemini-3.7-flash"]
    assert last_end(events).model == "gemini-3.7-flash"


async def test_all_models_unavailable_returns_retryable_error() -> None:
    transport = ScriptedTransport([http_error(503)] * 6)
    sleep = FakeSleep()
    adapter = make_adapter(transport, sleep=sleep)

    events = await collect(adapter, user_request())

    assert transport.models == [model for model in MODELS for _ in range(2)]
    assert sleep.calls == [2.0, 2.0, 2.0]
    error = last_error(events)
    assert (error.code, error.retryable) == ("model_unavailable", True)


@pytest.mark.parametrize("status", [400, 404])
async def test_client_errors_fail_without_fallback(status: int) -> None:
    transport = ScriptedTransport([http_error(status, "INVALID_ARGUMENT: bad")])
    adapter = make_adapter(transport)

    events = await collect(adapter, user_request())

    assert len(transport.calls) == 1
    error = last_error(events)
    assert (error.code, error.retryable, error.status) == ("model_request_invalid", False, status)
    assert error.model == "gemini-3.8-flash"


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error_moves_to_next_key_on_the_same_model(status: int) -> None:
    transport = ScriptedTransport([http_error(status, "PERMISSION_DENIED: key"), text_chunks()])
    adapter = make_adapter(transport)

    events = await collect(adapter, user_request())

    assert transport.models == ["gemini-3.8-flash", "gemini-3.8-flash"]
    assert transport.key_indexes == [0, 1]
    end = last_end(events)
    assert (end.model, end.key_index) == ("gemini-3.8-flash", 1)
    assert [entry.disabled for entry in adapter.key_status()] == [True, False]


async def test_auth_errors_on_all_keys_end_as_model_auth() -> None:
    message = f"API_KEY_INVALID: {KEYS[1]} is not valid"
    transport = ScriptedTransport([http_error(401, "denied"), http_error(401, message)])
    adapter = make_adapter(transport)

    events = await collect(adapter, user_request())
    after = await collect(adapter, user_request())

    assert len(transport.calls) == 2
    error = last_error(events)
    assert (error.code, error.retryable, error.status) == ("model_auth", False, 401)
    assert "다시 띄운다" in error.message
    assert KEYS[1] not in error.message
    # 모든 키가 빠진 뒤에는 요청을 보내지 않고 바로 실패한다.
    assert last_error(after).code == "model_auth"
    assert len(transport.calls) == 2


async def test_requested_model_starts_the_chain_at_that_model() -> None:
    transport = ScriptedTransport([http_error(503), http_error(503), text_chunks()])
    adapter = make_adapter(transport)

    events = await collect(adapter, user_request(model="gemini-3.7-flash"))

    assert transport.models == ["gemini-3.7-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
    assert last_end(events).model == "gemini-3.6-flash"


async def test_unlisted_model_has_no_fallback() -> None:
    transport = ScriptedTransport([http_error(503), http_error(503)])
    adapter = make_adapter(transport)

    events = await collect(adapter, user_request(model="gemini-custom"))

    assert transport.models == ["gemini-custom", "gemini-custom"]
    assert last_error(events).code == "model_unavailable"


async def test_error_after_partial_output_is_not_retried() -> None:
    partial = [{"choices": [{"index": 0, "delta": {"content": "부분"}}]}]
    transport = ScriptedTransport([(partial, http_error(503, "UNAVAILABLE: overloaded"))])
    adapter = make_adapter(transport)

    events = await collect(adapter, user_request())

    assert len(transport.calls) == 1
    assert isinstance(events[0], TextDelta)
    error = last_error(events)
    assert (error.code, error.retryable) == ("model_stream_interrupted", True)


async def test_error_before_any_content_still_falls_back() -> None:
    role_only = [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]}]
    transport = ScriptedTransport(
        [(role_only, http_error(503)), (role_only, http_error(503)), text_chunks()]
    )
    adapter = make_adapter(transport)

    events = await collect(adapter, user_request())

    assert transport.models[-1] == "gemini-3.7-flash"
    assert last_end(events).model == "gemini-3.7-flash"


async def test_stream_ending_without_finish_reason() -> None:
    role_only = [{"choices": [{"index": 0, "delta": {"role": "assistant"}}]}]
    cut_text = [{"choices": [{"index": 0, "delta": {"content": "잘린"}}]}]
    retried = ScriptedTransport([role_only, text_chunks()])
    interrupted = ScriptedTransport([cut_text])

    retried_events = await collect(make_adapter(retried), user_request())
    interrupted_events = await collect(make_adapter(interrupted), user_request())

    assert retried.models == ["gemini-3.8-flash", "gemini-3.8-flash"]
    assert last_end(retried_events).stop_reason == "end_turn"
    assert last_error(interrupted_events).code == "model_stream_interrupted"


def signed_history(*, text_signature: bool = True) -> list[CanonicalMessage]:
    return [
        CanonicalMessage(role="user", blocks=[TextBlock(text="안녕")]),
        CanonicalMessage(
            role="assistant",
            blocks=[TextBlock(text="안녕하세요")],
            provider_extra=TEXT_SIGNATURE if text_signature else None,
        ),
        CanonicalMessage(role="user", blocks=[TextBlock(text="Get-ChildItem을 실행해")]),
        CanonicalMessage(
            role="assistant",
            blocks=[
                ToolUseBlock(
                    id="call_1",
                    name="shell",
                    input={"command": "Get-ChildItem"},
                    provider_extra=SIGNATURE,
                )
            ],
        ),
        CanonicalMessage(
            role="user", blocks=[ToolResultBlock(tool_use_id="call_1", content="a.txt")]
        ),
    ]


def sent_signatures(body: dict[str, Any]) -> tuple[list[str | None], list[str | None]]:
    texts: list[str | None] = []
    calls: list[str | None] = []
    for message in body["messages"]:
        if message["role"] != "assistant":
            continue
        texts.append(thought_signature(message))
        calls += [thought_signature(call) for call in message.get("tool_calls", [])]
    return texts, calls


async def test_signature_error_strips_text_signatures_and_retries_once() -> None:
    signature_error = http_error(400, "INVALID_ARGUMENT: Thought signature is not valid.")
    transport = ScriptedTransport([signature_error, text_chunks()])
    adapter = make_adapter(transport)
    req = ChatRequest(system="s", messages=signed_history())

    events = await collect(adapter, req)

    assert len(transport.calls) == 2
    real = thought_signature(SIGNATURE)
    assert sent_signatures(transport.bodies[0]) == (
        [thought_signature(TEXT_SIGNATURE), None],
        [real],
    )
    # 다시 보낼 때는 텍스트 응답의 서명만 뺀다. 도구 호출 서명을 빼면 그것대로 400이 난다.
    assert sent_signatures(transport.bodies[1]) == ([None, None], [real])
    assert transport.models == ["gemini-3.8-flash", "gemini-3.8-flash"]
    assert last_end(events).stop_reason == "end_turn"


async def test_signature_error_retry_happens_only_once() -> None:
    missing = http_error(400, "Function call is missing a thought_signature in functionCall parts.")
    transport = ScriptedTransport([missing, missing])
    adapter = make_adapter(transport)

    events = await collect(adapter, ChatRequest(system="s", messages=signed_history()))

    assert len(transport.calls) == 2
    assert last_error(events).code == "model_request_invalid"


@pytest.mark.parametrize(
    "messages",
    [
        [CanonicalMessage(role="user", blocks=[TextBlock(text="hello")])],
        # 도구 호출 서명만 있으면 다시 보내도 같은 본문이라 재시도하지 않는다.
        signed_history(text_signature=False),
    ],
)
async def test_signature_error_without_text_signature_is_not_retried(
    messages: list[CanonicalMessage],
) -> None:
    transport = ScriptedTransport([http_error(400, "missing a thought_signature")])
    adapter = make_adapter(transport)

    events = await collect(adapter, ChatRequest(system="s", messages=messages))

    assert len(transport.calls) == 1
    assert last_error(events).code == "model_request_invalid"


async def test_unsigned_tool_calls_are_resent_with_the_skip_signature() -> None:
    history = signed_history(text_signature=False)
    # 가짜 어댑터나 다른 모델이 만든 것처럼 서명 없는 병렬 호출 두 개를 앞에 끼운다.
    unsigned = CanonicalMessage(
        role="assistant",
        blocks=[
            ToolUseBlock(id="call_a", name="shell", input={"command": "pwd"}),
            ToolUseBlock(id="call_b", name="shell", input={"command": "ls"}),
        ],
    )
    results = CanonicalMessage(
        role="user",
        blocks=[
            ToolResultBlock(tool_use_id="call_a", content="/ws"),
            ToolResultBlock(tool_use_id="call_b", content="a.txt"),
        ],
    )
    messages = [history[0], unsigned, results, *history[2:]]
    transport = ScriptedTransport([text_chunks()])
    adapter = make_adapter(transport)

    await collect(adapter, ChatRequest(system="s", messages=messages))

    assert sent_signatures(transport.bodies[0]) == (
        [None, None],
        [SKIP_SIGNATURE, SKIP_SIGNATURE, thought_signature(SIGNATURE)],
    )
    # 정규 메시지 쪽은 바뀌지 않는다.
    assert all(
        isinstance(block, ToolUseBlock) and block.provider_extra is None
        for block in unsigned.blocks
    )


async def test_error_messages_never_contain_keys() -> None:
    google_style = "AIza" + "B" * 35
    message = f"API key {KEYS[0]} rejected, also {google_style}"
    transport = ScriptedTransport([http_error(400, message)])
    adapter = make_adapter(transport)

    events = await collect(adapter, user_request())

    error = last_error(events)
    assert KEYS[0] not in error.message
    assert google_style not in error.message
    assert "***" in error.message


async def test_closing_the_stream_early_closes_the_transport_stream() -> None:
    transport = ScriptedTransport([text_chunks("첫 조각")])
    adapter = make_adapter(transport)

    stream = adapter.stream(user_request())
    assert isinstance(stream, AsyncGenerator)
    first = await anext(stream)
    await stream.aclose()

    assert isinstance(first, TextDelta)
    assert transport.closed_streams == 1


async def test_missing_keys_or_models_fail_cleanly() -> None:
    no_keys = GeminiAdapter(api_keys=[], model_fallback=MODELS, transport=ScriptedTransport())
    no_models = GeminiAdapter(api_keys=KEYS, model_fallback=[], transport=ScriptedTransport())

    assert last_error(await collect(no_keys, user_request())).code == "model_auth"
    assert last_error(await collect(no_models, user_request())).code == "model_request_invalid"


async def test_estimate_tokens_is_calibrated_by_last_usage() -> None:
    req = user_request("이 문장은 한국어라서 휴리스틱이 실제보다 작게 나온다.")
    heuristic = estimate_request(req)
    transport = ScriptedTransport([text_chunks(prompt_tokens=heuristic * 2)])
    adapter = make_adapter(transport)
    assert adapter.estimate_tokens(req) == heuristic

    await collect(adapter, req)

    assert adapter.estimate_tokens(req) == heuristic * 2


def test_model_chain_follows_fallback_order() -> None:
    adapter = make_adapter(ScriptedTransport())

    assert adapter.model_chain(None) == MODELS
    assert adapter.model_chain("gemini-3.7-flash") == MODELS[1:]
    assert adapter.model_chain("gemini-other") == ["gemini-other"]


def test_adapter_satisfies_model_adapter_protocol() -> None:
    adapter = make_adapter(ScriptedTransport())

    assert isinstance(adapter, ModelAdapter)
    assert adapter.name == "gemini"
    assert adapter.capabilities("gemini-3.8-flash").context_window == 1_048_576


async def test_list_models_rotates_past_rate_limited_key() -> None:
    transport = ScriptedTransport()
    transport.list_steps = [http_error(429), ["gemini-3.8-flash", "gemini-3.6-flash"]]
    adapter = make_adapter(transport)

    models = await adapter.list_models()

    assert models == ["gemini-3.8-flash", "gemini-3.6-flash"]
    assert transport.list_calls == [0, 1]


async def test_list_models_skips_a_key_with_auth_error() -> None:
    transport = ScriptedTransport()
    transport.list_steps = [http_error(403, "PERMISSION_DENIED"), ["gemini-3.8-flash"]]
    adapter = make_adapter(transport)

    models = await adapter.list_models()

    assert models == ["gemini-3.8-flash"]
    assert transport.list_calls == [0, 1]
    assert [entry.disabled for entry in adapter.key_status()] == [True, False]


async def test_list_models_auth_error_on_every_key_raises() -> None:
    transport = ScriptedTransport()
    transport.list_steps = [http_error(401, f"bad key {KEYS[0]}"), http_error(401, "bad key")]
    adapter = make_adapter(transport)

    with pytest.raises(LLMError) as caught:
        await adapter.list_models()
    with pytest.raises(LLMError) as again:
        await adapter.list_models()

    assert caught.value.code == again.value.code == "model_auth"
    assert KEYS[0] not in caught.value.message
    assert transport.list_calls == [0, 1]


def sse(*payloads: object) -> bytes:
    events = "".join(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n" for payload in payloads)
    return (events + "data: [DONE]\n\n").encode("utf-8")


def sdk_transport(handler: Any) -> OpenAICompatTransport:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAICompatTransport("https://example.test/v1beta/openai", http_client=client)


async def test_sdk_transport_streams_raw_chunks() -> None:
    chunks = load_fixture("tool_call_single")["chunks"]
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(*chunks)
        )

    transport = sdk_transport(handler)
    body = {"model": "gemini-3.8-flash", "messages": [], "stream": True}

    got = [chunk async for chunk in transport.stream_chat(api_key=KEYS[1], body=body)]
    await transport.aclose()

    # extra_content까지 응답 JSON 그대로 받아야 한다.
    assert got == chunks
    assert seen[0].url.path == "/v1beta/openai/chat/completions"
    assert seen[0].headers["authorization"] == f"Bearer {KEYS[1]}"
    assert json.loads(seen[0].content) == body


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (
            429,
            [{"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}],
            "RESOURCE_EXHAUSTED: Quota exceeded",
        ),
        (
            503,
            [{"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}}],
            "UNAVAILABLE: high demand",
        ),
        (400, {"error": {"message": "bad field"}}, "bad field"),
    ],
)
async def test_sdk_transport_maps_status_errors(status: int, body: object, message: str) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(status, json=body)

    transport = sdk_transport(handler)

    with pytest.raises(TransportError) as caught:
        async for _ in transport.stream_chat(api_key=KEYS[0], body={"model": "m"}):
            pass

    assert (caught.value.status, caught.value.kind, caught.value.message) == (
        status,
        "http",
        message,
    )
    # SDK 자체 재시도는 꺼져 있어야 한다(키 순환은 어댑터가 한다).
    assert len(requests) == 1


async def test_sdk_transport_maps_error_event_inside_stream() -> None:
    first = {"choices": [{"index": 0, "delta": {"content": "앞부분"}}]}
    failure = {"error": {"code": 503, "message": "backend died", "status": "UNAVAILABLE"}}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=sse(first, failure)
        )

    transport = sdk_transport(handler)
    got: list[dict[str, Any]] = []

    with pytest.raises(TransportError) as caught:
        async for chunk in transport.stream_chat(api_key=KEYS[0], body={"model": "m"}):
            got.append(chunk)

    assert got == [first]
    assert (caught.value.status, caught.value.kind) == (503, "stream")
    assert caught.value.message == "UNAVAILABLE: backend died"


async def test_sdk_transport_maps_connection_errors() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    transport = sdk_transport(handler)

    with pytest.raises(TransportError) as caught:
        async for _ in transport.stream_chat(api_key=KEYS[0], body={"model": "m"}):
            pass

    assert (caught.value.status, caught.value.kind) == (None, "connection")


async def test_sdk_transport_lists_models_without_prefix() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1beta/openai/models"
        data = [
            {"id": "models/gemini-3.8-flash", "object": "model", "owned_by": "google"},
            {"id": "models/gemini-3.6-flash", "object": "model", "owned_by": "google"},
        ]
        return httpx2.Response(200, json={"object": "list", "data": data})

    transport = sdk_transport(handler)

    assert await transport.list_models(api_key=KEYS[0]) == ["gemini-3.8-flash", "gemini-3.6-flash"]
