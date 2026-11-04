import asyncio
import json
import math
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

import structlog
from pydantic import SecretStr

from app.llm.catalog import ModelCapabilities, get_capabilities, resolve_reasoning_effort
from app.llm.errors import ErrorClass, classify, daily_block_seconds
from app.llm.errors import TransportError as TransportError
from app.llm.key_pool import KeyPool, KeyStatus, KeyTier
from app.llm.token_estimate import estimate_request, estimate_text
from app.llm.types import (
    ERR_MODEL_AUTH,
    ERR_MODEL_REQUEST_INVALID,
    ERR_MODEL_STREAM_INTERRUPTED,
    ERR_MODEL_UNAVAILABLE,
    Block,
    CanonicalMessage,
    ChatRequest,
    ErrorEvent,
    ForcedTool,
    ImageBlock,
    LLMError,
    MessageEnd,
    ModelEvent,
    StopReason,
    TextBlock,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)

if TYPE_CHECKING:
    import openai

    from app.config import Settings
    from app.llm.embeddings import EmbeddingTransport

log = structlog.get_logger("app.llm")

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
RETRY_DELAY_SEC = 2.0
# 유료 키는 하나뿐이라 429에서 다른 키로 갈 수 없다. retryDelay만큼 기다려 같은 키로 이만큼
# 다시 보낸 뒤 다음 모델로 간다. retryDelay가 없으면 기본값을 쓰고, 너무 길면 상한까지만 기다린다.
PAID_RATE_LIMIT_RETRIES = 3
PAID_RATE_LIMIT_WAIT_SEC = 30.0
PAID_RATE_LIMIT_MAX_WAIT_SEC = 60.0
# OpenAI 호환 /embeddings가 한 요청에 받는 입력 수(2026-11-02 실측).
MAX_EMBED_INPUTS = 100

# 2026-10-13 라이브 확인. 사고 서명은 스트림 청크의
#   choices[0].delta.tool_calls[i].extra_content.google.thought_signature
#     (도구 호출. 병렬 호출이면 첫 호출에만 붙는다)
#   choices[0].delta.extra_content.google.thought_signature (텍스트 응답의 마지막 청크)
# 에 온다. 다음 요청에서는 같은 키를 tool_calls[i]와 assistant 메시지에 그대로 펼쳐 보낸다.
# 이 목록에 없는 provider_extra 키는 다른 프로바이더 것으로 보고 보내지 않는다.
PROVIDER_EXTRA_KEYS = ("extra_content",)
THOUGHT_SIGNATURE_PATH = ("extra_content", "google", "thought_signature")
# Gemini 3는 히스토리의 도구 호출(스텝마다 첫 호출)에 서명이 없으면 400을 낸다. 서명이 없는
# 호출(가짜 어댑터·다른 모델이 만든 히스토리, 병렬 호출의 두 번째부터)에는 Google 문서가 안내하는
# 검증 생략용 더미를 넣는다.
SKIP_SIGNATURE = "skip_thought_signature_validator"
ALL_KEYS_DISABLED = (
    "GEMINI_API_KEYS의 키가 모두 401·403을 받아 빠졌다. 키를 고치고 서버를 다시 띄운다"
)

_KNOWN_FINISH = ("stop", "length", "tool_calls", "content_filter")
_LOG_CONTEXT_KEYS = ("run_id", "session_id", "step")


def _as_dict(value: object) -> dict[str, Any] | None:
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _as_list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _pick_extra(source: Mapping[str, Any] | None) -> dict[str, Any]:
    if not source:
        return {}
    return {key: source[key] for key in PROVIDER_EXTRA_KEYS if source.get(key) is not None}


def _merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        current = _as_dict(target.get(key))
        incoming = _as_dict(value)
        if current is not None and incoming is not None:
            merged = dict(current)
            _merge(merged, incoming)
            target[key] = merged
        else:
            target[key] = value


def thought_signature(provider_extra: Mapping[str, Any] | None) -> str | None:
    node: object = provider_extra
    for key in THOUGHT_SIGNATURE_PATH:
        current = _as_dict(node)
        if current is None:
            return None
        node = current.get(key)
    return node if isinstance(node, str) else None


def _skip_signature_extra() -> dict[str, Any]:
    return {"extra_content": {"google": {"thought_signature": SKIP_SIGNATURE}}}


def _tool_call_extra(block: ToolUseBlock) -> dict[str, Any]:
    extra = _pick_extra(block.provider_extra)
    if thought_signature(extra) is None:
        _merge(extra, _skip_signature_extra())
    return extra


def _received_extra(call: Mapping[str, Any]) -> dict[str, Any]:
    extra = _pick_extra(call)
    # 보낼 때 넣은 더미는 프로바이더가 준 값이 아니므로 정규 메시지로 되돌릴 때 뺀다.
    return {} if extra == _skip_signature_extra() else extra


class ChatTransport(Protocol):
    def stream_chat(
        self, *, api_key: str, body: Mapping[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]: ...

    async def list_models(self, *, api_key: str) -> list[str]: ...

    async def aclose(self) -> None: ...


def _error_payload(body: object) -> dict[str, Any] | None:
    # Gemini는 오류 본문을 [{"error": {"code", "message", "status"}}]처럼 리스트로 감싼다.
    items = _as_list(body)
    if items:
        body = items[0]
    data = _as_dict(body)
    if data is None:
        return None
    return _as_dict(data.get("error")) or data


def _error_text(body: object, fallback: str) -> str:
    payload = _error_payload(body)
    if payload is not None:
        message = payload.get("message")
        status = payload.get("status")
        if isinstance(message, str) and message:
            return f"{status}: {message}" if isinstance(status, str) and status else message
    return fallback


def _transport_error(exc: "openai.APIError") -> TransportError:
    import openai

    if isinstance(exc, openai.APIStatusError):
        return TransportError(
            _error_text(exc.body, exc.message),
            status=exc.status_code,
            kind="http",
            payload=_error_payload(exc.body),
        )
    if isinstance(exc, openai.APITimeoutError):
        return TransportError("모델 API 요청 시간이 초과됐다", kind="timeout")
    if isinstance(exc, openai.APIConnectionError):
        return TransportError(f"모델 API 연결 오류: {exc.message}", kind="connection")
    # 스트림 도중 온 {"error": {...}} 이벤트
    payload = _error_payload(exc.body)
    code = payload.get("code") if payload is not None else None
    return TransportError(
        _error_text(exc.body, exc.message),
        status=code if isinstance(code, int) else None,
        kind="stream",
        payload=payload,
    )


def _embedding_vectors(response: object, count: int) -> list[list[float]]:
    items = _as_list((_as_dict(response) or {}).get("data"))
    if len(items) != count:
        raise TransportError(
            f"임베딩 응답의 항목 수({len(items)})가 입력 수({count})와 다르다", kind="stream"
        )
    entries = [_as_dict(item) or {} for item in items]
    # 응답은 입력 순서대로 오지만 index가 있으면 그것을 믿는다.
    if all(isinstance(entry.get("index"), int) for entry in entries):
        entries.sort(key=lambda entry: cast(int, entry["index"]))
    vectors: list[list[float]] = []
    for entry in entries:
        values = entry.get("embedding")
        if not isinstance(values, list) or not values:
            raise TransportError("임베딩 응답에 벡터가 없다", kind="stream")
        vector: list[float] = []
        for value in cast(list[object], values):
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise TransportError("임베딩 벡터에 숫자가 아닌 값이 있다", kind="stream")
            vector.append(float(value))
        vectors.append(vector)
    return vectors


class OpenAICompatTransport:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_sec: float = 600.0,
        connect_timeout_sec: float = 10.0,
        http_client: Any = None,
    ) -> None:
        self._base_url = base_url
        self._timeout_sec = timeout_sec
        self._connect_timeout_sec = connect_timeout_sec
        self._http_client = http_client
        self._client: openai.AsyncOpenAI | None = None

    def _get_client(self) -> "openai.AsyncOpenAI":
        if self._client is None:
            # SDK는 첫 호출 때 불러온다(기동 직후 RSS를 낮게 유지, 설계 2.7).
            import openai

            self._client = openai.AsyncOpenAI(
                base_url=self._base_url,
                api_key="unused",
                # SDK가 429를 같은 키로 재시도하며 시간을 쓰지 않게 끈다.
                # 키 순환·폴백은 어댑터가 한다.
                max_retries=0,
                timeout=openai.Timeout(self._timeout_sec, connect=self._connect_timeout_sec),
                http_client=self._http_client,
            )
        return self._client

    async def stream_chat(
        self, *, api_key: str, body: Mapping[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        import openai

        client = self._get_client().with_options(api_key=api_key)
        try:
            # create() 대신 post(cast_to=object)로 청크를 응답 JSON 그대로(dict) 받는다.
            # extra_content 같은 프로바이더 필드를 모델 변환 없이 원문대로 보존하기 위해서다.
            stream = await client.post(
                "/chat/completions",
                cast_to=object,
                body=dict(body),
                stream=True,
                stream_cls=openai.AsyncStream[object],
            )
        except openai.APIError as exc:
            raise _transport_error(exc) from exc
        try:
            async for item in stream:
                chunk = _as_dict(item)
                if chunk is not None:
                    yield chunk
        except openai.APIError as exc:
            raise _transport_error(exc) from exc
        except ValueError as exc:
            raise TransportError(f"응답 청크를 해석하지 못했다: {exc}", kind="stream") from exc
        finally:
            await stream.close()

    async def list_models(self, *, api_key: str) -> list[str]:
        import openai

        client = self._get_client().with_options(api_key=api_key)
        try:
            page = await client.models.list()
        except openai.APIError as exc:
            raise _transport_error(exc) from exc
        # 호환 레이어는 "models/gemini-3.8-flash"처럼 접두어를 붙여 준다.
        return [model.id.removeprefix("models/") for model in page.data]

    async def embed(
        self, *, api_key: str, model: str, inputs: Sequence[str], dimensions: int | None
    ) -> list[list[float]]:
        """OpenAI 호환 /embeddings. 입력 순서대로 벡터를 돌려준다(3단계 설계 6.6).

        응답에 usage가 없다(2026-11-02 실측). 사용량은 부르는 쪽이 추정한다.
        """
        if len(inputs) > MAX_EMBED_INPUTS:
            raise ValueError(f"임베딩 입력은 한 번에 {MAX_EMBED_INPUTS}개까지다: {len(inputs)}")
        if not inputs:
            return []
        import openai

        client = self._get_client().with_options(api_key=api_key)
        body: dict[str, Any] = {"model": model, "input": list(inputs)}
        if dimensions is not None:
            body["dimensions"] = dimensions
        try:
            # embeddings.create()는 encoding_format=base64를 덧붙이는데 호환 레이어는 모르는 필드를
            # 400으로 거부한다. 보낼 필드를 직접 정하려고 post를 쓴다.
            response = await client.post("/embeddings", cast_to=object, body=body)
        except openai.APIError as exc:
            raise _transport_error(exc) from exc
        return _embedding_vectors(response, len(inputs))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


def _image_part(block: ImageBlock, supports_images: bool) -> dict[str, Any]:
    if supports_images and block.data_b64:
        url = f"data:{block.media_type};base64,{block.data_b64}"
        return {"type": "image_url", "image_url": {"url": url}}
    return {"type": "text", "text": f"[이미지 첨부: {block.filename or block.media_type}]"}


def _user_to_openai(message: CanonicalMessage, supports_images: bool) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for block in message.blocks:
        match block:
            case ToolResultBlock():
                converted.append(
                    {"role": "tool", "tool_call_id": block.tool_use_id, "content": block.content}
                )
            case TextBlock():
                parts.append({"type": "text", "text": block.text})
            case ImageBlock():
                parts.append(_image_part(block, supports_images))
            case ToolUseBlock():
                raise ValueError("user 메시지에 tool_use 블록이 있다")
    if parts:
        # tool 메시지가 먼저, 같은 메시지의 텍스트·이미지는 그 뒤의 user 메시지로 보낸다.
        only_text = len(parts) == 1 and parts[0]["type"] == "text"
        converted.append({"role": "user", "content": parts[0]["text"] if only_text else parts})
    return converted


def _assistant_to_openai(message: CanonicalMessage, strip_text: bool) -> dict[str, Any] | None:
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in message.blocks:
        match block:
            case TextBlock():
                texts.append(block.text)
            case ToolUseBlock():
                # Gemini가 돌려준 인자와 같은 압축 형식이라 왕복해도 글자가 바뀌지 않는다.
                arguments = json.dumps(block.input, ensure_ascii=False, separators=(",", ":"))
                call: dict[str, Any] = {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": arguments},
                }
                call.update(_tool_call_extra(block))
                calls.append(call)
            case ImageBlock() | ToolResultBlock():
                raise ValueError(f"assistant 메시지에 {block.type} 블록이 있다")
    text = "\n\n".join(t for t in texts if t)
    if not text and not calls:
        # 빈 assistant 메시지는 Gemini가 빈 parts로 거부할 수 있어 보내지 않는다.
        return None
    converted: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        converted["tool_calls"] = calls
    if not strip_text:
        converted.update(_pick_extra(message.provider_extra))
    return converted


def to_openai_messages(
    system: str,
    messages: Sequence[CanonicalMessage],
    *,
    supports_images: bool = True,
    strip_text_signatures: bool = False,
) -> list[dict[str, Any]]:
    # strip_text_signatures는 텍스트 응답(메시지 단위)의 서명만 뺀다. 도구 호출 서명은 빼면
    # 400이 나므로 그대로 둔다.
    converted: list[dict[str, Any]] = []
    if system:
        converted.append({"role": "system", "content": system})
    for message in messages:
        if message.role == "user":
            converted.extend(_user_to_openai(message, supports_images))
        else:
            assistant = _assistant_to_openai(message, strip_text_signatures)
            if assistant is not None:
                converted.append(assistant)
    return converted


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    texts: list[str] = []
    for part in _as_list(content):
        part_dict = _as_dict(part)
        if part_dict is not None and part_dict.get("type") == "text":
            texts.append(str(part_dict.get("text", "")))
    return "\n\n".join(texts)


def _content_blocks(content: object) -> list[Block]:
    if isinstance(content, str):
        return [TextBlock(text=content)] if content else []
    blocks: list[Block] = []
    for part in _as_list(content):
        part_dict = _as_dict(part) or {}
        kind = part_dict.get("type")
        if kind == "text":
            blocks.append(TextBlock(text=str(part_dict.get("text", ""))))
        elif kind == "image_url":
            url = str((_as_dict(part_dict.get("image_url")) or {}).get("url", ""))
            header, _, data = url.partition(",")
            if not header.startswith("data:") or not header.endswith(";base64"):
                raise ValueError("data URL이 아닌 이미지는 정규 메시지로 바꿀 수 없다")
            media_type = header.removeprefix("data:").removesuffix(";base64")
            blocks.append(ImageBlock(media_type=media_type, data_b64=data))
        else:
            raise ValueError(f"알 수 없는 content 파트: {kind}")
    return blocks


def _parse_arguments(arguments: object) -> dict[str, Any]:
    if not arguments:
        return {}
    parsed = json.loads(str(arguments))
    result = _as_dict(parsed)
    if result is None:
        raise ValueError("도구 인자가 JSON 객체가 아니다")
    return result


def from_openai_messages(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[str, list[CanonicalMessage]]:
    # to_openai_messages의 역변환.
    # 연속된 tool 메시지와 바로 뒤의 user 메시지를 한 user 메시지로 묶는다.
    system_parts: list[str] = []
    result: list[CanonicalMessage] = []
    pending: list[Block] = []

    def flush() -> None:
        if pending:
            result.append(CanonicalMessage(role="user", blocks=list(pending)))
            pending.clear()

    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            system_parts.append(_content_text(message.get("content")))
        elif role == "tool":
            pending.append(
                ToolResultBlock(
                    tool_use_id=str(message.get("tool_call_id", "")),
                    content=_content_text(message.get("content")),
                )
            )
        elif role == "user":
            blocks = [*pending, *_content_blocks(message.get("content"))]
            pending.clear()
            result.append(CanonicalMessage(role="user", blocks=blocks))
        elif role == "assistant":
            flush()
            assistant_blocks: list[Block] = []
            text = _content_text(message.get("content"))
            if text:
                assistant_blocks.append(TextBlock(text=text))
            for raw_call in _as_list(message.get("tool_calls")):
                call = _as_dict(raw_call) or {}
                function = _as_dict(call.get("function")) or {}
                assistant_blocks.append(
                    ToolUseBlock(
                        id=str(call.get("id", "")),
                        name=str(function.get("name", "")),
                        input=_parse_arguments(function.get("arguments")),
                        provider_extra=_received_extra(call) or None,
                    )
                )
            result.append(
                CanonicalMessage(
                    role="assistant",
                    blocks=assistant_blocks,
                    provider_extra=_pick_extra(message) or None,
                )
            )
        else:
            raise ValueError(f"알 수 없는 role: {role}")
    flush()
    return "\n\n".join(part for part in system_parts if part), result


def build_request_body(
    req: ChatRequest,
    *,
    model: str,
    caps: ModelCapabilities,
    max_tokens: int,
    strip_text_signatures: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": to_openai_messages(
            req.system,
            req.messages,
            supports_images=caps.supports_images,
            strip_text_signatures=strip_text_signatures,
        ),
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": max_tokens,
    }
    params = req.params
    # Gemini 3.6부터 temperature·top_p는 무시되고 다음 세대는 400이다(4단계 설계 2절). 능력표가
    # 허용하는 모델에만 보낸다.
    if caps.supports_sampling_params:
        if params.temperature is not None:
            body["temperature"] = params.temperature
        if params.top_p is not None:
            body["top_p"] = params.top_p
    if req.tools and caps.supports_tools:
        # strict는 보내지 않는다. 입력 검증은 루프가 jsonschema로 한다(설계 T10).
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in req.tools
        ]
        choice = params.tool_choice
        body["tool_choice"] = (
            {"type": "function", "function": {"name": choice.name}}
            if isinstance(choice, ForcedTool)
            else "auto"
        )
    effort = resolve_reasoning_effort(caps, params.reasoning_effort)
    if effort is not None:
        body["reasoning_effort"] = effort
    return body


def normalize_stop_reason(raw: str | None, *, has_tool_calls: bool) -> StopReason:
    if raw == "length":
        return "max_tokens"
    if raw == "content_filter":
        return "refusal"
    # Gemini 호환 레이어는 도구 호출이 있어도 finish_reason을 "stop"으로 준다(2026-10-13 확인).
    if raw == "tool_calls" or has_tool_calls:
        return "tool_use"
    if raw not in _KNOWN_FINISH:
        log.warning("llm.unknown_finish_reason", raw_stop=raw)
    return "end_turn"


def normalize_usage(raw: Mapping[str, Any] | None) -> Usage | None:
    if not raw:
        return None
    prompt = _as_int(raw.get("prompt_tokens"))
    completion = _as_int(raw.get("completion_tokens"))
    total = raw.get("total_tokens")
    details = _as_dict(raw.get("completion_tokens_details")) or {}
    reported = details.get("reasoning_tokens")
    reasoning: int | None
    if isinstance(reported, int):
        # OpenAI 방식: completion_tokens가 사고 토큰을 이미 포함한다.
        reasoning = reported
        output = completion
    elif isinstance(total, int):
        # Gemini 호환 레이어는 사고 토큰을 completion_tokens에서 빼고 total_tokens에만 넣는다
        # (2026-10-13 확인: 빈 응답에서 completion 0, prompt 20, total 25).
        reasoning = max(total - prompt - completion, 0)
        output = completion + reasoning
    else:
        reasoning = None
        output = completion
    cached = (_as_dict(raw.get("prompt_tokens_details")) or {}).get("cached_tokens")
    return Usage(
        input_tokens=prompt,
        output_tokens=output,
        reasoning_tokens=reasoning,
        cached_input_tokens=cached if isinstance(cached, int) else None,
    )


@dataclass
class _ToolCallState:
    index: int
    id: str
    name: str
    fragments: list[str] = field(default_factory=list[str])
    extra: dict[str, Any] = field(default_factory=dict[str, Any])
    ended: bool = False

    @property
    def arguments(self) -> str:
        return "".join(self.fragments)


class StreamNormalizer:
    """Chat Completions 스트림 청크(dict)를 ModelEvent로 바꾼다. 요청 1회마다 새로 만든다."""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.tool_calls: list[_ToolCallState] = []
        self.finish_reason: str | None = None
        self.raw_usage: dict[str, Any] | None = None
        self.message_extra: dict[str, Any] = {}
        self._by_provider_index: dict[int, _ToolCallState] = {}
        self._open: _ToolCallState | None = None

    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    @property
    def has_content(self) -> bool:
        return bool(self.text_parts) or bool(self.tool_calls)

    def feed(self, chunk: Mapping[str, Any]) -> list[ModelEvent]:
        events: list[ModelEvent] = []
        usage = _as_dict(chunk.get("usage"))
        if usage:
            # Gemini는 매 청크에 누적 usage를 준다. 마지막 값이 최종값이다.
            self.raw_usage = usage
        for raw_choice in _as_list(chunk.get("choices")):
            choice = _as_dict(raw_choice)
            if choice is None or choice.get("index", 0) != 0:
                continue
            delta = _as_dict(choice.get("delta")) or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                self.text_parts.append(content)
                events.append(TextDelta(text=content))
            for raw_call in _as_list(delta.get("tool_calls")):
                call = _as_dict(raw_call)
                if call is not None:
                    events.extend(self._feed_tool_call(call))
            _merge(self.message_extra, _pick_extra(delta))
            finish = choice.get("finish_reason")
            if isinstance(finish, str) and finish:
                self.finish_reason = finish
                events.extend(self.close())
        return events

    def close(self) -> list[ModelEvent]:
        self._open = None
        return [self._end(state) for state in self.tool_calls if not state.ended]

    def _feed_tool_call(self, call: Mapping[str, Any]) -> list[ModelEvent]:
        events: list[ModelEvent] = []
        provider_index = call.get("index")
        raw_id = call.get("id")
        call_id = raw_id if isinstance(raw_id, str) and raw_id else None
        function = _as_dict(call.get("function")) or {}
        raw_name = function.get("name")
        name = raw_name if isinstance(raw_name, str) else ""
        raw_fragment = function.get("arguments")
        fragment = raw_fragment if isinstance(raw_fragment, str) else ""

        state = self._match(provider_index, call_id)
        if state is None:
            if self._open is not None and not self._open.ended:
                events.append(self._end(self._open))
            state = _ToolCallState(
                index=len(self.tool_calls),
                id=call_id or f"call_{uuid.uuid4().hex[:12]}",
                name=name,
            )
            self.tool_calls.append(state)
            if isinstance(provider_index, int):
                self._by_provider_index[provider_index] = state
            self._open = state
            events.append(ToolCallStart(index=state.index, id=state.id, name=state.name))
        elif name and not state.name:
            state.name = name
        _merge(state.extra, _pick_extra(call))
        if fragment:
            state.fragments.append(fragment)
            events.append(ToolCallDelta(index=state.index, json_fragment=fragment))
        return events

    def _match(self, provider_index: object, call_id: str | None) -> _ToolCallState | None:
        if isinstance(provider_index, int):
            state = self._by_provider_index.get(provider_index)
            if state is not None and not state.ended and call_id in (None, state.id):
                return state
            return None
        # Gemini 호환 레이어는 index 없이 호출마다 완성된 청크를 보낸다(2026-10-13 확인).
        # id가 없거나 같으면 진행 중인 호출에 이어지는 조각으로 본다.
        if self._open is not None and call_id in (None, self._open.id):
            return self._open
        return None

    def _end(self, state: _ToolCallState) -> ToolCallEnd:
        state.ended = True
        return ToolCallEnd(
            index=state.index,
            id=state.id,
            name=state.name,
            arguments=state.arguments,
            provider_extra=dict(state.extra) or None,
        )


def _paid_wait(retry_after: float | None) -> float:
    wait = PAID_RATE_LIMIT_WAIT_SEC if retry_after is None else retry_after
    return min(max(wait, 0.0), PAID_RATE_LIMIT_MAX_WAIT_SEC)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _log_fallback(
    models: Sequence[str], position: int, failures: Sequence[str], context: Mapping[str, Any]
) -> None:
    if position + 1 < len(models):
        log.warning(
            "llm.model_fallback",
            from_model=models[position],
            to_model=models[position + 1],
            reason=failures[-1][:300],
            **context,
        )


def _is_signature_error(message: str) -> bool:
    lowered = message.lower()
    return "thought_signature" in lowered or "thought signature" in lowered


def _has_text_signature(req: ChatRequest) -> bool:
    return any(
        _pick_extra(message.provider_extra)
        for message in req.messages
        if message.role == "assistant"
    )


def _log_context(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: metadata[key] for key in _LOG_CONTEXT_KEYS if isinstance(metadata.get(key), str | int)
    }


@dataclass
class _Attempt:
    emitted: bool = False
    error: ErrorEvent | None = None
    signature_error: bool = False
    model: str | None = None
    key_index: int | None = None
    max_tokens: int = 0
    normalizer: StreamNormalizer | None = None


class GeminiAdapter:
    name = "gemini"

    def __init__(
        self,
        *,
        api_keys: Sequence[SecretStr | str],
        model_fallback: Sequence[str],
        base_url: str = DEFAULT_BASE_URL,
        key_cooldown_sec: float = 60,
        transport: ChatTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        retry_delay_sec: float = RETRY_DELAY_SEC,
        paid_api_keys: Sequence[SecretStr | str] = (),
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.base_url = base_url
        self._pool = KeyPool(
            api_keys, cooldown_sec=key_cooldown_sec, clock=clock, now=now, paid_keys=paid_api_keys
        )
        self._fallback = list(dict.fromkeys(model for model in model_fallback if model))
        self._transport: ChatTransport = (
            transport if transport is not None else OpenAICompatTransport(base_url)
        )
        self._sleep = sleep
        self._now = now
        self._retry_delay_sec = retry_delay_sec
        # 직전 응답의 실제 입력 토큰 / 휴리스틱 추정치.
        # 한국어처럼 문자당 토큰이 많은 입력에서 휴리스틱이 작게 나오는 것을 보정한다.
        self._calibration = 1.0

    @classmethod
    def from_settings(cls, settings: "Settings") -> "GeminiAdapter":
        paid = settings.gemini_paid_api_key
        return cls(
            api_keys=settings.gemini_api_keys,
            model_fallback=settings.gemini_model_fallback,
            base_url=settings.gemini_openai_base_url,
            key_cooldown_sec=settings.gemini_key_cooldown_sec,
            paid_api_keys=[paid] if paid is not None else [],
        )

    @property
    def model_fallback(self) -> list[str]:
        return list(self._fallback)

    @property
    def key_pool(self) -> KeyPool:
        return self._pool

    @property
    def embedding_transport(self) -> "EmbeddingTransport | None":
        # 임베딩 클라이언트가 채팅과 같은 SDK 클라이언트(연결)를 쓴다(3단계 설계 6.6). 시험에서
        # 넣은 채팅 전용 전송에는 embed가 없다.
        if callable(getattr(self._transport, "embed", None)):
            return cast("EmbeddingTransport", self._transport)
        return None

    def key_status(self) -> list[KeyStatus]:
        return self._pool.status()

    def model_chain(self, requested: str | None) -> list[str]:
        if requested is None:
            return list(self._fallback)
        if requested in self._fallback:
            return self._fallback[self._fallback.index(requested) :]
        return [requested]

    def capabilities(self, model_id: str) -> ModelCapabilities:
        return get_capabilities(model_id)

    def estimate_tokens(self, req: ChatRequest) -> int:
        return math.ceil(estimate_request(req) * self._calibration)

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def list_models(self) -> list[str]:
        if not self._pool.count("free"):
            raise LLMError(ERR_MODEL_AUTH, "GEMINI_API_KEYS가 비어 있다")
        last_error = "모든 키가 429를 받았다"
        auth_message = ALL_KEYS_DISABLED
        for index in self._pool.order():
            if self._pool.is_disabled(index):
                continue
            try:
                return await self._transport.list_models(api_key=self._pool.secret(index))
            except TransportError as err:
                kind = classify(err).kind
                last_error = self._pool.redact(err.message)
                if kind == "rate_limited":
                    continue
                if kind == "auth":
                    self._pool.mark_auth_failed(index)
                    log.warning("llm.key_disabled", key_index=index, status=err.status)
                    auth_message = f"{ALL_KEYS_DISABLED}. 마지막 응답: {last_error}"
                    continue
                code = ERR_MODEL_REQUEST_INVALID if kind == "invalid" else ERR_MODEL_UNAVAILABLE
                raise LLMError(code, last_error, retryable=kind == "transient") from err
        if self._pool.all_disabled():
            raise LLMError(ERR_MODEL_AUTH, auth_message)
        raise LLMError(ERR_MODEL_UNAVAILABLE, last_error, retryable=True)

    async def stream(self, req: ChatRequest) -> AsyncIterator[ModelEvent]:
        context = _log_context(req.metadata)
        chain = self.model_chain(req.model)
        if not chain:
            yield ErrorEvent(
                code=ERR_MODEL_REQUEST_INVALID,
                message="사용할 모델이 없다(GEMINI_MODEL_FALLBACK)",
                retryable=False,
            )
            return
        if not self._pool.count(req.tier):
            setting = "GEMINI_API_KEYS" if req.tier == "free" else "GEMINI_PAID_API_KEY"
            yield ErrorEvent(code=ERR_MODEL_AUTH, message=f"{setting}가 비어 있다", retryable=False)
            return

        start = 0
        max_tokens: int | None = None
        strip_text = False
        empty_retried = False
        signature_retried = False
        while True:
            attempt = _Attempt()
            # 소비자가 중간에 멈추면(취소·aclose) 안쪽 제너레이터와 HTTP 응답도 바로 닫히게 한다.
            events = self._run_chain(req, chain[start:], attempt, max_tokens, strip_text, context)
            async with aclosing(events):
                async for event in events:
                    yield event
            model = attempt.model
            if attempt.error is not None or attempt.normalizer is None or model is None:
                error = attempt.error or ErrorEvent(
                    code=ERR_MODEL_UNAVAILABLE, message="모델 호출 결과가 없다", retryable=True
                )
                if (
                    attempt.signature_error
                    and not signature_retried
                    and not attempt.emitted
                    and model is not None
                    and _has_text_signature(req)
                ):
                    # 사고 서명 400이면 텍스트 응답의 서명을 비우고 1회 다시 보낸다(설계 4.3).
                    # 도구 호출 서명은 비우면 그것대로 400이라 그대로 둔다.
                    signature_retried = True
                    strip_text = True
                    start = chain.index(model)
                    log.warning("llm.signature_retry", model=model, **context)
                    continue
                yield error
                return

            normalizer = attempt.normalizer
            if (
                normalizer.finish_reason == "length"
                and not normalizer.has_content
                and not empty_retried
            ):
                doubled = min(attempt.max_tokens * 2, get_capabilities(model).max_output_tokens)
                if doubled > attempt.max_tokens:
                    # 사고 토큰이 출력 상한을 다 쓴 빈 응답(ADR-0003).
                    # 상한을 2배로 올려 1회만 다시 시도한다.
                    empty_retried = True
                    max_tokens = doubled
                    start = chain.index(model)
                    log.warning(
                        "llm.empty_response_retry", model=model, max_tokens=doubled, **context
                    )
                    continue
            if normalizer.finish_reason == "stop" and not normalizer.has_content:
                log.warning("llm.empty_response", model=model, **context)
            end = self._message_end(req, attempt, normalizer, model)
            log.info(
                "llm.completed",
                model=model,
                key_index=end.key_index,
                stop_reason=end.stop_reason,
                input_tokens=end.usage.input_tokens,
                output_tokens=end.usage.output_tokens,
                estimated=end.usage.estimated,
                **context,
            )
            yield end
            return

    async def _run_chain(
        self,
        req: ChatRequest,
        models: Sequence[str],
        attempt: _Attempt,
        max_tokens: int | None,
        strip_text: bool,
        context: Mapping[str, Any],
    ) -> AsyncGenerator[ModelEvent, None]:
        tier = req.tier
        failures: list[str] = []
        # 일일 한도로 실패한(건너뛴) 모델. 모델마다 failures에 한 줄씩 들어간다.
        daily: list[str] = []
        # 키가 모두 빠졌을 때의 오류. 이번 요청에서 401·403을 받았으면 마지막 응답을 덧붙인다.
        auth_status: int | None = None
        auth_message = ALL_KEYS_DISABLED
        for position, model in enumerate(models):
            if self._pool.any_exhausted(model, tier):
                # 같은 모델의 다른 키로 가면 하루 총량이 키 수만큼 늘어난다(3단계 설계 Z2).
                failures.append(f"{model}: 오늘의 일일 한도를 다 써서 건너뛴다")
                daily.append(model)
                _log_fallback(models, position, failures, context)
                continue
            caps = get_capabilities(model)
            limit = min(max_tokens or req.params.max_output_tokens, caps.max_output_tokens)
            body = build_request_body(
                req, model=model, caps=caps, max_tokens=limit, strip_text_signatures=strip_text
            )
            tried: set[int] = set()
            key_index: int | None = None
            transient_retried = False
            # 유료 등급에서 같은 키로 기다린 횟수(429·5xx·다른 요청의 hold 모두).
            paid_waits = 0
            while True:
                if key_index is None:
                    key_index = self._pool.pick(model, exclude=tried, tier=tier)
                    if key_index is None and self._pool.all_disabled(tier):
                        # 키 문제라 다른 모델로 가도 같다.
                        attempt.error = ErrorEvent(
                            code=ERR_MODEL_AUTH,
                            message=auth_message,
                            retryable=False,
                            model=model,
                            status=auth_status,
                        )
                        return
                    can_wait = tier == "paid" and paid_waits < PAID_RATE_LIMIT_RETRIES
                    if key_index is None and can_wait:
                        # 동시에 돈 다른 유료 요청이 키를 hold해 둔 동안이다. 바로 다음 모델로 가지
                        # 않고 가장 짧은 대기만큼 기다려 다시 고른다(3단계 설계 14.4).
                        held = self._shortest_hold(model, tier, tried)
                        if held is not None:
                            paid_waits += 1
                            await self._sleep(held)
                            continue
                    if key_index is None:
                        failures.append(f"{model}: 모든 키가 429 또는 쿨다운 중")
                        break
                attempt.model, attempt.key_index, attempt.max_tokens = model, key_index, limit
                normalizer = StreamNormalizer()
                try:
                    chunks = self._transport.stream_chat(
                        api_key=self._pool.secret(key_index), body=body
                    )
                    async with aclosing(chunks):
                        async for chunk in chunks:
                            for event in normalizer.feed(chunk):
                                if not isinstance(event, ThinkingDelta):
                                    attempt.emitted = True
                                yield event
                    if normalizer.finish_reason is None:
                        raise TransportError("finish_reason 없이 스트림이 끝났다", kind="stream")
                except TransportError as err:
                    verdict = classify(err)
                    kind = verdict.kind
                    message = self._pool.redact(err.message)
                    wait = 0.0
                    if kind == "rate_limited":
                        wait = self._note_rate_limit(key_index, model, tier, verdict)
                    elif kind == "auth":
                        self._pool.mark_auth_failed(key_index)
                    else:
                        self._pool.mark_failure(key_index)
                    log.warning(
                        "llm.attempt_failed",
                        model=model,
                        key_index=key_index,
                        tier=tier,
                        status=err.status,
                        kind=err.kind,
                        quota_scope=verdict.scope,
                        retry_after=verdict.retry_after,
                        error=message[:300],
                        **context,
                    )
                    if attempt.emitted:
                        # 이미 내보낸 출력이 있어 폴백할 수 없다. 루프가 스텝을 다시 실행한다(4.6).
                        attempt.error = ErrorEvent(
                            code=ERR_MODEL_STREAM_INTERRUPTED,
                            message=message,
                            retryable=True,
                            model=model,
                            status=err.status,
                        )
                        return
                    if kind == "auth":
                        log.warning(
                            "llm.key_disabled", key_index=key_index, status=err.status, **context
                        )
                        auth_status = err.status
                        auth_message = f"{ALL_KEYS_DISABLED}. 마지막 응답: {message}"
                        tried.add(key_index)
                        key_index = None
                        continue
                    if kind == "rate_limited":
                        if verdict.scope == "day":
                            # 일일 한도는 같은 모델의 다른 키로 순환하지 않고 다음 모델로 간다.
                            failures.append(f"{model}: 일일 한도 {message[:200]}")
                            daily.append(model)
                            break
                        if tier == "paid":
                            # 유료 키는 하나라 같은 키로 기다렸다 다시 보낸다. 무료 키로 넘어가지
                            # 않는다(3단계 설계 14.4).
                            if paid_waits < PAID_RATE_LIMIT_RETRIES:
                                paid_waits += 1
                                await self._sleep(wait)
                                continue
                            failures.append(f"{model}: 429 {message[:200]}")
                            break
                        tried.add(key_index)
                        key_index = None
                        continue
                    if kind == "transient" and tier == "paid":
                        # 5xx도 429처럼 같은 키로 기다려 다시 보낸다(임베딩과 같은 규칙, 6.6).
                        if paid_waits < PAID_RATE_LIMIT_RETRIES:
                            paid_waits += 1
                            wait = _paid_wait(verdict.retry_after)
                            self._pool.hold(key_index, model, wait)
                            await self._sleep(wait)
                            continue
                        failures.append(f"{model}: {err.status or err.kind} {message[:200]}")
                        break
                    if kind == "transient":
                        if not transient_retried:
                            # 5xx·연결 오류는 같은 키로 한 번 더, 그래도 안 되면 다음 모델.
                            transient_retried = True
                            await self._sleep(self._retry_delay_sec)
                            continue
                        failures.append(f"{model}: {err.status or err.kind} {message[:200]}")
                        break
                    attempt.error = ErrorEvent(
                        code=ERR_MODEL_REQUEST_INVALID,
                        message=message,
                        retryable=False,
                        model=model,
                        status=err.status,
                    )
                    attempt.signature_error = _is_signature_error(message)
                    return
                self._pool.mark_success(key_index)
                for event in normalizer.close():
                    yield event
                attempt.normalizer = normalizer
                return
            _log_fallback(models, position, failures, context)
        retry_after: float | None = None
        if daily and len(daily) == len(failures):
            # 모두 일일 한도라 기다리면 풀린다. 먼저 풀리는 모델까지 남은 초를 준다(3단계 12.4).
            waits = [wait for name in daily if (wait := self._daily_wait(name, tier)) is not None]
            retry_after = float(math.ceil(min(waits))) if waits else None
        attempt.error = ErrorEvent(
            code=ERR_MODEL_UNAVAILABLE,
            message="모든 모델이 실패했다: " + "; ".join(failures),
            retryable=True,
            model=models[-1] if models else None,
            retry_after=retry_after,
        )

    def _daily_wait(self, model: str, tier: KeyTier) -> float | None:
        """이 모델을 다시 고를 수 있을 때까지 남은 초. 등급의 키 하나라도 일일 한도면 건너뛰므로
        한도에 걸린 키들 가운데 가장 늦게 풀리는 때다."""
        waits = [
            status.exhausted_remaining[model]
            for status in self._pool.status()
            if status.tier == tier and model in status.exhausted_remaining
        ]
        return max(waits) if waits else None

    def _shortest_hold(self, model: str, tier: KeyTier, tried: set[int]) -> float | None:
        waits = [
            status.cooldown_remaining[model]
            for status in self._pool.status()
            if status.tier == tier
            and status.index not in tried
            and not status.disabled
            and model in status.cooldown_remaining
        ]
        if not waits:
            return None
        return min(max(min(waits), 0.0) + 0.1, PAID_RATE_LIMIT_MAX_WAIT_SEC)

    def _note_rate_limit(self, index: int, model: str, tier: KeyTier, verdict: ErrorClass) -> float:
        """429를 키 풀에 적고 같은 키로 다시 보내기 전에 기다릴 초를 돌려준다(0이면 안 보낸다)."""
        if verdict.scope == "day":
            seconds = daily_block_seconds(verdict.retry_after, self._now())
            if verdict.retry_after is not None and verdict.retry_after < seconds:
                log.info(
                    "llm.daily_limit_until_reset",
                    model=model,
                    retry_after=verdict.retry_after,
                    blocked_sec=round(seconds),
                )
            self._pool.exhaust(index, model, seconds)
            return 0.0
        if tier == "paid":
            wait = _paid_wait(verdict.retry_after)
            self._pool.mark_failure(index)
            self._pool.hold(index, model, wait)
            return wait
        self._pool.mark_rate_limited(index, model)
        return 0.0

    def _message_end(
        self, req: ChatRequest, attempt: _Attempt, normalizer: StreamNormalizer, model: str
    ) -> MessageEnd:
        heuristic = estimate_request(req)
        usage = normalize_usage(normalizer.raw_usage)
        if usage is None:
            produced = normalizer.text + "".join(call.arguments for call in normalizer.tool_calls)
            usage = Usage(
                input_tokens=math.ceil(heuristic * self._calibration),
                output_tokens=estimate_text(produced),
                estimated=True,
            )
        elif heuristic > 0 and usage.input_tokens > 0:
            self._calibration = min(max(usage.input_tokens / heuristic, 0.5), 4.0)
        return MessageEnd(
            stop_reason=normalize_stop_reason(
                normalizer.finish_reason, has_tool_calls=bool(normalizer.tool_calls)
            ),
            usage=usage,
            raw_stop=normalizer.finish_reason,
            model=model,
            key_index=attempt.key_index,
            provider_extra=dict(normalizer.message_extra) or None,
        )
