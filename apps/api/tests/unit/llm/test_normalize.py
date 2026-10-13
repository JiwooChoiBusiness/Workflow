import json
from pathlib import Path
from typing import Any

import pytest

from app.llm.gemini_adapter import (
    SKIP_SIGNATURE,
    StreamNormalizer,
    from_openai_messages,
    normalize_stop_reason,
    normalize_usage,
    thought_signature,
    to_openai_messages,
)
from app.llm.pricing import ModelPrice, compute_cost, cost_usd
from app.llm.token_estimate import (
    BLOCK_OVERHEAD,
    IMAGE_TOKENS,
    estimate_block,
    estimate_request,
    estimate_text,
)
from app.llm.types import (
    CanonicalMessage,
    ChatRequest,
    ImageBlock,
    TextBlock,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

FIXTURES = Path(__file__).parent / "fixtures"
SIG_A = {"extra_content": {"google": {"thought_signature": "c2lnLWE="}}}
SIG_B = {"extra_content": {"google": {"thought_signature": "c2lnLWI="}}}


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("raw", "has_calls", "expected"),
    [
        ("stop", False, "end_turn"),
        ("stop", True, "tool_use"),
        ("tool_calls", True, "tool_use"),
        ("length", False, "max_tokens"),
        ("length", True, "max_tokens"),
        ("content_filter", False, "refusal"),
        ("something_new", False, "end_turn"),
    ],
)
def test_stop_reason_normalization(raw: str, has_calls: bool, expected: str) -> None:
    assert normalize_stop_reason(raw, has_tool_calls=has_calls) == expected


def test_usage_counts_gemini_hidden_thinking_as_output() -> None:
    # 녹음한 빈 응답의 usage. completion에는 사고 토큰이 없고 total에만 들어 있다.
    usage = normalize_usage({"completion_tokens": 0, "prompt_tokens": 20, "total_tokens": 25})

    assert usage == Usage(input_tokens=20, output_tokens=5, reasoning_tokens=5)


def test_usage_openai_style_details() -> None:
    usage = normalize_usage(
        {
            "prompt_tokens": 50,
            "completion_tokens": 30,
            "total_tokens": 80,
            "completion_tokens_details": {"reasoning_tokens": 12},
            "prompt_tokens_details": {"cached_tokens": 20},
        }
    )

    assert usage == Usage(
        input_tokens=50, output_tokens=30, reasoning_tokens=12, cached_input_tokens=20
    )


def test_usage_without_total_or_at_all() -> None:
    assert normalize_usage({"prompt_tokens": 5, "completion_tokens": 3}) == Usage(
        input_tokens=5, output_tokens=3
    )
    assert normalize_usage(None) is None
    assert normalize_usage({}) is None


def history() -> list[CanonicalMessage]:
    return [
        CanonicalMessage(role="user", blocks=[TextBlock(text="파일 목록을 보여줘")]),
        CanonicalMessage(
            role="assistant",
            blocks=[
                TextBlock(text="확인할게요."),
                ToolUseBlock(
                    id="call_1",
                    name="shell",
                    input={"command": "Get-ChildItem"},
                    provider_extra=SIG_A,
                ),
                ToolUseBlock(id="call_2", name="read_file", input={"path": "메모.txt"}),
            ],
        ),
        CanonicalMessage(
            role="user",
            blocks=[
                ToolResultBlock(tool_use_id="call_1", content="메모.txt"),
                ToolResultBlock(tool_use_id="call_2", content="없음", is_error=True),
                TextBlock(text="이어서 요약해줘"),
            ],
        ),
        CanonicalMessage(
            role="assistant", blocks=[TextBlock(text="요약했습니다.")], provider_extra=SIG_B
        ),
    ]


def test_to_openai_messages_layout() -> None:
    messages = to_openai_messages("시스템", history())

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "tool",
        "tool",
        "user",
        "assistant",
    ]
    assert messages[0] == {"role": "system", "content": "시스템"}
    assert messages[2] == {
        "role": "assistant",
        "content": "확인할게요.",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "shell", "arguments": '{"command":"Get-ChildItem"}'},
                "extra_content": SIG_A["extra_content"],
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"메모.txt"}'},
                # 병렬 호출의 두 번째부터는 서명이 없어 검증 생략용 더미가 붙는다.
                "extra_content": {"google": {"thought_signature": SKIP_SIGNATURE}},
            },
        ],
    }
    # tool 메시지가 먼저, 같은 user 메시지의 텍스트는 그 뒤 user 메시지로 간다.
    assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "메모.txt"}
    assert messages[4] == {"role": "tool", "tool_call_id": "call_2", "content": "없음"}
    assert messages[5] == {"role": "user", "content": "이어서 요약해줘"}
    assert messages[6] == {
        "role": "assistant",
        "content": "요약했습니다.",
        "extra_content": SIG_B["extra_content"],
    }


def test_round_trip_canonical_to_openai_and_back() -> None:
    original = history()
    # is_error는 OpenAI 형식에 자리가 없어 돌아오지 않는다.
    original[2].blocks[1] = ToolResultBlock(tool_use_id="call_2", content="없음")

    system, restored = from_openai_messages(to_openai_messages("시스템", original))

    assert system == "시스템"
    assert restored == original


def test_round_trip_of_recorded_request_keeps_signatures() -> None:
    sent = load_fixture("tool_result_followup")["request"]["messages"]

    system, canonical = from_openai_messages(sent)

    assert to_openai_messages(system, canonical) == sent
    text_turn, tool_turn = canonical[1], canonical[3]
    assert thought_signature(text_turn.provider_extra) is not None
    tool_use = tool_turn.blocks[0]
    assert isinstance(tool_use, ToolUseBlock)
    assert thought_signature(tool_use.provider_extra) is not None


def test_foreign_provider_extra_is_not_sent() -> None:
    message = CanonicalMessage(
        role="assistant",
        blocks=[
            ToolUseBlock(
                id="c",
                name="shell",
                input={},
                provider_extra={"other_provider": {"blob": 1}, **SIG_A},
            )
        ],
        provider_extra={"other_provider": {"blob": 2}},
    )

    [converted] = to_openai_messages("", [message])

    assert set(converted) == {"role", "content", "tool_calls"}
    assert set(converted["tool_calls"][0]) == {"id", "type", "function", "extra_content"}


def test_strip_text_signatures_keeps_tool_call_signatures() -> None:
    messages = to_openai_messages("s", history(), strip_text_signatures=True)

    first, second = messages[2]["tool_calls"]
    assert thought_signature(first) == thought_signature(SIG_A)
    assert thought_signature(second) == SKIP_SIGNATURE
    assert messages[6] == {"role": "assistant", "content": "요약했습니다."}


def test_skip_signature_is_dropped_when_converting_back() -> None:
    sent = to_openai_messages("", history())

    _, restored = from_openai_messages(sent)

    calls = [block for block in restored[1].blocks if isinstance(block, ToolUseBlock)]
    assert [call.provider_extra for call in calls] == [SIG_A, None]


def test_images_become_data_urls_or_placeholders() -> None:
    image = ImageBlock(media_type="image/png", data_b64="iVBORw0KGgo=", filename="그림.png")
    stored_only = ImageBlock(media_type="image/jpeg", storage_key="uploads/x", filename="사진.jpg")
    message = CanonicalMessage(role="user", blocks=[TextBlock(text="이 그림"), image, stored_only])

    [with_images] = to_openai_messages("", [message])
    [without_images] = to_openai_messages("", [message], supports_images=False)

    assert with_images["content"] == [
        {"type": "text", "text": "이 그림"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        {"type": "text", "text": "[이미지 첨부: 사진.jpg]"},
    ]
    assert without_images["content"][1] == {"type": "text", "text": "[이미지 첨부: 그림.png]"}


def test_empty_assistant_message_is_not_sent() -> None:
    messages = [
        CanonicalMessage(role="user", blocks=[TextBlock(text="a")]),
        CanonicalMessage(role="assistant", blocks=[], provider_extra=SIG_A),
        CanonicalMessage(role="user", blocks=[TextBlock(text="b")]),
    ]

    assert [m["role"] for m in to_openai_messages("", messages)] == ["user", "user"]


def tool_chunk(call: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "delta": {"tool_calls": [call]}}
    if finish:
        choice["finish_reason"] = finish
    return {"choices": [choice]}


def test_normalizer_without_index_uses_ids_to_split_calls() -> None:
    normalizer = StreamNormalizer()

    events = [
        *normalizer.feed(
            tool_chunk({"id": "a", "function": {"name": "shell", "arguments": '{"command":'}})
        ),
        *normalizer.feed(tool_chunk({"function": {"arguments": '"dir"}'}})),
        *normalizer.feed(
            tool_chunk({"id": "b", "function": {"name": "list_files", "arguments": "{}"}})
        ),
        *normalizer.feed({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
    ]

    assert [type(event) for event in events] == [
        ToolCallStart,
        ToolCallDelta,
        ToolCallDelta,
        ToolCallEnd,
        ToolCallStart,
        ToolCallDelta,
        ToolCallEnd,
    ]
    ends = [event for event in events if isinstance(event, ToolCallEnd)]
    assert [(end.index, end.id, end.arguments) for end in ends] == [
        (0, "a", '{"command":"dir"}'),
        (1, "b", "{}"),
    ]


def test_normalizer_same_index_with_new_id_starts_new_call() -> None:
    normalizer = StreamNormalizer()

    normalizer.feed(tool_chunk({"index": 0, "id": "a", "function": {"name": "x"}}))
    events = normalizer.feed(tool_chunk({"index": 0, "id": "b", "function": {"name": "y"}}))

    assert [type(event) for event in events] == [ToolCallEnd, ToolCallStart]
    assert [call.id for call in normalizer.tool_calls] == ["a", "b"]


def test_normalizer_generates_missing_call_id() -> None:
    normalizer = StreamNormalizer()

    [start] = normalizer.feed(tool_chunk({"function": {"name": "shell"}}))

    assert isinstance(start, ToolCallStart)
    assert start.id.startswith("call_")


def test_normalizer_merges_extra_content_across_chunks() -> None:
    normalizer = StreamNormalizer()
    normalizer.feed(tool_chunk({"id": "a", "function": {"name": "x"}, **SIG_A}))
    normalizer.feed(tool_chunk({"extra_content": {"google": {"note": 1}}}))
    normalizer.feed({"choices": [{"index": 0, "delta": {"extra_content": {"google": {"m": 1}}}}]})
    normalizer.feed({"choices": [{"index": 0, "delta": {"extra_content": {"google": {"n": 2}}}}]})

    [end] = normalizer.close()

    assert isinstance(end, ToolCallEnd)
    assert end.provider_extra == {
        "extra_content": {"google": {"thought_signature": "c2lnLWE=", "note": 1}}
    }
    assert normalizer.message_extra == {"extra_content": {"google": {"m": 1, "n": 2}}}


def test_normalizer_ignores_other_choices_and_keeps_last_usage() -> None:
    normalizer = StreamNormalizer()

    events = [
        *normalizer.feed(
            {
                "choices": [
                    {"index": 1, "delta": {"content": "다른 후보"}},
                    {"index": 0, "delta": {"content": "본문"}},
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ),
        *normalizer.feed({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 9}}),
    ]

    assert events == [TextDelta(text="본문")]
    assert normalizer.raw_usage == {"prompt_tokens": 1, "completion_tokens": 9}


def test_thought_signature_helper() -> None:
    assert thought_signature(SIG_A) == "c2lnLWE="
    assert thought_signature(None) is None
    assert thought_signature({"extra_content": {"google": {}}}) is None


def test_cost_assumes_cached_input_at_quarter_price_by_default() -> None:
    usage = Usage(input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=100_000)
    price = ModelPrice(input_per_mtok=1.0, output_per_mtok=4.0)

    assert compute_cost(usage, price) == pytest.approx(0.6 + 0.1 + 0.4)
    explicit = ModelPrice(input_per_mtok=1.0, output_per_mtok=4.0, cached_input_per_mtok=0.5)
    assert compute_cost(usage, explicit) == pytest.approx(0.6 + 0.2 + 0.4)


def test_cost_only_with_price_table() -> None:
    usage = Usage(input_tokens=2_000_000, output_tokens=0)
    prices = {"gemini-3.8-flash": {"input_per_mtok": 0.5, "output_per_mtok": 3.0}}

    assert cost_usd("gemini-3.8-flash", usage, prices) == pytest.approx(1.0)
    assert cost_usd("gemini-3.6-flash", usage, prices) is None
    assert cost_usd("gemini-3.8-flash", usage, None) is None
    assert cost_usd("gemini-3.8-flash", usage, {}) is None


def test_token_estimates() -> None:
    assert estimate_text("") == 0
    assert estimate_text("abcdefg") == 2
    image = ImageBlock(media_type="image/png", data_b64="A" * 100_000)
    assert estimate_block(image) == IMAGE_TOKENS + BLOCK_OVERHEAD

    bare = ChatRequest(system="s", messages=[])
    with_tool = ChatRequest(
        system="s",
        messages=[],
        tools=[ToolSpec(name="shell", description="run", input_schema={"type": "object"})],
    )
    assert estimate_request(with_tool) > estimate_request(bare) > 0
