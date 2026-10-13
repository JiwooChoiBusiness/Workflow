"""AgentLoop 시나리오(설계 10장 M6). 가짜 어댑터와 메모리 백엔드로 루프 전체를 돈다."""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from app.agent.interactions import ResumePayload
from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.llm.fake_adapter import fake_end, fake_text, fake_tool_call
from app.llm.types import (
    ERR_MODEL_STREAM_INTERRUPTED,
    ChatRequest,
    ErrorEvent,
    ModelEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from app.schemas.common import RunLimits
from app.schemas.events import MessageFileData

Harness = Callable[..., Any]


def results_of(request: ChatRequest) -> list[ToolResultBlock]:
    return [
        block
        for message in request.messages
        for block in message.blocks
        if isinstance(block, ToolResultBlock)
    ]


def last_results(request: ChatRequest) -> list[ToolResultBlock]:
    return [block for block in request.messages[-1].blocks if isinstance(block, ToolResultBlock)]


def shell(command: str, call_id: str, **extra: Any) -> list[ModelEvent]:
    return fake_tool_call("shell", {"command": command, **extra}, call_id=call_id)


async def test_text_only_answer(harness: Harness) -> None:
    # message_end를 빼면 가짜 어댑터가 휴리스틱 usage를 채운다.
    run = harness([fake_text("안녕하세요. 무엇을 도와드릴까요?")])
    run.ports.files = [MessageFileData(path="outputs/a.txt", size=3, downloadable=True)]

    outcome = await run.run()

    assert outcome.status == "succeeded"
    assert outcome.steps == 1
    assert run.ports.types()[0] == "step.started"
    assert "message.delta" in run.ports.types()
    completed = run.ports.of("message.completed")
    assert completed[-1]["stop_reason"] == "end_turn"
    assert completed[-1]["text"] == "안녕하세요. 무엇을 도와드릴까요?"
    # 마지막 메시지에 이 런이 바꾼 파일이 붙는다.
    assert completed[-1]["files"][0]["path"] == "outputs/a.txt"
    assert outcome.files_collected
    [message] = run.ports.messages.values()
    assert message.stop_reason == "end_turn"
    assert run.ports.of("usage")[0]["step"] == 1
    assert outcome.usage.input_tokens > 0


async def test_one_tool_call_then_answer(harness: Harness) -> None:
    run = harness(
        [
            [*fake_text("확인할게요."), *shell("Get-ChildItem", "call_1"), fake_end("tool_use")],
            [*fake_text("파일이 없습니다."), fake_end()],
        ]
    )
    run.backend.outputs["Get-ChildItem"] = ("한글 출력\n", 0)

    outcome = await run.run()

    assert outcome.status == "succeeded"
    assert outcome.steps == 2
    types = run.ports.types()
    for expected in (
        "tool_call.started",
        "tool_call.input_delta",
        "tool_call.ready",
        "tool_call.output_delta",
        "tool_call.completed",
    ):
        assert expected in types
    assert types.index("tool_call.ready") < types.index("tool_call.completed")
    [completed] = run.ports.of("tool_call.completed")
    assert completed["status"] == "ok"
    assert completed["exit_code"] == 0
    assert completed["output_excerpt"] == "한글 출력\n"
    [result] = last_results(run.adapter.requests[1])
    assert result.tool_use_id == "call_1"
    assert "[종료 코드 0]" in result.content and "한글 출력" in result.content
    assert not result.is_error
    # 스텝 메시지에 도구 결과가 붙어 저장된다.
    first = next(iter(run.ports.messages.values()))
    assert [block.type for block in first.blocks] == ["text", "tool_use", "tool_result"]


async def test_parallel_calls_run_one_by_one_in_order(harness: Harness) -> None:
    run = harness(
        [
            [
                *fake_tool_call(
                    "write_file", {"path": "a.txt", "content": "A"}, index=0, call_id="call_w"
                ),
                *fake_tool_call("read_file", {"path": "a.txt"}, index=1, call_id="call_r"),
                fake_end("tool_use"),
            ],
            [*fake_text("다 했다."), fake_end()],
        ]
    )

    outcome = await run.run()

    assert outcome.status == "succeeded"
    results = last_results(run.adapter.requests[1])
    assert [result.tool_use_id for result in results] == ["call_w", "call_r"]
    # 순서대로 실행했으므로 읽기가 방금 쓴 내용을 본다.
    assert results[1].content == "A"
    completed = run.ports.of("tool_call.completed")
    assert [item["tool_call_id"] for item in completed] == ["call_w", "call_r"]


async def test_approval_in_the_middle_of_a_batch_pauses_and_resumes(harness: Harness) -> None:
    run = harness(
        [
            [
                *fake_tool_call(
                    "write_file", {"path": "a.txt", "content": "A"}, index=0, call_id="call_w"
                ),
                *shell("python job.py", "call_s", timeout_sec=30),
                *fake_tool_call("read_file", {"path": "a.txt"}, index=2, call_id="call_r"),
                fake_end("tool_use"),
            ],
            [*fake_text("끝."), fake_end()],
        ],
        approval_mode="all_shell",
    )

    paused = await run.run()

    assert paused.status == "waiting_input"
    pending = paused.pending
    assert pending is not None
    assert (pending.kind, pending.tool_call_id, pending.tool_name) == (
        "approval",
        "call_s",
        "shell",
    )
    assert pending.completed_tool_call_ids == ["call_w"]
    assert pending.input == {"command": "python job.py", "timeout_sec": 30}
    assert (pending.cwd, pending.timeout_sec) == (".", 30)
    [waiting] = run.ports.of("run.waiting_input")
    assert waiting["kind"] == "approval" and waiting["tool_name"] == "shell"
    # 승인 전에는 셸이 돌지 않았다. 앞의 write_file만 실행됐다.
    assert run.backend.execs == []
    assert run.backend.files == {"a.txt": b"A"}
    assert len(run.adapter.requests) == 1

    done = await run.resume(
        paused,
        ResumePayload(interaction_id=pending.interaction_id, kind="approval", decision="approve"),
    )

    assert done.status == "succeeded"
    assert [spec.script for spec in run.backend.execs] == ["python job.py"]
    results = last_results(run.adapter.requests[1])
    assert [result.tool_use_id for result in results] == ["call_w", "call_s", "call_r"]
    assert done.steps == 2


async def test_tool_error_is_returned_and_model_tries_again(harness: Harness) -> None:
    run = harness(
        [
            [*shell("Get-Item missing.txt", "call_1"), fake_end("tool_use")],
            [*shell("Get-ChildItem", "call_2"), fake_end("tool_use")],
            [*fake_text("목록을 확인했다."), fake_end()],
        ]
    )
    run.backend.outputs["Get-Item missing.txt"] = ("찾을 수 없다\n", 1)

    outcome = await run.run()

    assert outcome.status == "succeeded"
    first, second = run.ports.of("tool_call.completed")
    assert (first["status"], first["exit_code"]) == ("error", 1)
    assert second["status"] == "ok"
    [result] = last_results(run.adapter.requests[1])
    assert result.is_error and "[종료 코드 1]" in result.content


async def test_step_limit_appends_notice_and_succeeds(harness: Harness) -> None:
    def always_tool(request: ChatRequest) -> list[ModelEvent]:
        index = len(results_of(request))
        return [*shell("echo hi", f"call_{index}"), fake_end("tool_use")]

    run = harness([always_tool] * 5, limits=RunLimits(max_steps=2))

    outcome = await run.run()

    assert outcome.status == "succeeded"
    assert outcome.limit == "steps"
    assert outcome.steps == 2
    assert len(run.adapter.requests) == 2
    final = run.ports.of("message.completed")[-1]
    assert final["stop_reason"] == "max_steps"
    assert "스텝 한도(2회)" in final["text"]


async def test_cancel_kills_the_running_tool(harness: Harness) -> None:
    run = harness([[*shell("Start-Sleep 30", "call_long"), fake_end("tool_use")]])
    run.backend.hang.add("Start-Sleep 30")

    async def stop_when_started() -> None:
        await run.backend.started.wait()
        run.cancel.set("user")

    stopper = asyncio.create_task(stop_when_started())
    started = time.monotonic()
    outcome = await run.run()
    await stopper

    assert time.monotonic() - started < 3
    assert outcome.status == "cancelled"
    assert outcome.interrupted == "cancel"
    assert run.backend.killed == ["call_long"]
    [completed] = run.ports.of("tool_call.completed")
    assert completed["status"] == "cancelled"
    # 결과는 저장돼 다음 런의 히스토리가 도구 호출 규칙을 지킨다.
    history = run.history()
    assert any(
        isinstance(block, ToolResultBlock) and block.tool_use_id == "call_long"
        for message in history
        for block in message.blocks
    )


async def test_cancel_while_streaming_keeps_partial_text(harness: Harness) -> None:
    long_text = "가" * 200
    run = harness([[*fake_text(long_text, chunk_size=10), fake_end()]], delay_sec=0.02)

    async def stop_after_some_text() -> None:
        while run.ports.of("message.delta") == []:
            await asyncio.sleep(0.01)
        run.cancel.set("user")

    stopper = asyncio.create_task(stop_after_some_text())
    outcome = await run.run()
    await stopper

    assert outcome.status == "cancelled"
    [message] = run.ports.messages.values()
    assert message.stop_reason == "cancelled"
    assert 0 < len(message.text) < len(long_text)
    assert run.ports.of("message.completed")[-1]["stop_reason"] == "cancelled"


async def test_shutdown_fails_the_run_with_code_shutdown(harness: Harness) -> None:
    run = harness([[*shell("Start-Sleep 30", "call_long"), fake_end("tool_use")]])
    run.backend.hang.add("Start-Sleep 30")

    async def shutdown_when_started() -> None:
        await run.backend.started.wait()
        run.cancel.set("shutdown")

    stopper = asyncio.create_task(shutdown_when_started())
    outcome = await run.run()
    await stopper

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert (outcome.error.code, outcome.error.retryable) == ("shutdown", True)
    assert outcome.interrupted == "shutdown"
    assert run.backend.killed == ["call_long"]


async def test_invalid_tool_json_becomes_error_result(harness: Harness) -> None:
    broken: list[ModelEvent] = [
        ToolCallStart(index=0, id="call_bad", name="shell"),
        ToolCallDelta(index=0, json_fragment='{"command": "dir"'),
        ToolCallEnd(index=0, id="call_bad", name="shell", arguments='{"command": "dir"'),
        fake_end("tool_use"),
    ]
    run = harness(
        [
            broken,
            [*shell("dir", "call_ok"), fake_end("tool_use")],
            [*fake_text("됐다."), fake_end()],
        ]
    )

    outcome = await run.run()

    assert outcome.status == "succeeded"
    [result] = last_results(run.adapter.requests[1])
    assert result.is_error and result.content.startswith("INVALID_JSON")
    # 실행하지 않았고 tool_call.ready도 내지 않았다.
    assert [spec.script for spec in run.backend.execs] == ["dir"]
    assert "call_bad" not in [item["tool_call_id"] for item in run.ports.of("tool_call.ready")]
    assert run.ports.of("tool_call.completed")[0]["status"] == "error"


async def test_schema_mismatch_three_times_fails_the_run(harness: Harness) -> None:
    def bad_input(request: ChatRequest) -> list[ModelEvent]:
        index = len(results_of(request))
        return [
            *fake_tool_call("shell", {"cmd": "dir"}, call_id=f"call_{index}"),
            fake_end("tool_use"),
        ]

    run = harness([bad_input] * 5)

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.error is not None and outcome.error.code == "tool_input_invalid"
    assert len(run.adapter.requests) == 3
    assert run.backend.execs == []
    first = last_results(run.adapter.requests[1])[0]
    assert first.content.startswith("INVALID_INPUT")


async def test_ask_user_pauses_and_answer_resumes(harness: Harness) -> None:
    run = harness(
        [
            [
                *fake_tool_call(
                    "ask_user",
                    {"question": "이름이 무엇인가요?", "options": ["철수", "영희"]},
                    call_id="call_q",
                ),
                fake_end("tool_use"),
            ],
            lambda request: [*fake_text(f"안녕하세요, {last_results(request)[0].content}님!")],
        ]
    )

    paused = await run.run()

    assert paused.status == "waiting_input"
    pending = paused.pending
    assert pending is not None
    assert (pending.kind, pending.question, pending.options) == (
        "question",
        "이름이 무엇인가요?",
        ["철수", "영희"],
    )
    assert pending.allow_free_text is True
    assert run.ports.of("run.waiting_input")[0]["question"] == "이름이 무엇인가요?"

    done = await run.resume(
        paused, ResumePayload(interaction_id=pending.interaction_id, kind="question", answer="지우")
    )

    assert done.status == "succeeded"
    assert run.ports.of("message.completed")[-1]["text"] == "안녕하세요, 지우님!"
    answered = run.ports.of("tool_call.completed")[-1]
    assert (answered["tool_call_id"], answered["status"]) == ("call_q", "ok")


async def test_rejected_approval_tells_the_model(harness: Harness) -> None:
    run = harness(
        [
            [*shell("Remove-Item -Recurse data", "call_rm"), fake_end("tool_use")],
            [*fake_text("알겠습니다. 지우지 않겠습니다."), fake_end()],
        ],
        approval_mode="all_shell",
    )
    paused = await run.run()
    assert paused.pending is not None

    done = await run.resume(
        paused,
        ResumePayload(
            interaction_id=paused.pending.interaction_id,
            kind="approval",
            decision="reject",
            note="데이터를 지우면 안 된다",
        ),
    )

    assert done.status == "succeeded"
    assert run.backend.execs == []
    [result] = last_results(run.adapter.requests[1])
    assert result.content == "사용자가 이 작업을 거절했습니다: 데이터를 지우면 안 된다"
    assert not result.is_error
    assert run.ports.of("tool_call.completed")[-1]["status"] == "rejected"


async def test_approve_with_session_auto_approve_skips_later_approvals(harness: Harness) -> None:
    run = harness(
        [
            [*shell("pip list", "call_1"), fake_end("tool_use")],
            [*shell("python main.py", "call_2"), fake_end("tool_use")],
            [*fake_text("완료"), fake_end()],
        ],
        approval_mode="all_shell",
    )
    paused = await run.run()
    assert paused.pending is not None

    done = await run.resume(
        paused,
        ResumePayload(
            interaction_id=paused.pending.interaction_id,
            kind="approval",
            decision="approve",
            session_auto_approve=True,
        ),
    )

    assert done.status == "succeeded"
    assert [spec.script for spec in run.backend.execs] == ["pip list", "python main.py"]
    assert len(run.ports.of("run.waiting_input")) == 1


async def test_http_write_needs_approval_but_get_does_not(harness: Harness) -> None:
    run = harness(
        [
            [
                *fake_tool_call(
                    "http_request",
                    {
                        "method": "GET",
                        "url": "https://pypi.org/simple/",
                        "headers": {"Authorization": "Bearer secret-token"},
                    },
                    call_id="call_get",
                ),
                *fake_tool_call(
                    "http_request",
                    {"method": "POST", "url": "https://pypi.org/x", "body": "{}"},
                    index=1,
                    call_id="call_post",
                ),
                fake_end("tool_use"),
            ],
        ],
        approval_mode="http_writes",
    )

    paused = await run.run()

    assert paused.status == "waiting_input"
    assert paused.pending is not None and paused.pending.tool_call_id == "call_post"
    assert [spec.url for spec in run.backend.http] == ["https://pypi.org/simple/"]
    ready = run.ports.of("tool_call.ready")[0]
    assert ready["input"]["headers"] == {"Authorization": "***"}
    completed = run.ports.of("tool_call.completed")[0]
    assert "token=secret" not in completed["output_excerpt"]


async def test_workspace_folder_lost_is_recreated(harness: Harness) -> None:
    run = harness(
        [
            [*shell("dir", "call_1"), fake_end("tool_use")],
            [*shell("dir", "call_2"), fake_end("tool_use")],
            [*fake_text("다시 만들어진 작업공간에서 실행했다."), fake_end()],
        ]
    )
    run.backend.missing = True

    outcome = await run.run()

    assert outcome.status == "succeeded"
    assert run.ports.recreated == 1
    assert "workspace.recreated" in run.ports.types()
    first, second = run.ports.of("tool_call.completed")
    assert first["status"] == "error" and "workspace_missing" in first["output_excerpt"]
    assert second["status"] == "ok"


async def test_sandbox_unavailable_twice_fails_the_run(harness: Harness) -> None:
    run = harness(
        [
            [*shell("dir", "call_1"), fake_end("tool_use")],
            [*shell("dir", "call_2"), fake_end("tool_use")],
            [*fake_text("x"), fake_end()],
        ]
    )
    run.backend.unavailable = True

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.error is not None and outcome.error.code == "sandbox_unavailable"


async def test_truncated_tool_call_is_retried_with_double_output_limit(harness: Harness) -> None:
    truncated: list[ModelEvent] = [
        ToolCallStart(index=0, id="call_cut", name="write_file"),
        ToolCallDelta(index=0, json_fragment='{"path": "a.txt", "content": "AAA'),
        ToolCallEnd(index=0, id="call_cut", name="write_file", arguments='{"path": "a.txt"'),
        fake_end("max_tokens"),
    ]
    run = harness(
        [
            truncated,
            [
                *fake_tool_call("write_file", {"path": "a.txt", "content": "AAA"}, call_id="c2"),
                fake_end("tool_use"),
            ],
            [*fake_text("썼다."), fake_end()],
        ]
    )

    outcome = await run.run()

    assert outcome.status == "succeeded"
    retrying = run.ports.of("run.retrying")
    assert retrying == [{"step": 1, "reason": "max_tokens", "attempt": 1}]
    assert [request.params.max_output_tokens for request in run.adapter.requests[:2]] == [
        1000,
        2000,
    ]
    # 잘린 호출은 실행하지 않았다.
    assert run.backend.files == {"a.txt": b"AAA"}


async def test_empty_max_tokens_answer_is_not_retried_by_the_loop(harness: Harness) -> None:
    # 사고 토큰이 상한을 다 써 버린 빈 응답의 재시도는 어댑터가 한다(12.2 M4). 루프는 다시 묻지
    # 않는다.
    run = harness([[fake_end("max_tokens")]])

    outcome = await run.run()

    assert outcome.status == "succeeded"
    assert len(run.adapter.requests) == 1
    assert run.ports.of("message.completed")[-1]["stop_reason"] == "max_tokens"


async def test_refusal_ends_the_run(harness: Harness) -> None:
    run = harness([[*fake_text("도와드릴 수 없습니다."), fake_end("refusal")]])

    outcome = await run.run()

    assert outcome.status == "succeeded"
    [message] = run.ports.messages.values()
    assert message.stop_reason == "refusal"


async def test_interrupted_stream_is_retried(harness: Harness) -> None:
    interrupted: list[ModelEvent] = [
        TextDelta(text="부분"),
        ErrorEvent(code=ERR_MODEL_STREAM_INTERRUPTED, message="끊김", retryable=True),
    ]
    run = harness([interrupted, [*fake_text("완성된 답"), fake_end()]])

    outcome = await run.run()

    assert outcome.status == "succeeded"
    assert run.ports.of("run.retrying") == [
        {"step": 1, "reason": ERR_MODEL_STREAM_INTERRUPTED, "attempt": 1}
    ]
    [message] = run.ports.messages.values()
    assert message.text == "완성된 답"
    deltas = run.ports.of("message.delta")
    assert deltas[0]["message_id"] != deltas[-1]["message_id"]


async def test_model_error_fails_the_run(harness: Harness) -> None:
    run = harness([[ErrorEvent(code="model_unavailable", message="모든 키가 429", retryable=True)]])

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.error is not None
    assert (outcome.error.code, outcome.error.retryable) == ("model_unavailable", True)


async def test_token_budget_stops_the_run(harness: Harness) -> None:
    big = Usage(input_tokens=900, output_tokens=200)
    run = harness(
        [[*shell("dir", "call_1"), fake_end("tool_use", usage=big)]],
        limits=RunLimits(max_total_tokens=1000),
    )

    outcome = await run.run()

    assert outcome.status == "failed"
    assert outcome.error is not None and outcome.error.code == "token_budget"
    assert run.backend.execs == []


async def test_run_time_limit_fails_with_timeout(harness: Harness) -> None:
    run = harness([[*shell("Start-Sleep 30", "call_long"), fake_end("tool_use")]])
    run.backend.hang.add("Start-Sleep 30")

    # 이미 1799.8초를 쓴 런을 재개한 것처럼 둔다. 남은 0.2초 안에 도구가 끝나지 않는다.
    outcome = await run.run(elapsed_ms=1_799_800)

    assert outcome.status == "failed"
    assert outcome.error is not None and outcome.error.code == "timeout"
    assert run.backend.killed == ["call_long"]


async def test_large_output_is_cut_for_the_model_and_spilled(harness: Harness) -> None:
    run = harness(
        [
            [*shell("big", "call_big"), fake_end("tool_use")],
            [*fake_text("확인"), fake_end()],
        ]
    )
    line = "0123456789" * 10 + "\n"
    run.backend.outputs["big"] = (line * 3000, 0)

    await run.run()

    [completed] = run.ports.of("tool_call.completed")
    assert completed["output_bytes"] == len(line) * 3000
    assert completed["output_storage_key"] == "tool-outputs/run/call_big.txt"
    assert run.ports.sinks["tool-outputs/run/call_big.txt"] == (line * 3000).encode()
    [result] = last_results(run.adapter.requests[1])
    assert "bytes omitted" in result.content
    assert len(result.content.encode()) < 34 * 1024


async def test_terminal_tool_ends_the_run_with_output(harness: Harness) -> None:
    class SubmitTool(Tool):
        name = "submit_result"
        group = "ask_user"
        terminal = True
        description = "결과 제출"
        input_schema = {"type": "object", "properties": {"answer": {"type": "integer"}}}

        async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
            result = ToolResult.ok("제출했다")
            result.output = data
            return result

    run = harness(
        [[*fake_tool_call("submit_result", {"answer": 42}), fake_end("tool_use")]],
        groups=(),
        extra_tools=[SubmitTool()],
    )

    outcome = await run.run()

    assert outcome.status == "succeeded"
    assert outcome.output == {"answer": 42}
    assert run.ports.collect_calls == 1


async def test_tool_use_blocks_keep_provider_extra(harness: Harness) -> None:
    signature = {"extra_content": {"google": {"thought_signature": "sig"}}}
    run = harness(
        [
            [*fake_tool_call("list_files", {}, provider_extra=signature), fake_end("tool_use")],
            [*fake_text("목록"), fake_end()],
        ]
    )

    await run.run()

    calls = [
        block
        for message in run.adapter.requests[1].messages
        for block in message.blocks
        if isinstance(block, ToolUseBlock)
    ]
    assert calls[0].provider_extra == signature
    stored = run.ports.rows[0].blocks
    assert json.dumps(signature) in json.dumps(stored)
