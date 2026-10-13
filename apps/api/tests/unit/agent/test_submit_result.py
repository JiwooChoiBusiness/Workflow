"""과업 모드(설계 4.10): submit_result 검증과 루프의 독촉. 가짜 어댑터와 메모리 백엔드로 돈다."""

from collections.abc import Callable
from typing import Any

from app.agent.tools.submit_result import (
    MAX_SUBMIT_ATTEMPTS,
    SUBMIT_RESULT,
    SubmitResultTool,
    schema_validator,
)
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import (
    ERR_MODEL_REQUEST_INVALID,
    ChatRequest,
    ErrorEvent,
    ForcedTool,
    ModelEvent,
    TextBlock,
    ToolResultBlock,
)
from app.schemas.common import RunLimits

Harness = Callable[..., Any]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "total": {"type": "integer", "minimum": 0},
        "note": {"type": ["string", "null"], "maxLength": 20},
    },
    "required": ["total"],
    "additionalProperties": False,
}


def submit(data: dict[str, Any], call_id: str = "call_submit") -> list[ModelEvent]:
    return [*fake_tool_call(SUBMIT_RESULT, data, call_id=call_id), fake_end("tool_use")]


def task_harness(harness: Harness, turns: list[Any], **options: Any) -> Any:
    options.setdefault("groups", ("files",))
    return harness(turns, extra_tools=[SubmitResultTool(SCHEMA)], **options)


def results_in(request: ChatRequest) -> list[ToolResultBlock]:
    return [
        block
        for message in request.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    ]


def test_model_sees_the_output_schema_without_keys_the_compat_layer_may_reject() -> None:
    spec = SubmitResultTool(SCHEMA).spec()

    assert spec.name == "submit_result"
    assert spec.input_schema == {
        "type": "object",
        "properties": {"total": {"type": "integer", "minimum": 0}, "note": {"type": "string"}},
        "required": ["total"],
    }


def test_validator_follows_the_declared_draft() -> None:
    draft7 = schema_validator({"$schema": "http://json-schema.org/draft-07/schema#"})
    default = schema_validator({"type": "object"})

    assert type(draft7).__name__ == "Draft7Validator"
    assert type(default).__name__ == "Draft202012Validator"


async def test_valid_result_ends_the_run_with_the_output(harness: Harness) -> None:
    run = task_harness(harness, [submit({"total": 5, "note": None})])

    outcome = await run.run(submit_tool=SUBMIT_RESULT)

    assert outcome.status == "succeeded"
    assert outcome.output == {"total": 5, "note": None}
    assert outcome.files_collected
    [completed] = run.ports.of("tool_call.completed")
    assert completed["status"] == "ok"


async def test_invalid_result_is_returned_to_the_model_and_can_be_fixed(harness: Harness) -> None:
    run = task_harness(
        harness, [submit({"total": "다섯"}, "call_1"), submit({"total": 5}, "call_2")]
    )

    outcome = await run.run(submit_tool=SUBMIT_RESULT)

    assert outcome.status == "succeeded"
    assert outcome.output == {"total": 5}
    [error] = results_in(run.adapter.requests[1])
    assert error.is_error
    assert error.content.startswith("INVALID_OUTPUT: total:")
    assert f"남은 기회 {MAX_SUBMIT_ATTEMPTS - 1}번" in error.content


async def test_three_invalid_results_fail_the_run(harness: Harness) -> None:
    run = task_harness(
        harness,
        [
            submit({"total": -1}, "call_1"),
            submit({}, "call_2"),
            submit({"total": 1, "extra": True}, "call_3"),
            fake_text("여기까지 오면 안 된다"),
        ],
    )

    outcome = await run.run(submit_tool=SUBMIT_RESULT)

    assert outcome.status == "failed"
    assert outcome.error is not None and outcome.error.code == "output_invalid"
    assert "Additional properties" in outcome.error.message
    assert outcome.output is None
    assert len(run.adapter.requests) == MAX_SUBMIT_ATTEMPTS
    statuses = [item["status"] for item in run.ports.of("tool_call.completed")]
    assert statuses == ["error"] * MAX_SUBMIT_ATTEMPTS


async def test_text_only_turn_is_nudged_once_with_forced_tool_choice(harness: Harness) -> None:
    run = task_harness(harness, [fake_text("다 했습니다."), submit({"total": 3})])

    outcome = await run.run(submit_tool=SUBMIT_RESULT)

    assert outcome.status == "succeeded" and outcome.output == {"total": 3}
    first, second = run.adapter.requests
    assert first.params.tool_choice is None
    assert second.params.tool_choice == ForcedTool(name=SUBMIT_RESULT)
    assistant, nudge = second.messages[-2], second.messages[-1]
    assert assistant.role == "assistant"
    assert nudge.role == "user"
    assert any(
        isinstance(block, TextBlock) and "`submit_result`" in block.text for block in nudge.blocks
    )
    # 독촉한 턴의 글도 메시지로 남는다.
    stops = [item["stop_reason"] for item in run.ports.of("message.completed")]
    assert stops == ["end_turn", "tool_use"]


async def test_no_result_after_the_nudge_fails_with_output_missing(harness: Harness) -> None:
    run = task_harness(harness, [fake_text("끝났다"), fake_text("정말 끝났다")])

    outcome = await run.run(submit_tool=SUBMIT_RESULT)

    assert outcome.status == "failed"
    assert outcome.error is not None and outcome.error.code == "output_missing"
    assert len(run.adapter.requests) == 2
    assert run.adapter.requests[1].params.tool_choice == ForcedTool(name=SUBMIT_RESULT)


async def test_rejected_forced_choice_falls_back_to_a_plain_nudge(harness: Harness) -> None:
    def reject_forced(request: ChatRequest) -> list[ModelEvent]:
        assert request.params.tool_choice == ForcedTool(name=SUBMIT_RESULT)
        return [
            ErrorEvent(
                code=ERR_MODEL_REQUEST_INVALID,
                message="tool_choice를 받지 않는다",
                retryable=False,
                status=400,
            )
        ]

    run = task_harness(harness, [fake_text("끝"), reject_forced, submit({"total": 1})])

    outcome = await run.run(submit_tool=SUBMIT_RESULT)

    assert outcome.status == "succeeded"
    assert run.adapter.requests[2].params.tool_choice is None
    [retry] = run.ports.of("run.retrying")
    assert retry["reason"] == "tool_choice_rejected"


async def test_step_limit_without_a_result_fails(harness: Harness) -> None:
    run = task_harness(
        harness,
        [[*fake_tool_call("list_files", {}), fake_end("tool_use")]],
        limits=RunLimits(max_steps=1),
    )

    outcome = await run.run(submit_tool=SUBMIT_RESULT)

    assert outcome.status == "failed"
    assert outcome.error is not None and outcome.error.code == "output_missing"
    assert outcome.limit == "steps"


async def test_chat_runs_are_not_nudged(harness: Harness) -> None:
    run = task_harness(harness, [fake_text("그냥 답")])

    outcome = await run.run()

    assert outcome.status == "succeeded"
    assert len(run.adapter.requests) == 1
