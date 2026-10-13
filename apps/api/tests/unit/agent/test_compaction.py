"""컨텍스트 압축(설계 4.7): 예산, 추정, "전체 요약 1개", 요약 뒤 히스토리."""

import uuid
from collections.abc import Callable
from typing import Any

from app.agent.context import (
    SUMMARY_FOLLOW_UP,
    SUMMARY_OPEN,
    ContextTracker,
    StoredMessage,
    build_history,
    context_budget,
    history_transcript,
)
from app.agent.prompt_builder import load_prompt
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.token_estimate import estimate_request
from app.llm.types import (
    CanonicalMessage,
    ChatRequest,
    ErrorEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

Harness = Callable[..., Any]
BIG = Usage(input_tokens=250_000, output_tokens=100)


def user(text: str) -> CanonicalMessage:
    return CanonicalMessage(role="user", blocks=[TextBlock(text=text)])


def test_budget_is_the_smaller_of_200k_and_60_percent_of_the_window() -> None:
    assert context_budget(None, 1_048_576) == 200_000
    assert context_budget(None, 100_000) == 60_000
    assert context_budget(5_000, 1_048_576) == 5_000


def test_tracker_uses_last_usage_plus_new_messages() -> None:
    tracker = ContextTracker(estimate_request)
    first = ChatRequest(system="s", messages=[user("안녕")])
    assert tracker.estimate(first) == estimate_request(first)

    tracker.observe(input_tokens=10_000, sent_messages=1)
    grown = ChatRequest(system="s", messages=[user("안녕"), user("더 긴 질문" * 10)])
    assert tracker.estimate(grown) > 10_000
    assert tracker.estimate(grown) < 10_000 + estimate_request(grown)

    tracker.reset()
    assert tracker.estimate(grown) == estimate_request(grown)


async def test_large_usage_triggers_one_summary_and_restarts_history(harness: Harness) -> None:
    summary = "1. 사용자의 목표: 보고서 작성\n2. 지금까지 한 일: notes.md를 읽었다"
    run = harness(
        [
            [*fake_tool_call("list_files", {}, call_id="call_1"), fake_end("tool_use", usage=BIG)],
            fake_text(summary),
            [*fake_text("요약을 바탕으로 이어서 끝냈다."), fake_end()],
        ]
    )

    outcome = await run.run()

    assert outcome.status == "succeeded"
    summary_request = run.adapter.requests[1]
    assert summary_request.system == load_prompt("compaction.md")
    assert summary_request.tools == []
    assert summary_request.params.max_output_tokens == 8000
    assert summary_request.params.reasoning_effort == "low"
    request_text = summary_request.messages[0].blocks[0]
    assert isinstance(request_text, TextBlock)
    assert "[도구 호출 list_files call_1]" in request_text.text
    assert "작업을 해 줘" in request_text.text

    [compacted] = run.ports.of("context.compacted")
    assert compacted["before_tokens"] > 200_000
    assert compacted["after_tokens"] < compacted["before_tokens"]
    assert run.ports.summaries == [summary]

    continued = run.adapter.requests[2]
    assert len(continued.messages) == 1
    block = continued.messages[0].blocks[0]
    assert isinstance(block, TextBlock)
    assert block.text.startswith(SUMMARY_OPEN) and block.text.endswith(SUMMARY_FOLLOW_UP)
    assert summary in block.text
    # 요약 호출의 토큰도 런 사용량에 들어간다.
    assert len(run.ports.of("usage")) == 3


async def test_failed_summary_keeps_full_history(harness: Harness) -> None:
    run = harness(
        [
            [*fake_tool_call("list_files", {}, call_id="call_1"), fake_end("tool_use", usage=BIG)],
            [ErrorEvent(code="model_unavailable", message="429", retryable=True)],
            [*fake_text("압축 없이 이어 갔다."), fake_end()],
        ]
    )

    outcome = await run.run()

    assert outcome.status == "succeeded"
    assert run.ports.of("context.compacted") == []
    assert len(run.adapter.requests[2].messages) == 3


def test_history_restarts_from_the_last_summary() -> None:
    def row(seq: int, role: str, text: str, blocks: list[dict[str, Any]] | None = None) -> Any:
        return StoredMessage(
            id=uuid.uuid4(),
            seq=seq,
            role=role,  # pyright: ignore[reportArgumentType]
            blocks=blocks if blocks is not None else [{"type": "text", "text": text}],
            text=text,
        )

    rows = [
        row(1, "user", "옛 질문"),
        row(2, "assistant", "옛 답"),
        row(3, "system_summary", "앞의 대화 요약"),
        row(4, "user", "새 질문"),
        row(
            5,
            "assistant",
            "",
            [
                {"type": "tool_use", "id": "c1", "name": "shell", "input": {"command": "dir"}},
                {"type": "tool_use", "id": "c2", "name": "shell", "input": {"command": "ls"}},
                {"type": "tool_result", "tool_use_id": "c1", "content": "ok", "is_error": False},
            ],
        ),
    ]

    history = build_history(rows)

    assert [message.role for message in history] == ["user", "assistant", "user"]
    first = history[0].blocks
    # 요약과 새 질문은 이어진 user 메시지 하나로 합쳐진다.
    assert isinstance(first[0], TextBlock) and first[0].text.startswith(SUMMARY_OPEN)
    assert isinstance(first[1], TextBlock) and first[1].text == "새 질문"
    assert all(not isinstance(block, TextBlock) or "옛" not in block.text for block in first)
    calls = [block for block in history[1].blocks if isinstance(block, ToolUseBlock)]
    assert [call.id for call in calls] == ["c1", "c2"]
    results = [block for block in history[2].blocks if isinstance(block, ToolResultBlock)]
    # 끝나지 못한 c2에는 실행되지 않았다는 결과를 채운다.
    assert [(result.tool_use_id, result.is_error) for result in results] == [
        ("c1", False),
        ("c2", True),
    ]


def test_transcript_clips_long_tool_results() -> None:
    messages = [
        user("질문"),
        CanonicalMessage(
            role="assistant",
            blocks=[ToolUseBlock(id="c1", name="shell", input={"command": "type big.log"})],
        ),
        CanonicalMessage(
            role="user", blocks=[ToolResultBlock(tool_use_id="c1", content="x" * 10_000)]
        ),
    ]

    text = history_transcript(messages)

    assert "[사용자]\n질문" in text
    assert "[도구 호출 shell c1]" in text
    assert "자 생략" in text
    assert len(text) < 6_000
