"""루프의 call_agent 멈춤·재개(오케스트레이션 3.1, 3.2, 3.4)."""

import uuid
from collections.abc import Callable
from typing import Any

from app.agent.child_runs import (
    ChildCall,
    ChildResultSummary,
    ChildStarted,
    ChildStartError,
    SubAgentChoice,
)
from app.agent.interactions import ResumePayload
from app.agent.tools.call_agent import CallAgentTool
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import ChatRequest, ToolResultBlock

Harness = Callable[..., Any]
RESEARCHER = SubAgentChoice(
    agent_id=uuid.UUID("01920000-0000-7000-8000-00000000000a"),
    slug="researcher",
    name="조사 담당",
    description="근거를 모은다",
)


def started_for(call: ChildCall) -> ChildStarted:
    return ChildStarted(
        child_run_id=uuid.UUID("01920000-0000-7000-8000-0000000000c1"),
        child_session_id=uuid.UUID("01920000-0000-7000-8000-0000000000c2"),
        agent_id=call.agent.agent_id,
        agent_slug=call.agent.slug,
        agent_name=call.agent.name,
        version="published",
        depth=1,
        result_format="text",
        task_preview=call.task,
    )


def last_results(request: ChatRequest) -> list[ToolResultBlock]:
    return [block for block in request.messages[-1].blocks if isinstance(block, ToolResultBlock)]


def child_summary(**fields: Any) -> ChildResultSummary:
    values: dict[str, Any] = {
        "run_id": uuid.UUID("01920000-0000-7000-8000-0000000000c1"),
        "agent_slug": "researcher",
        "agent_name": "조사 담당",
        "status": "succeeded",
        "text": "근거 A와 B",
        "steps": 2,
        "duration_ms": 900,
    }
    values.update(fields)
    return ChildResultSummary.model_validate(values)


def call_agent(call_id: str, **args: Any) -> list[Any]:
    return fake_tool_call(
        "call_agent", {"agent": "researcher", "task": "조사", **args}, call_id=call_id
    )


async def test_call_agent_pauses_without_waiting_input_event(harness: Harness) -> None:
    run = harness(
        [[*call_agent("call_c", files=["notes.md"]), fake_end("tool_use")]],
        extra_tools=[CallAgentTool([RESEARCHER])],
    )
    run.ports.child_reply = started_for

    outcome = await run.run()

    assert outcome.status == "waiting_input"
    pending = outcome.pending
    assert pending is not None and pending.kind == "agent_call"
    assert pending.child_run_id == uuid.UUID("01920000-0000-7000-8000-0000000000c1")
    assert pending.agent_slug == "researcher" and pending.tool_call_id == "call_c"
    assert pending.result is not None and pending.result.format == "text"
    [call] = run.ports.child_calls
    assert call.files == ["notes.md"] and call.message_id == pending.message_id
    assert "run.waiting_input" not in run.ports.types()
    [started] = run.ports.of("child_run.started")
    assert started["tool_call_id"] == "call_c"
    assert started["child_session_id"] == "01920000-0000-7000-8000-0000000000c2"
    assert started["agent_name"] == "조사 담당" and started["version"] == "published"
    # 아직 결과가 없으므로 tool_call.completed도 없다.
    assert run.ports.of("tool_call.completed") == []


async def test_resume_records_the_child_result_and_continues(harness: Harness) -> None:
    run = harness(
        [
            [*fake_text("맡긴다."), *call_agent("call_c"), fake_end("tool_use")],
            [*fake_text("결과를 합쳤다."), fake_end()],
        ],
        extra_tools=[CallAgentTool([RESEARCHER])],
    )
    run.ports.child_reply = started_for
    paused = await run.run()
    assert paused.pending is not None

    outcome = await run.resume(
        paused,
        ResumePayload(
            interaction_id=paused.pending.interaction_id,
            kind="agent_call",
            child=child_summary(),
        ),
    )

    assert outcome.status == "succeeded"
    [completed] = run.ports.of("tool_call.completed")
    assert completed["tool_call_id"] == "call_c"
    assert completed["status"] == "ok"
    assert completed["child_run_id"] == "01920000-0000-7000-8000-0000000000c1"
    assert completed["duration_ms"] == 900
    assert completed["exit_code"] is None and completed["timed_out"] is False
    assert completed["output_bytes"] == len(completed["output_excerpt"].encode("utf-8"))
    [result] = last_results(run.adapter.requests[-1])
    assert result.tool_use_id == "call_c" and not result.is_error
    assert result.content.startswith("[하위 에이전트 researcher(조사 담당) 완료")
    assert result.content.endswith("근거 A와 B")


async def test_call_agent_in_the_middle_of_a_parallel_batch(harness: Harness) -> None:
    run = harness(
        [
            [
                *fake_tool_call(
                    "write_file", {"path": "a.txt", "content": "A"}, index=0, call_id="w"
                ),
                *call_agent("call_c"),
                *fake_tool_call("read_file", {"path": "a.txt"}, index=2, call_id="r"),
                fake_end("tool_use"),
            ],
            [*fake_text("다 했다."), fake_end()],
        ],
        extra_tools=[CallAgentTool([RESEARCHER])],
    )
    run.ports.child_reply = started_for

    paused = await run.run()

    assert paused.pending is not None
    assert paused.pending.completed_tool_call_ids == ["w"]
    assert run.backend.files["a.txt"] == b"A"
    outcome = await run.resume(
        paused,
        ResumePayload(
            interaction_id=paused.pending.interaction_id,
            kind="agent_call",
            child=child_summary(status="failed", error={"code": "timeout", "message": "한도"}),
        ),
    )

    assert outcome.status == "succeeded"
    ids = [item["tool_call_id"] for item in run.ports.of("tool_call.completed")]
    assert ids == ["w", "call_c", "r"]
    results = {item.tool_use_id: item for item in last_results(run.adapter.requests[-1])}
    assert results["call_c"].is_error and "실패: timeout" in results["call_c"].content
    assert not results["r"].is_error


async def test_child_start_error_is_a_tool_error_and_the_run_goes_on(harness: Harness) -> None:
    run = harness(
        [
            [*call_agent("call_c"), fake_end("tool_use")],
            [*fake_tool_call("call_agent", {"agent": "nobody", "task": "x"}, call_id="bad")],
            [*fake_text("직접 했다."), fake_end()],
        ],
        extra_tools=[CallAgentTool([RESEARCHER])],
    )
    run.ports.child_reply = lambda call: ChildStartError(
        code="agent_not_published", message="발행한 버전이 없다"
    )

    outcome = await run.run()

    assert outcome.status == "succeeded"
    completed = run.ports.of("tool_call.completed")
    assert completed[0]["status"] == "error"
    assert completed[0]["output_excerpt"].startswith("agent_not_published")
    # enum에 없는 에이전트는 입력 검증에서 걸린다.
    assert completed[1]["output_excerpt"].startswith("INVALID_INPUT")
    assert "child_run.started" not in run.ports.types()


async def test_start_child_exception_becomes_a_tool_error(harness: Harness) -> None:
    def boom(call: ChildCall) -> ChildStarted:
        raise RuntimeError("db down")

    run = harness(
        [[*call_agent("call_c"), fake_end("tool_use")], [*fake_text("끝."), fake_end()]],
        extra_tools=[CallAgentTool([RESEARCHER])],
    )
    run.ports.child_reply = boom

    outcome = await run.run()

    assert outcome.status == "succeeded"
    [completed] = run.ports.of("tool_call.completed")
    assert completed["output_excerpt"].startswith("child_start_failed")
