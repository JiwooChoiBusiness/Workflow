"""에이전트 루프(설계 4.1~4.9).

HTTP와 DB를 모른다. 이벤트·메시지 저장·도구 출력 저장·파일 수집은 LoopPorts로 받고, 런 행 상태와
작업공간 확보는 부르는 쪽(app/services/run_executor.py)이 맡는다. 2단계 워크플로우 엔진도 같은
클래스를 프로세스 안에서 부른다(13장).

한 스텝은 모델 스트림 1회와 그 도구 호출들이다. 도구는 순서대로 하나씩 실행하고 결과를 같은 스텝
메시지에 붙인다. 승인이 필요하거나 ask_user가 오면 PendingInteraction을 남기고 waiting_input으로
끝낸다. 재개는 같은 스텝 메시지의 남은 호출부터 이어 간다.
"""

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

import structlog
import uuid6
from pydantic import BaseModel, Field

from app.agent.context import (
    ContextTracker,
    StepMessage,
    context_budget,
    summary_request_text,
    summary_user_text,
)
from app.agent.interactions import (
    PendingInteraction,
    ResumePayload,
    rejection_text,
    requires_approval,
)
from app.agent.prompt_builder import load_prompt
from app.agent.tools.base import (
    OutputCapture,
    OutputSink,
    OutputStream,
    Tool,
    ToolContext,
    ToolDefaults,
    ToolResult,
    parse_arguments,
    utf8_len,
    validate_tool_input,
)
from app.llm.base import ModelAdapter
from app.llm.pricing import cost_usd
from app.llm.types import (
    ERR_MODEL_REQUEST_INVALID,
    ERR_MODEL_STREAM_INTERRUPTED,
    Block,
    CanonicalMessage,
    ChatRequest,
    ErrorEvent,
    ForcedTool,
    ImageBlock,
    MessageEnd,
    ModelParams,
    TextBlock,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultBlock,
    ToolUseBlock,
)
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.schemas.common import ApprovalMode, RunLimits
from app.schemas.events import MessageFileData, UsageTotal

log = structlog.get_logger("app.agent")

# 스트림이 중간에 끊기면 그 스텝을 이만큼 다시 실행한다(4.6).
MAX_STREAM_RETRIES = 2
MAX_INVALID_INPUT_STREAK = 3
MAX_SANDBOX_FAILURES = 2
# 중지 요청 뒤 도구가 정리될 때까지 기다리는 시간. Job 종료는 즉시라 보통 훨씬 빨리 끝난다.
KILL_GRACE_SEC = 5.0
SUMMARY_MAX_OUTPUT_TOKENS = 8000
STEP_LIMIT_TEXT = "스텝 한도({limit}회)에 도달해 작업을 멈췄다. 이어서 하려면 메시지를 다시 보낸다."
SUBMIT_NUDGE_TEXT = (
    "결과를 아직 제출하지 않았다. 지금 `{tool}`을(를) 호출해 출력 스키마에 맞는 결과를 제출한다."
)

InterruptKind = Literal["cancel", "shutdown", "timeout"]
OutcomeStatus = Literal["succeeded", "failed", "cancelled", "waiting_input"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> uuid.UUID:
    return uuid6.uuid7()


class CancelToken(Protocol):
    """런 취소 신호(app/runner/supervisor.py의 CancelSignal). reason이 shutdown이면 서버 종료다."""

    reason: str | None

    def is_set(self) -> bool: ...

    async def wait(self) -> None: ...


class FileSummary(BaseModel):
    files: list[MessageFileData] = Field(default_factory=list[MessageFileData])
    truncated: bool | None = None


class LoopPorts(Protocol):
    async def emit(self, event_type: str, data: Mapping[str, Any]) -> None: ...

    # 스텝 메시지를 넣거나(처음) 고친다(도구 결과가 붙을 때마다).
    async def save_message(self, message: StepMessage) -> None: ...

    async def save_summary(self, text: str, usage: Mapping[str, Any] | None) -> uuid.UUID: ...

    async def save_progress(self, steps: int, usage: UsageTotal) -> None: ...

    async def open_output_sink(self, tool_call_id: str) -> OutputSink: ...

    # 도구가 workspace_missing을 받으면 부른다. 빈 폴더로 다시 만들고 workspace.recreated를 낸다.
    async def recreate_workspace(self) -> WorkspaceHandle: ...

    # 런이 바꾼 파일을 storage로 옮기고 file.changed를 낸다.
    async def collect_files(self) -> FileSummary: ...

    # 첨부 이미지의 base64. 읽지 못하면 None(어댑터가 "[이미지 첨부]" 글로 바꾼다).
    async def load_image(self, storage_key: str) -> str | None: ...


@dataclass(frozen=True)
class ResumeState:
    pending: PendingInteraction
    payload: ResumePayload


@dataclass
class LoopConfig:
    run_id: uuid.UUID
    session_id: uuid.UUID
    system_prompt: str
    tools: Sequence[Tool]
    params: ModelParams
    limits: RunLimits
    # 세션 자동 승인까지 반영한 값.
    approval_mode: ApprovalMode
    model_id: str | None = None
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    tool_defaults: ToolDefaults = field(default_factory=ToolDefaults)
    memory_limit_mb: int | None = None
    max_processes: int | None = None
    context_window: int = 1_000_000
    max_output_cap: int = 65_536
    model_prices: Mapping[str, Mapping[str, Any]] | None = None
    # 재개할 때 이어 쓰는 값(run.steps, run.usage, pending.elapsed_ms).
    steps_done: int = 0
    usage: UsageTotal = field(default_factory=UsageTotal)
    elapsed_ms: int = 0
    resume: ResumeState | None = None
    # 과업 모드(4.10)에서 결과를 내는 종료형 도구(submit_result). 이 도구 없이 턴을 끝내면 그 도구를
    # 강제한 스텝으로 한 번 독촉하고, 그래도 없으면 output_missing으로 실패한다.
    submit_tool: str | None = None


class LoopError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class LoopOutcome(BaseModel):
    status: OutcomeStatus
    steps: int
    usage: UsageTotal
    elapsed_ms: int
    error: LoopError | None = None
    pending: PendingInteraction | None = None
    final_message_id: uuid.UUID | None = None
    limit: Literal["steps"] | None = None
    # True면 루프가 이미 파일을 모았다(마지막 메시지의 files에 들어 있다).
    files_collected: bool = False
    changes_truncated: bool | None = None
    models_used: list[str] = Field(default_factory=list[str])
    interrupted: InterruptKind | None = None
    output: Any = None


class _Interrupted(Exception):
    def __init__(self, kind: InterruptKind) -> None:
        super().__init__(kind)
        self.kind: InterruptKind = kind


@dataclass
class _ModelReply:
    message_id: uuid.UUID
    sent_messages: int
    text_parts: list[str] = field(default_factory=list[str])
    calls: list[ToolUseBlock] = field(default_factory=list[ToolUseBlock])
    parse_errors: dict[str, str] = field(default_factory=dict[str, str])
    call_ids: dict[int, str] = field(default_factory=dict[int, str])
    end: MessageEnd | None = None
    error: ErrorEvent | None = None

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


class AgentLoop:
    def __init__(
        self,
        *,
        config: LoopConfig,
        model: ModelAdapter,
        backend: SandboxBackend,
        handle: WorkspaceHandle,
        ports: LoopPorts,
        cancel: CancelToken,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        self._model = model
        self._backend = backend
        self._handle = handle
        self._ports = ports
        self._cancel = cancel
        self._clock = clock
        self._now = now
        self._tools = {tool.name: tool for tool in config.tools}
        self._specs = [tool.spec() for tool in config.tools]
        self._messages: list[CanonicalMessage] = []
        self._steps = config.steps_done
        self._usage = config.usage.model_copy()
        self._started = clock()
        self._invalid_streak = 0
        self._sandbox_failures = 0
        self._models: list[str] = []
        self._images: dict[str, str | None] = {}
        self._tracker = ContextTracker(model.estimate_tokens)
        self._nudged = False
        self._forced_tool: str | None = None

    async def run(
        self, history: Sequence[CanonicalMessage], open_step: StepMessage | None = None
    ) -> LoopOutcome:
        """history는 이번에 보낼 히스토리, open_step은 재개할 때 멈춘 스텝 메시지다."""
        self._messages = list(history)
        self._started = self._clock()
        try:
            resume = self._config.resume
            if resume is not None:
                if open_step is None or open_step.id != resume.pending.message_id:
                    return self._failed("resume_state_invalid", "재개할 스텝 메시지를 찾지 못했다")
                outcome = await self._run_batch(open_step, {}, resume)
                if outcome is not None:
                    return outcome
            while True:
                self._check_interrupt()
                if self._steps >= self._config.limits.max_steps:
                    return await self._step_limit()
                outcome = await self._step()
                if outcome is not None:
                    return outcome
        except _Interrupted as stop:
            return self._interrupted(stop.kind)

    @property
    def handle(self) -> WorkspaceHandle:
        return self._handle

    async def _emit(self, event_type: str, data: Mapping[str, Any]) -> None:
        await self._ports.emit(event_type, data)

    def _elapsed_ms(self) -> int:
        return self._config.elapsed_ms + int((self._clock() - self._started) * 1000)

    def _remaining_sec(self) -> float:
        return self._config.limits.max_run_seconds - self._elapsed_ms() / 1000

    def _cancel_kind(self) -> InterruptKind:
        return "shutdown" if self._cancel.reason == "shutdown" else "cancel"

    def _check_interrupt(self) -> None:
        if self._cancel.is_set():
            raise _Interrupted(self._cancel_kind())
        if self._remaining_sec() <= 0:
            raise _Interrupted("timeout")

    async def _wait(self, task: "asyncio.Task[Any]") -> InterruptKind | None:
        """작업이 끝나면 None, 그 전에 중지·종료·시간 초과가 오면 그 종류."""
        if self._cancel.is_set():
            return self._cancel_kind()
        remaining = self._remaining_sec()
        if remaining <= 0:
            return "timeout"
        waiter = asyncio.create_task(self._cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {task, waiter}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            waiter.cancel()
        if task in done:
            return None
        if self._cancel.is_set():
            return self._cancel_kind()
        return "timeout"

    def _outcome(self, status: OutcomeStatus, **fields: Any) -> LoopOutcome:
        return LoopOutcome(
            status=status,
            steps=self._steps,
            usage=self._usage.model_copy(),
            elapsed_ms=self._elapsed_ms(),
            models_used=list(self._models),
            **fields,
        )

    def _failed(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        interrupted: InterruptKind | None = None,
    ) -> LoopOutcome:
        log.warning("agent.run_failed", run_id=str(self._config.run_id), code=code)
        return self._outcome(
            "failed",
            error=LoopError(code=code, message=message, retryable=retryable),
            interrupted=interrupted,
        )

    def _interrupted(self, kind: InterruptKind) -> LoopOutcome:
        if kind == "cancel":
            return self._outcome("cancelled", interrupted=kind)
        if kind == "shutdown":
            return self._failed(
                "shutdown", "서버가 종료되어 런을 멈췄다", retryable=True, interrupted=kind
            )
        limit = self._config.limits.max_run_seconds
        return self._failed("timeout", f"런 실행 시간 한도({limit}초)를 넘었다", interrupted=kind)

    async def _step(self) -> LoopOutcome | None:
        self._steps += 1
        step = self._steps
        await self._emit("step.started", {"step": step})
        await self._maybe_compact(step)
        max_output = self._config.params.max_output_tokens
        # 독촉 스텝에만 결과 도구를 강제한다.
        forced, self._forced_tool = self._forced_tool, None
        stream_retries = 0
        truncation_retried = False
        while True:
            reply = await self._call_model(step, max_output, forced)
            if reply.error is not None or reply.end is None:
                error = reply.error or ErrorEvent(
                    code=ERR_MODEL_STREAM_INTERRUPTED,
                    message="모델 응답이 끝 표시 없이 끊겼다",
                    retryable=True,
                )
                if forced is not None and error.code == ERR_MODEL_REQUEST_INVALID:
                    # 강제 tool_choice를 받지 않으면 강제 없이 독촉한다(4.10).
                    log.warning(
                        "agent.forced_tool_rejected", run_id=str(self._config.run_id), tool=forced
                    )
                    forced = None
                    await self._emit(
                        "run.retrying",
                        {"step": step, "reason": "tool_choice_rejected", "attempt": 1},
                    )
                    continue
                if (
                    error.code == ERR_MODEL_STREAM_INTERRUPTED
                    and stream_retries < MAX_STREAM_RETRIES
                ):
                    # 부분 출력은 버린다. 화면은 run.retrying을 받으면 진행 중이던 메시지를 지운다.
                    stream_retries += 1
                    await self._emit(
                        "run.retrying",
                        {"step": step, "reason": error.code, "attempt": stream_retries},
                    )
                    continue
                return self._failed(error.code, error.message, retryable=error.retryable)
            end = reply.end
            usage = await self._record_usage(step, end, sent_messages=reply.sent_messages)
            if end.stop_reason == "max_tokens" and reply.calls:
                # 출력 상한에 잘린 도구 호출은 실행하지 않는다(4.3). 빈 응답 재시도는 어댑터가 한다.
                cap = self._config.max_output_cap
                if not truncation_retried and max_output < cap:
                    truncation_retried = True
                    max_output = min(max_output * 2, cap)
                    await self._emit(
                        "run.retrying", {"step": step, "reason": "max_tokens", "attempt": 1}
                    )
                    continue
                log.warning("agent.tool_call_truncated", run_id=str(self._config.run_id), step=step)
                return await self._end_turn(self._text_message(reply, end, usage), "max_tokens")
            if reply.calls:
                message = StepMessage(
                    id=reply.message_id,
                    blocks=[*self._text_blocks(reply), *reply.calls],
                    provider_extra=end.provider_extra,
                    stop_reason="tool_use",
                    usage=usage,
                )
                await self._ports.save_message(message)
                await self._emit(
                    "message.completed",
                    {
                        "message_id": str(message.id),
                        "text": message.text,
                        "stop_reason": "tool_use",
                        "files": [],
                    },
                )
                exceeded = self._budget_exceeded()
                if exceeded is not None:
                    return exceeded
                return await self._run_batch(message, reply.parse_errors, None)
            return await self._end_turn(self._text_message(reply, end, usage), end.stop_reason)

    async def _end_turn(self, message: StepMessage, stop_reason: str) -> LoopOutcome | None:
        """도구 호출 없이 끝난 턴. 과업 모드에서 결과를 아직 내지 않았으면 독촉한다."""
        tool = self._config.submit_tool
        if tool is None:
            return await self._finish(message, stop_reason)
        message.stop_reason = stop_reason
        await self._ports.save_message(message)
        await self._emit(
            "message.completed",
            {
                "message_id": str(message.id),
                "text": message.text,
                "stop_reason": stop_reason,
                "files": [],
            },
        )
        if self._nudged:
            return self._failed(
                "output_missing", f"독촉한 뒤에도 {tool}(으)로 결과를 제출하지 않았다"
            )
        self._nudged = True
        self._forced_tool = tool
        assistant = message.assistant_message()
        if assistant.blocks:
            self._messages.append(assistant)
        # 독촉 글은 이 런의 모델 히스토리에만 둔다. 저장된 메시지에는 남지 않는다.
        self._append_user_text(SUBMIT_NUDGE_TEXT.format(tool=tool))
        log.info("agent.submit_nudged", run_id=str(self._config.run_id), step=self._steps)
        return None

    def _append_user_text(self, text: str) -> None:
        block = TextBlock(text=text)
        if self._messages and self._messages[-1].role == "user":
            last = self._messages[-1]
            self._messages[-1] = last.model_copy(update={"blocks": [*last.blocks, block]})
        else:
            self._messages.append(CanonicalMessage(role="user", blocks=[block]))

    def _text_blocks(self, reply: _ModelReply) -> list[Block]:
        return [TextBlock(text=reply.text)] if reply.text else []

    def _text_message(
        self, reply: _ModelReply, end: MessageEnd, usage: dict[str, Any]
    ) -> StepMessage:
        return StepMessage(
            id=reply.message_id,
            blocks=self._text_blocks(reply),
            provider_extra=end.provider_extra,
            usage=usage,
        )

    async def _finish(self, message: StepMessage, stop_reason: str) -> LoopOutcome:
        message.stop_reason = stop_reason
        await self._ports.save_message(message)
        collected = await self._ports.collect_files()
        await self._emit(
            "message.completed",
            {
                "message_id": str(message.id),
                "text": message.text,
                "stop_reason": stop_reason,
                "files": [item.model_dump(mode="json") for item in collected.files],
            },
        )
        return self._outcome(
            "succeeded",
            final_message_id=message.id,
            files_collected=True,
            changes_truncated=collected.truncated,
        )

    async def _step_limit(self) -> LoopOutcome:
        limit = self._config.limits.max_steps
        tool = self._config.submit_tool
        if tool is not None:
            # 과업은 결과 없이 성공으로 끝내지 않는다.
            outcome = self._failed(
                "output_missing",
                f"스텝 한도({limit}회)에 닿을 때까지 {tool}(으)로 결과를 제출하지 않았다",
            )
            outcome.limit = "steps"
            return outcome
        message = StepMessage(
            id=new_id(), blocks=[TextBlock(text=STEP_LIMIT_TEXT.format(limit=limit))]
        )
        outcome = await self._finish(message, "max_steps")
        outcome.limit = "steps"
        return outcome

    async def _call_model(
        self, step: int, max_output: int, forced: str | None = None
    ) -> _ModelReply:
        messages = await self._prepared_messages()
        update: dict[str, Any] = {"max_output_tokens": max_output}
        if forced is not None:
            update["tool_choice"] = ForcedTool(name=forced)
        request = ChatRequest(
            model=self._config.model_id,
            system=self._config.system_prompt,
            messages=messages,
            tools=self._specs,
            params=self._config.params.model_copy(update=update),
            metadata={
                "run_id": str(self._config.run_id),
                "session_id": str(self._config.session_id),
                "step": step,
            },
        )
        reply = _ModelReply(message_id=new_id(), sent_messages=len(messages))
        task = asyncio.create_task(self._consume(request, reply))
        kind = await self._wait(task)
        if kind is not None:
            task.cancel()
            await asyncio.wait({task})
            await self._save_partial(reply)
            raise _Interrupted(kind)
        task.result()
        return reply

    async def _consume(self, request: ChatRequest, reply: _ModelReply) -> None:
        # 취소되면 어댑터의 async generator 안으로 CancelledError가 들어가 HTTP 응답도 닫힌다.
        async for event in self._model.stream(request):
            if isinstance(event, TextDelta):
                reply.text_parts.append(event.text)
                await self._emit(
                    "message.delta", {"message_id": str(reply.message_id), "text": event.text}
                )
            elif isinstance(event, ThinkingDelta):
                await self._emit("thinking.delta", {"text": event.text})
            elif isinstance(event, ToolCallStart):
                reply.call_ids[event.index] = event.id
                await self._emit(
                    "tool_call.started", {"tool_call_id": event.id, "name": event.name}
                )
            elif isinstance(event, ToolCallDelta):
                await self._emit(
                    "tool_call.input_delta",
                    {
                        "tool_call_id": reply.call_ids.get(event.index, ""),
                        "json_fragment": event.json_fragment,
                    },
                )
            elif isinstance(event, ToolCallEnd):
                data, error = parse_arguments(event.arguments)
                reply.calls.append(
                    ToolUseBlock(
                        id=event.id,
                        name=event.name,
                        input=data,
                        provider_extra=event.provider_extra,
                    )
                )
                if error is not None:
                    reply.parse_errors[event.id] = error
                    continue
                tool = self._tools.get(event.name)
                await self._emit(
                    "tool_call.ready",
                    {
                        "tool_call_id": event.id,
                        "name": event.name,
                        "input": tool.display_input(data) if tool is not None else data,
                    },
                )
            elif isinstance(event, MessageEnd):
                reply.end = event
            else:
                reply.error = event

    async def _save_partial(self, reply: _ModelReply) -> None:
        # 중지로 끊긴 모델 출력은 글만 남긴다. 잘린 도구 호출은 실행하지 않았으므로 버린다(4.6).
        if not reply.text:
            return
        message = StepMessage(
            id=reply.message_id, blocks=[TextBlock(text=reply.text)], stop_reason="cancelled"
        )
        await self._ports.save_message(message)
        await self._emit(
            "message.completed",
            {
                "message_id": str(message.id),
                "text": message.text,
                "stop_reason": "cancelled",
                "files": [],
            },
        )

    async def _record_usage(
        self, step: int, end: MessageEnd, *, sent_messages: int | None
    ) -> dict[str, Any]:
        usage = end.usage
        cost = cost_usd(end.model, usage, self._config.model_prices)
        data: dict[str, Any] = {
            "step": step,
            "model": end.model,
            "key_index": end.key_index,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "estimated": usage.estimated,
            "cost_usd": cost,
        }
        await self._emit("usage", data)
        total = self._usage
        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens
        total.reasoning_tokens += usage.reasoning_tokens or 0
        total.cached_input_tokens += usage.cached_input_tokens or 0
        total.estimated = total.estimated or usage.estimated
        if cost is not None:
            total.cost_usd = (total.cost_usd or 0.0) + cost
        if end.model not in self._models:
            self._models.append(end.model)
        if sent_messages is not None:
            self._tracker.observe(usage.input_tokens, sent_messages)
        await self._ports.save_progress(self._steps, self._usage)
        return {key: value for key, value in data.items() if key != "step"}

    def _budget_exceeded(self) -> LoopOutcome | None:
        limits = self._config.limits
        total = self._usage.input_tokens + self._usage.output_tokens
        if total > limits.max_total_tokens:
            return self._failed(
                "token_budget", f"런 토큰 예산({limits.max_total_tokens})을 넘었다({total})"
            )
        cost = self._usage.cost_usd
        if limits.max_cost_usd is not None and cost is not None and cost > limits.max_cost_usd:
            return self._failed(
                "token_budget", f"런 비용 상한(${limits.max_cost_usd})을 넘었다(${cost:.4f})"
            )
        return None

    async def _run_batch(
        self,
        message: StepMessage,
        parse_errors: Mapping[str, str],
        resume: ResumeState | None,
    ) -> LoopOutcome | None:
        """스텝 메시지의 도구 호출을 순서대로 처리한다. 다 끝나면 None, 멈추거나 끝나면 결과."""
        for call in message.tool_uses():
            if call.id in message.results():
                continue
            self._check_interrupt()
            resumed = resume is not None and call.id == resume.pending.tool_call_id
            if resume is not None and resumed and resume.payload.kind == "question":
                await self._record(message, call, ToolResult.ok(resume.payload.answer or ""))
                continue
            tool = self._tools.get(call.name)
            error = parse_errors.get(call.id)
            if error is None:
                if tool is None:
                    known = ", ".join(sorted(self._tools))
                    error = f"UNKNOWN_TOOL: {call.name!r} 도구는 없다. 쓸 수 있는 도구: {known}"
                else:
                    error = validate_tool_input(tool, call.input)
            if error is not None or tool is None:
                self._invalid_streak += 1
                await self._record(message, call, ToolResult.error(error or "INVALID_INPUT"))
                if self._invalid_streak >= MAX_INVALID_INPUT_STREAK:
                    return self._failed(
                        "tool_input_invalid",
                        f"도구 입력 오류가 {MAX_INVALID_INPUT_STREAK}번 연속으로 났다",
                    )
                continue
            self._invalid_streak = 0
            if tool.pauses:
                return await self._pause(message, call, tool, "question")
            if resume is not None and resumed and resume.payload.decision == "reject":
                rejected = ToolResult(
                    status="rejected", content=rejection_text(resume.payload.note)
                )
                await self._record(message, call, rejected)
                continue
            if not resumed and requires_approval(self._config.approval_mode, tool.name, call.input):
                return await self._pause(message, call, tool, "approval")
            result, interrupted = await self._execute(tool, call)
            await self._record(message, call, result)
            if interrupted is not None:
                raise _Interrupted(interrupted)
            outcome = await self._after_tool(result)
            if outcome is not None:
                return outcome
            if tool.terminal and result.status == "ok":
                # 과업 모드(M8)의 submit_result처럼 결과를 내고 런을 끝내는 도구.
                collected = await self._ports.collect_files()
                return self._outcome(
                    "succeeded",
                    final_message_id=message.id,
                    files_collected=True,
                    changes_truncated=collected.truncated,
                    output=result.output,
                )
        self._messages.append(message.assistant_message())
        results = message.results_message(fill_missing=True)
        if results is not None:
            self._messages.append(results)
        return None

    async def _after_tool(self, result: ToolResult) -> LoopOutcome | None:
        if result.fatal:
            return self._failed(result.error_code or "tool_failed", result.content[:500])
        if result.error_code == "sandbox_unavailable":
            self._sandbox_failures += 1
            if self._sandbox_failures >= MAX_SANDBOX_FAILURES:
                return self._failed(
                    "sandbox_unavailable",
                    f"도구 프로세스를 띄우지 못했다({self._sandbox_failures}회). 셸과 샌드박스 "
                    "파이썬을 확인한다",
                )
        elif result.error_code == "workspace_missing":
            # 밖에서 폴더가 지워졌다. 빈 폴더로 다시 만들고 이 호출은 오류로 돌려준다(4.6).
            self._handle = await self._ports.recreate_workspace()
        return None

    async def _record(self, message: StepMessage, call: ToolUseBlock, result: ToolResult) -> None:
        message.blocks.append(
            ToolResultBlock(tool_use_id=call.id, content=result.content, is_error=result.is_error)
        )
        await self._ports.save_message(message)
        excerpt = result.output_excerpt or result.content
        await self._emit(
            "tool_call.completed",
            {
                "tool_call_id": call.id,
                "status": result.status,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": result.duration_ms,
                "peak_memory_bytes": result.peak_memory_bytes,
                "output_excerpt": excerpt,
                "output_bytes": result.output_bytes or utf8_len(excerpt),
                "output_storage_key": result.output_storage_key,
            },
        )
        log.info(
            "agent.tool_completed",
            run_id=str(self._config.run_id),
            tool_call_id=call.id,
            tool=call.name,
            status=result.status,
            duration_ms=result.duration_ms,
        )

    async def _pause(
        self,
        message: StepMessage,
        call: ToolUseBlock,
        tool: Tool,
        kind: Literal["question", "approval"],
    ) -> LoopOutcome:
        data = call.input
        pending = PendingInteraction(
            interaction_id=str(new_id()),
            kind=kind,
            tool_call_id=call.id,
            tool_name=tool.name,
            message_id=message.id,
            step=self._steps,
            completed_tool_call_ids=list(message.results()),
            elapsed_ms=self._elapsed_ms(),
            created_at=self._now(),
        )
        if kind == "question":
            raw_options = data.get("options")
            options = cast(list[Any], raw_options) if isinstance(raw_options, list) else None
            pending.question = str(data.get("question", ""))
            pending.options = [str(item) for item in options] if options is not None else None
            pending.allow_free_text = data.get("allow_free_text", True) is not False
        else:
            pending.input = tool.display_input(data)
            pending.cwd = tool.workdir(data)
            pending.timeout_sec = tool.timeout_for(data, self._config.tool_defaults)
        await self._emit("run.waiting_input", pending.event_data())
        log.info(
            "agent.waiting_input",
            run_id=str(self._config.run_id),
            kind=kind,
            tool=tool.name,
            tool_call_id=call.id,
        )
        return self._outcome("waiting_input", pending=pending, final_message_id=message.id)

    async def _execute(
        self, tool: Tool, call: ToolUseBlock
    ) -> tuple[ToolResult, InterruptKind | None]:
        capture = OutputCapture(lambda: self._ports.open_output_sink(call.id))

        async def emit_output(stream: OutputStream, text: str) -> None:
            await self._emit(
                "tool_call.output_delta", {"tool_call_id": call.id, "stream": stream, "text": text}
            )

        context = ToolContext(
            backend=self._backend,
            handle=self._handle,
            call_id=call.id,
            env=self._config.env,
            defaults=self._config.tool_defaults,
            capture=capture,
            emit_output=emit_output,
            memory_limit_mb=self._config.memory_limit_mb,
            max_processes=self._config.max_processes,
        )
        started = self._clock()
        task = asyncio.create_task(tool.execute(context, dict(call.input)))
        kind = await self._wait(task)
        if kind is not None:
            result = await self._stop_tool(tool, call, task)
        else:
            try:
                result = task.result()
            except SandboxError as exc:
                result = ToolResult.from_sandbox_error(exc)
        result.duration_ms = int((self._clock() - started) * 1000)
        storage_key = await capture.close()
        if capture.spill_failed:
            # 전문 저장에 실패해도 도구 결과는 그대로 쓴다. 다운로드는 이벤트의 발췌만 준다.
            log.warning("agent.tool_output_spill_failed", tool_call_id=call.id)
        if capture.total_bytes:
            result.output_excerpt = capture.event_text()
            result.output_bytes = capture.total_bytes
            result.output_storage_key = storage_key
        return result, kind

    async def _stop_tool(
        self, tool: Tool, call: ToolUseBlock, task: "asyncio.Task[ToolResult]"
    ) -> ToolResult:
        if tool.uses_process:
            try:
                await self._backend.kill(self._handle, call.id)
            except SandboxError as exc:
                log.warning("agent.kill_failed", tool_call_id=call.id, error=exc.message)
        done, _ = await asyncio.wait({task}, timeout=KILL_GRACE_SEC)
        if task in done and not task.cancelled() and task.exception() is None:
            return task.result().with_status("cancelled")
        task.cancel()
        await asyncio.wait({task})
        return ToolResult(status="cancelled", content="[도구 실행을 중지했다]", is_error=True)

    def _estimate_request(self) -> ChatRequest:
        return ChatRequest(
            model=self._config.model_id,
            system=self._config.system_prompt,
            messages=self._messages,
            tools=self._specs,
        )

    async def _maybe_compact(self, step: int) -> None:
        # 스텝 경계에서만 부르므로 도구 라운드는 언제나 끝나 있다(4.7의 3번).
        budget = context_budget(
            self._config.limits.context_budget_tokens, self._config.context_window
        )
        estimated = self._tracker.estimate(self._estimate_request())
        if estimated <= budget:
            return
        summary = await self._summarize(step)
        if summary is None:
            return
        text, usage = summary
        summary_id = await self._ports.save_summary(text, usage)
        self._messages = [
            CanonicalMessage(role="user", blocks=[TextBlock(text=summary_user_text(text))])
        ]
        self._tracker.reset()
        after = self._model.estimate_tokens(self._estimate_request())
        await self._emit(
            "context.compacted",
            {
                "before_tokens": estimated,
                "after_tokens": after,
                "summary_message_id": str(summary_id),
            },
        )
        log.info(
            "agent.context_compacted",
            run_id=str(self._config.run_id),
            before_tokens=estimated,
            after_tokens=after,
        )

    async def _summarize(self, step: int) -> tuple[str, dict[str, Any]] | None:
        request = ChatRequest(
            model=self._config.model_id,
            system=load_prompt("compaction.md"),
            messages=[
                CanonicalMessage(
                    role="user", blocks=[TextBlock(text=summary_request_text(self._messages))]
                )
            ],
            params=ModelParams(max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS, reasoning_effort="low"),
            metadata={
                "run_id": str(self._config.run_id),
                "session_id": str(self._config.session_id),
                "step": step,
                "purpose": "compaction",
            },
        )
        task = asyncio.create_task(self._collect_text(request))
        kind = await self._wait(task)
        if kind is not None:
            task.cancel()
            await asyncio.wait({task})
            raise _Interrupted(kind)
        text, end, error = task.result()
        if end is None or not text.strip():
            # 요약이 안 되면 압축 없이 계속한다. 모델 컨텍스트에는 아직 여유가 있다(예산은 60% 선).
            log.warning(
                "agent.compaction_failed",
                run_id=str(self._config.run_id),
                code=error.code if error is not None else "empty_summary",
            )
            return None
        usage = await self._record_usage(step, end, sent_messages=None)
        return text.strip(), usage

    async def _collect_text(
        self, request: ChatRequest
    ) -> tuple[str, MessageEnd | None, ErrorEvent | None]:
        parts: list[str] = []
        end: MessageEnd | None = None
        error: ErrorEvent | None = None
        async for event in self._model.stream(request):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, MessageEnd):
                end = event
            elif isinstance(event, ErrorEvent):
                error = event
        return "".join(parts), end, error

    async def _prepared_messages(self) -> list[CanonicalMessage]:
        # 첨부 이미지는 storage_key만 저장돼 있다. 보내기 직전에 내용을 채운다(app/llm/types.py).
        prepared: list[CanonicalMessage] = []
        for message in self._messages:
            if not any(
                isinstance(block, ImageBlock) and block.data_b64 is None and block.storage_key
                for block in message.blocks
            ):
                prepared.append(message)
                continue
            blocks: list[Block] = []
            for block in message.blocks:
                if isinstance(block, ImageBlock) and block.data_b64 is None and block.storage_key:
                    data = await self._image(block.storage_key)
                    blocks.append(block.model_copy(update={"data_b64": data}) if data else block)
                else:
                    blocks.append(block)
            prepared.append(message.model_copy(update={"blocks": blocks}))
        return prepared

    async def _image(self, storage_key: str) -> str | None:
        if storage_key not in self._images:
            self._images[storage_key] = await self._ports.load_image(storage_key)
        return self._images[storage_key]
