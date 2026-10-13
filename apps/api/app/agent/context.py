"""세션 메시지와 모델 히스토리 사이의 변환, <context> 블록, 컨텍스트 예산(설계 4.7, 4.8).

메시지 행 하나가 스텝 하나다. assistant 행의 blocks에는 모델이 낸 text·tool_use 블록 뒤에 그
도구들의 tool_result 블록이 붙는다. 모델에 보낼 때는 assistant 메시지와 tool_result만 담은 user
메시지로 나눈다. 행에 따로 열이 없는 메시지 단위 provider_extra(텍스트 응답의 사고 서명)는 text
블록의 MESSAGE_EXTRA_KEY 키에 담는다. 정규 블록 모델은 모르는 키를 무시하므로 화면에는 영향이 없다.
"""

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, cast

import structlog
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.llm.token_estimate import estimate_messages
from app.llm.types import (
    Block,
    CanonicalMessage,
    ChatRequest,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

log = structlog.get_logger("app.agent")

MESSAGE_EXTRA_KEY = "message_provider_extra"
# user 메시지의 <context> 블록 표시. 화면은 message.text를 쓰고 이 블록은 보여 주지 않는다.
CONTEXT_BLOCK_KIND = "context"
SUMMARY_OPEN = "<이전 대화 요약>"
SUMMARY_CLOSE = "</이전 대화 요약>"
SUMMARY_FOLLOW_UP = "위 요약을 바탕으로 계속 진행한다."
NOT_EXECUTED = "[실행되지 않음: 이 도구를 실행하기 전에 런이 끝났다]"
DEFAULT_CONTEXT_BUDGET = 200_000
CONTEXT_WINDOW_SHARE = 0.6
TRANSCRIPT_TOOL_RESULT_CHARS = 4000
TRANSCRIPT_TOOL_INPUT_CHARS = 2000

_BLOCK = TypeAdapter[Block](Block)


class StoredMessage(BaseModel):
    """메시지 행에서 루프가 쓰는 부분."""

    id: uuid.UUID
    seq: int
    role: Literal["user", "assistant", "system_summary"]
    blocks: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    text: str = ""


class StepMessage(BaseModel):
    """스텝 하나의 assistant 메시지. 실행한 도구 결과도 같은 행에 붙는다."""

    id: uuid.UUID
    blocks: list[Block] = Field(default_factory=list[Block])
    provider_extra: dict[str, Any] | None = None
    stop_reason: str | None = None
    usage: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.blocks if isinstance(block, TextBlock))

    def tool_uses(self) -> list[ToolUseBlock]:
        return [block for block in self.blocks if isinstance(block, ToolUseBlock)]

    def results(self) -> dict[str, ToolResultBlock]:
        return {
            block.tool_use_id: block for block in self.blocks if isinstance(block, ToolResultBlock)
        }

    def assistant_message(self) -> CanonicalMessage:
        blocks: list[Block] = [
            block
            for block in self.blocks
            if isinstance(block, ToolUseBlock) or (isinstance(block, TextBlock) and block.text)
        ]
        return CanonicalMessage(role="assistant", blocks=blocks, provider_extra=self.provider_extra)

    def results_message(self, *, fill_missing: bool) -> CanonicalMessage | None:
        """tool_use 순서대로 결과를 모은 user 메시지. fill_missing이면 없는 결과를 채운다."""
        results = self.results()
        blocks: list[Block] = []
        for call in self.tool_uses():
            result = results.get(call.id)
            if result is None:
                if not fill_missing:
                    continue
                result = ToolResultBlock(tool_use_id=call.id, content=NOT_EXECUTED, is_error=True)
            blocks.append(result)
        return CanonicalMessage(role="user", blocks=blocks) if blocks else None


def encode_blocks(blocks: Sequence[Block]) -> list[dict[str, Any]]:
    return [block.model_dump(mode="json", exclude_none=True) for block in blocks]


def encode_step(step: StepMessage) -> list[dict[str, Any]]:
    encoded = encode_blocks(step.blocks)
    if step.provider_extra:
        texts = [item for item in encoded if item.get("type") == "text"]
        if texts:
            texts[-1][MESSAGE_EXTRA_KEY] = step.provider_extra
        else:
            encoded.append({"type": "text", "text": "", MESSAGE_EXTRA_KEY: step.provider_extra})
    return encoded


def decode_blocks(raw: Sequence[Mapping[str, Any]]) -> tuple[list[Block], dict[str, Any] | None]:
    blocks: list[Block] = []
    extra: dict[str, Any] | None = None
    for item in raw:
        found = item.get(MESSAGE_EXTRA_KEY)
        if isinstance(found, dict):
            extra = cast(dict[str, Any], found)
        try:
            block = _BLOCK.validate_python(item)
        except ValidationError:
            log.warning("agent.block_invalid", block_type=str(item.get("type")))
            continue
        if isinstance(block, TextBlock) and not block.text:
            continue
        blocks.append(block)
    return blocks, extra


def decode_step(stored: StoredMessage) -> StepMessage:
    blocks, extra = decode_blocks(stored.blocks)
    return StepMessage(id=stored.id, blocks=blocks, provider_extra=extra)


def summary_user_text(summary: str) -> str:
    return f"{SUMMARY_OPEN}\n{summary.strip()}\n{SUMMARY_CLOSE}\n{SUMMARY_FOLLOW_UP}"


def _user_blocks(raw: Sequence[Mapping[str, Any]]) -> list[Block]:
    blocks, _ = decode_blocks(raw)
    # user 행에는 text·image만 있다. 다른 블록이 섞여 있으면 히스토리가 깨지므로 뺀다.
    return [block for block in blocks if isinstance(block, TextBlock | ImageBlock)]


def _merge_adjacent(messages: list[CanonicalMessage]) -> list[CanonicalMessage]:
    merged: list[CanonicalMessage] = []
    for message in messages:
        if not message.blocks:
            continue
        if merged and merged[-1].role == message.role:
            previous = merged[-1]
            merged[-1] = CanonicalMessage(
                role=message.role,
                blocks=[*previous.blocks, *message.blocks],
                provider_extra=message.provider_extra or previous.provider_extra,
            )
            continue
        merged.append(message)
    return merged


def build_history(rows: Sequence[StoredMessage]) -> list[CanonicalMessage]:
    """세션 메시지를 모델 히스토리로. 마지막 system_summary가 있으면 그 요약부터 시작한다(4.7).

    결과가 없는 tool_use(중지·종료·장애로 끝난 런)에는 "실행되지 않음" 결과를 채워 히스토리가
    프로바이더 규칙(도구 호출 뒤에는 결과)을 지키게 한다.
    """
    start = 0
    for index, row in enumerate(rows):
        if row.role == "system_summary":
            start = index
    messages: list[CanonicalMessage] = []
    for row in rows[start:]:
        if row.role == "system_summary":
            text = row.text or "".join(
                block.text for block in _user_blocks(row.blocks) if isinstance(block, TextBlock)
            )
            messages.append(
                CanonicalMessage(role="user", blocks=[TextBlock(text=summary_user_text(text))])
            )
        elif row.role == "user":
            messages.append(CanonicalMessage(role="user", blocks=_user_blocks(row.blocks)))
        else:
            step = decode_step(row)
            messages.append(step.assistant_message())
            results = step.results_message(fill_missing=True)
            if results is not None:
                messages.append(results)
    return _merge_adjacent(messages)


class AttachmentNote(BaseModel):
    path: str
    size: int
    mime: str


def context_text(
    *,
    now: datetime,
    source: str,
    external_user: str | None = None,
    attachments: Sequence[AttachmentNote] = (),
    inputs: Mapping[str, Any] | None = None,
) -> str:
    lines = [
        "<context>",
        f"현재 시각: {now.isoformat(timespec='seconds')}",
        f"세션 소스: {source}",
    ]
    if external_user:
        lines.append(f"외부 사용자: {external_user}")
    if inputs:
        # 서비스 API chat-messages의 inputs. 호출자가 정한 값이라 그대로 보여 준다.
        lines.append(f"입력 값: {_clip(json.dumps(dict(inputs), ensure_ascii=False), 4000)}")
    if attachments:
        lines.append("첨부 파일(작업공간 상대 경로):")
        lines.extend(f"- {item.path} ({item.size}바이트, {item.mime})" for item in attachments)
    lines.append("</context>")
    return "\n".join(lines)


def user_message_blocks(
    text: str, *, context: str, images: Sequence[ImageBlock] = ()
) -> list[dict[str, Any]]:
    """user 메시지 행의 blocks. 사용자 글, 첨부 이미지, <context> 순서다(4.8)."""
    blocks: list[dict[str, Any]] = [TextBlock(text=text).model_dump(mode="json")]
    blocks.extend(image.model_dump(mode="json", exclude_none=True) for image in images)
    context_block = TextBlock(text=context).model_dump(mode="json")
    context_block["kind"] = CONTEXT_BLOCK_KIND
    blocks.append(context_block)
    return blocks


def context_budget(configured: int | None, context_window: int) -> int:
    """압축 임계(4.6). 설정값이 없으면 200,000과 모델 컨텍스트의 60% 중 작은 값."""
    if configured is not None:
        return configured
    return min(DEFAULT_CONTEXT_BUDGET, int(context_window * CONTEXT_WINDOW_SHARE))


class ContextTracker:
    """다음 요청의 입력 토큰 추정(4.7의 2번).

    직전 응답의 실제 input_tokens에 그 뒤로 붙은 메시지의 휴리스틱 추정을 더한다. 직전 응답이 없으면
    요청 전체를 휴리스틱으로 잰다.
    """

    def __init__(self, estimate: Callable[[ChatRequest], int]) -> None:
        self._estimate = estimate
        self._base_tokens: int | None = None
        self._base_count = 0

    def observe(self, input_tokens: int, sent_messages: int) -> None:
        if input_tokens > 0:
            self._base_tokens = input_tokens
            self._base_count = sent_messages

    def reset(self) -> None:
        self._base_tokens = None
        self._base_count = 0

    def estimate(self, req: ChatRequest) -> int:
        if self._base_tokens is None or self._base_count > len(req.messages):
            return self._estimate(req)
        return self._base_tokens + estimate_messages(req.messages[self._base_count :])


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... ({len(text) - limit}자 생략)"


def history_transcript(messages: Sequence[CanonicalMessage]) -> str:
    """요약 요청에 넣는 대화 기록. 도구 호출이 든 히스토리를 그대로 보내지 않고 글로 펴서 보낸다.

    도구 정의 없이 tool_calls가 든 히스토리를 보내거나 사고 서명을 다시 맞출 일이 없어진다.
    """
    lines: list[str] = []
    for message in messages:
        speaker = "사용자" if message.role == "user" else "어시스턴트"
        for block in message.blocks:
            if isinstance(block, TextBlock):
                lines.append(f"[{speaker}]\n{block.text}")
            elif isinstance(block, ImageBlock):
                lines.append(f"[{speaker}] (이미지 첨부: {block.filename or block.media_type})")
            elif isinstance(block, ToolUseBlock):
                arguments = json.dumps(block.input, ensure_ascii=False)
                lines.append(
                    f"[도구 호출 {block.name} {block.id}] "
                    f"{_clip(arguments, TRANSCRIPT_TOOL_INPUT_CHARS)}"
                )
            else:
                label = "도구 오류" if block.is_error else "도구 결과"
                lines.append(
                    f"[{label} {block.tool_use_id}]\n"
                    f"{_clip(block.content, TRANSCRIPT_TOOL_RESULT_CHARS)}"
                )
    return "\n\n".join(lines)


def summary_request_text(messages: Sequence[CanonicalMessage]) -> str:
    return (
        "다음은 지금까지의 대화 기록이다.\n<대화 기록>\n"
        f"{history_transcript(messages)}\n</대화 기록>\n"
        "지시에 따라 이 기록을 요약한다."
    )
