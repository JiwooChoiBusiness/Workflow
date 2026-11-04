"""에이전트 런 이벤트·메시지 행의 비밀값 가리기(1단계 S8, 3단계 KS9). 끝까지의 흐름은
integration/test_agent_knowledge.py가 본다."""

from typing import Any

from app.services.run_executor import RunMasker

SECRET = "tok-KS9-0123456789abcdef"  # noqa: S105  시험용 가짜 값


def joined(events: list[tuple[str, Any]], kind: str, field: str = "text") -> str:
    return "".join(str(data[field]) for event, data in events if event == kind)


def test_deltas_split_across_a_secret_are_masked_per_stream() -> None:
    masker = RunMasker([SECRET])
    out: list[tuple[str, Any]] = []

    out += masker.event("message.delta", {"message_id": "m1", "text": "값은 " + SECRET[:6]})
    out += masker.event(
        "tool_call.output_delta", {"tool_call_id": "c1", "stream": "stdout", "text": SECRET[:3]}
    )
    out += masker.event("message.delta", {"message_id": "m1", "text": SECRET[6:] + " 이다"})
    out += masker.event(
        "tool_call.output_delta", {"tool_call_id": "c1", "stream": "stdout", "text": SECRET[3:]}
    )
    # 델타가 아닌 이벤트 앞에서 붙잡아 둔 끝부분을 낸다.
    out += masker.event("tool_call.completed", {"tool_call_id": "c1", "output_excerpt": SECRET})

    assert joined(out, "message.delta") == "값은 *** 이다"
    assert joined(out, "tool_call.output_delta") == "***"
    assert out[-1] == ("tool_call.completed", {"tool_call_id": "c1", "output_excerpt": "***"})
    assert SECRET not in repr(out)


def test_flush_releases_a_held_tail_that_was_not_a_secret() -> None:
    masker = RunMasker([SECRET])

    first = masker.event("message.delta", {"message_id": "m1", "text": "끝은 tok-KS"})

    assert joined(first, "message.delta") == "끝은 "
    assert masker.flush() == [("message.delta", {"message_id": "m1", "text": "tok-KS"})]
    assert masker.flush() == []


def test_a_held_tail_comes_out_before_a_tool_call_starts() -> None:
    # 글 끝이 비밀값의 앞부분처럼 보여 붙잡아 둔 사이 모델이 도구 호출을 시작하면, 붙잡은 끝을
    # 먼저 내고 tool_call.started를 낸다. 순서가 바뀌면 화면의 글 끝이 도구 카드 뒤로 밀린다.
    masker = RunMasker([SECRET])
    out: list[tuple[str, Any]] = []

    out += masker.event("message.delta", {"message_id": "m1", "text": "확인할게요 tok-K"})
    out += masker.event("tool_call.started", {"tool_call_id": "c1", "name": "shell"})
    out += masker.event("tool_call.input_delta", {"tool_call_id": "c1", "json_fragment": "{}"})

    assert [kind for kind, _ in out] == [
        "message.delta",
        "message.delta",
        "tool_call.started",
        "tool_call.input_delta",
    ]
    assert [data.get("text") for _, data in out[:2]] == ["확인할게요 ", "tok-K"]


def test_stored_blocks_keep_tool_inputs_for_resuming() -> None:
    masker = RunMasker([SECRET])
    blocks = [
        {"type": "text", "text": f"토큰 {SECRET}", "extra": {"sig": "x"}},
        {"type": "tool_use", "id": "c1", "name": "shell", "input": {"command": f"echo {SECRET}"}},
        {"type": "tool_result", "tool_use_id": "c1", "content": SECRET, "is_error": False},
    ]

    masked = masker.blocks(blocks)

    assert masked[0] == {"type": "text", "text": "토큰 ***", "extra": {"sig": "x"}}
    # 승인 뒤 재개할 때 그대로 실행하는 입력이다.
    assert masked[1] == blocks[1]
    assert masked[2]["content"] == "***"


def test_without_secrets_events_pass_through() -> None:
    masker = RunMasker(["short"])
    data = {"message_id": "m1", "text": "short"}

    assert masker.event("message.delta", data) == [("message.delta", data)]
    assert masker.flush() == []
