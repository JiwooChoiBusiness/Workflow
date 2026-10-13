import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog

from app.config import Settings
from app.db.types import utcnow
from app.llm.base import ModelAdapter
from app.llm.catalog import get_capabilities
from app.llm.gemini_adapter import GeminiAdapter
from app.llm.key_pool import KeyPool, KeyStatus
from app.llm.types import (
    CanonicalMessage,
    ChatRequest,
    ErrorEvent,
    MessageEnd,
    ModelParams,
    TextBlock,
    TextDelta,
)
from app.schemas.llm import (
    LlmAttempt,
    LlmError,
    LlmGeneration,
    LlmKeyStatus,
    LlmStatus,
    LlmTestResult,
    ModelCapabilityInfo,
    ModelEntry,
    ModelList,
)

log = structlog.get_logger("app.services.llm_status")

# 에이전트 빌더가 화면을 열 때마다 부르므로 라이브 목록은 잠시 기억해 둔다.
MODELS_CACHE_SEC = 600.0
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
# 모델 목록 조회에서 다음 키로 넘어가는 오류. 401·403도 키마다 다르다(폐기·권한, 설계 12.2).
_NEXT_KEY = frozenset(
    {"rate_limited", "model_auth", "upstream_error", "timeout", "connection_error"}
)
_MESSAGE_MAX = 300
_REPLY_MAX = 200
# 연결 테스트의 생성 호출. 사고 수준과 출력 상한을 낮춰 짧게 끝낸다(설계 12.2).
TEST_PROMPT = "연결 확인입니다. '확인'이라고만 답하세요."
TEST_PARAMS = ModelParams(max_output_tokens=256, reasoning_effort="low")


class _UpstreamError(Exception):
    def __init__(self, error: LlmError) -> None:
        super().__init__(error.code)
        self.error = error


@dataclass
class _Fetched:
    models: list[str] | None
    attempts: list[LlmAttempt]
    error: LlmError | None


class LlmStatusService:
    # 설정 화면용 Gemini 상태. 키 값은 응답·로그 어디에도 내보내지 않고 index와 마지막 4자리만 쓴다.
    # 키별 쿨다운·호출 수는 앱이 쓰는 어댑터(app.state.llm)의 키 풀에서 읽고, 모델 목록은 키를
    # 하나씩 직접 확인하려고 OpenAI 호환 GET /models를 따로 부른다.

    def __init__(
        self,
        settings: Settings,
        adapter: ModelAdapter,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._adapter = adapter
        self._keys = [key.get_secret_value() for key in settings.gemini_api_keys]
        # 키 원문과 Google 키 형식을 지우는 규칙은 키 풀의 것을 그대로 쓴다.
        self._redactor = KeyPool(settings.gemini_api_keys, cooldown_sec=0)
        self._transport = transport
        self._clock = clock
        self._cache: tuple[float, datetime, list[str]] | None = None

    @property
    def key_count(self) -> int:
        return len(self._keys)

    def _last4(self, index: int) -> str:
        return self._keys[index][-4:]

    def status(self) -> LlmStatus:
        settings = self._settings
        # 가짜 어댑터는 키를 쓰지 않아 키 풀 상태가 없다.
        pool = (
            {entry.index: entry for entry in self._adapter.key_status()}
            if isinstance(self._adapter, GeminiAdapter)
            else {}
        )
        now = utcnow()
        return LlmStatus(
            fake=settings.llm_fake,
            base_url=settings.gemini_openai_base_url,
            key_count=self.key_count,
            keys=[self._key_status(i, pool.get(i), now) for i in range(self.key_count)],
            fallback_models=list(settings.gemini_model_fallback),
            key_cooldown_sec=settings.gemini_key_cooldown_sec,
        )

    def _key_status(self, index: int, entry: KeyStatus | None, now: datetime) -> LlmKeyStatus:
        if entry is None:
            return LlmKeyStatus(index=index, last4=self._last4(index))
        cooldown = {
            model: now + timedelta(seconds=remaining)
            for model, remaining in entry.cooldown_remaining.items()
        }
        return LlmKeyStatus(
            index=index,
            last4=self._last4(index),
            cooldown_until=max(cooldown.values(), default=None),
            cooldown_by_model=cooldown,
            disabled=entry.disabled,
            success_count=entry.success_count,
            failure_count=entry.failure_count,
            rate_limited_count=entry.rate_limited_count,
            last_429_at=entry.last_429_at,
        )

    async def test(self, key_index: int | None = None) -> LlmTestResult:
        if self._settings.llm_fake:
            # 각본(LLM_FAKE_SCRIPT)의 턴을 쓰지 않도록 가짜 어댑터는 부르지 않는다.
            models = list(self._settings.gemini_model_fallback)
            return LlmTestResult(
                ok=True, fake=True, model_count=len(models), models=models, attempts=[]
            )
        if key_index is not None and not 0 <= key_index < self.key_count:
            raise ValueError(f"key_index는 0~{self.key_count - 1} 범위여야 한다")
        fetched = await self._fetch(only=key_index)
        generation = await self._generate()
        models = fetched.models or []
        last = fetched.attempts[-1] if fetched.attempts else None
        return LlmTestResult(
            ok=fetched.models is not None and generation.ok,
            fake=False,
            key_index=last.key_index if last else None,
            key_last4=last.key_last4 if last else None,
            latency_ms=last.latency_ms if last else None,
            model_count=len(models),
            models=models,
            attempts=fetched.attempts,
            generation=generation,
            error=fetched.error or generation.error,
        )

    async def _generate(self) -> LlmGeneration:
        request = ChatRequest(
            system="짧게 답한다.",
            messages=[CanonicalMessage(role="user", blocks=[TextBlock(text=TEST_PROMPT)])],
            params=TEST_PARAMS,
        )
        parts: list[str] = []
        end: MessageEnd | None = None
        failure: ErrorEvent | None = None
        started = self._clock()
        async for event in self._adapter.stream(request):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, MessageEnd):
                end = event
            elif isinstance(event, ErrorEvent):
                failure = event
        latency_ms = round((self._clock() - started) * 1000)
        reply = "".join(parts)[:_REPLY_MAX]
        if end is None:
            error = (
                LlmError(
                    code=failure.code,
                    message=self._mask(failure.message),
                    status_code=failure.status,
                )
                if failure is not None
                else LlmError(code="no_response", message="모델 응답이 끝나지 않았다")
            )
            log.warning("llm.test_generation_failed", code=error.code, latency_ms=latency_ms)
            return LlmGeneration(
                ok=False,
                model=failure.model if failure is not None else None,
                latency_ms=latency_ms,
                text=reply,
                error=error,
            )
        key_index = end.key_index
        log.info(
            "llm.test_generated",
            model=end.model,
            key_index=key_index,
            latency_ms=latency_ms,
            output_tokens=end.usage.output_tokens,
        )
        return LlmGeneration(
            ok=True,
            model=end.model,
            key_index=key_index,
            key_last4=(
                self._last4(key_index)
                if key_index is not None and 0 <= key_index < self.key_count
                else None
            ),
            latency_ms=latency_ms,
            stop_reason=end.stop_reason,
            text=reply,
            usage=end.usage,
        )

    async def models(self) -> ModelList:
        settings = self._settings
        fallback = list(settings.gemini_model_fallback)
        if settings.llm_fake:
            entries = [
                ModelEntry(id=m, fallback_index=i, capabilities=_capability_info(m))
                for i, m in enumerate(fallback)
            ]
            return ModelList(fake=True, models=entries, live_ok=False)

        live: list[str] | None = None
        fetched_at: datetime | None = None
        error: LlmError | None = None
        if self._cache is not None and self._clock() - self._cache[0] < MODELS_CACHE_SEC:
            _, fetched_at, live = self._cache
        else:
            fetched = await self._fetch(only=None)
            live, error = fetched.models, fetched.error
            if self._cache is not None and live is not None:
                fetched_at = self._cache[1]

        available = set(live) if live is not None else None
        entries = [
            ModelEntry(
                id=model,
                fallback_index=index,
                live=None if available is None else model in available,
                capabilities=_capability_info(model),
            )
            for index, model in enumerate(fallback)
        ]
        entries += [
            ModelEntry(id=model, live=True)
            for model in sorted(available or ())
            if model not in fallback
        ]
        return ModelList(
            fake=False,
            models=entries,
            live_ok=live is not None,
            live_error=error,
            fetched_at=fetched_at,
        )

    async def _fetch(self, *, only: int | None) -> _Fetched:
        if not self._keys:
            error = LlmError(code="no_api_keys", message="GEMINI_API_KEYS가 비어 있다")
            return _Fetched(models=None, attempts=[], error=error)
        indexes = [only] if only is not None else list(range(self.key_count))
        attempts: list[LlmAttempt] = []
        error: LlmError | None = None
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, transport=self._transport) as client:
            for index in indexes:
                started = self._clock()
                try:
                    models = await self._list_models(client, index)
                except _UpstreamError as exc:
                    error = exc.error
                    attempts.append(self._attempt(index, started, error=error))
                    log.warning(
                        "llm.models_failed",
                        key_index=index,
                        code=error.code,
                        status_code=error.status_code,
                    )
                    # 400·404처럼 요청 자체가 틀린 오류는 다른 키로 가도 같다.
                    if error.code not in _NEXT_KEY:
                        break
                    continue
                attempts.append(self._attempt(index, started))
                log.info("llm.models_fetched", key_index=index, count=len(models))
                self._cache = (self._clock(), utcnow(), models)
                return _Fetched(models=models, attempts=attempts, error=None)
        return _Fetched(models=None, attempts=attempts, error=error)

    def _attempt(self, index: int, started: float, *, error: LlmError | None = None) -> LlmAttempt:
        return LlmAttempt(
            key_index=index,
            key_last4=self._last4(index),
            ok=error is None,
            status_code=error.status_code if error else 200,
            latency_ms=round((self._clock() - started) * 1000),
            error=error,
        )

    async def _list_models(self, client: httpx.AsyncClient, index: int) -> list[str]:
        url = self._settings.gemini_openai_base_url.rstrip("/") + "/models"
        try:
            response = await client.get(
                url, headers={"Authorization": f"Bearer {self._keys[index]}"}
            )
        except httpx.TimeoutException:
            raise _UpstreamError(LlmError(code="timeout", message="응답 시간 초과")) from None
        except httpx.HTTPError as exc:
            message = self._mask(f"{type(exc).__name__}: {exc}")
            raise _UpstreamError(LlmError(code="connection_error", message=message)) from None
        if response.status_code != 200:
            raise _UpstreamError(
                LlmError(
                    code=_error_code(response.status_code),
                    message=self._mask(_upstream_message(response)),
                    status_code=response.status_code,
                )
            )
        try:
            data = response.json()["data"]
            ids = [str(item["id"]) for item in data if isinstance(item, dict) and "id" in item]
        except (ValueError, KeyError, TypeError):
            raise _UpstreamError(
                LlmError(
                    code="invalid_response",
                    message="모델 목록 응답 형식이 OpenAI 호환 형식이 아니다",
                    status_code=response.status_code,
                )
            ) from None
        # Gemini는 "models/gemini-..."로 주지만 채팅 호출과 폴백 설정은 접두 없는 이름을 쓴다.
        return sorted({model_id.removeprefix("models/") for model_id in ids})

    def _mask(self, text: str) -> str:
        return self._redactor.redact(text)[:_MESSAGE_MAX]


def _capability_info(model: str) -> ModelCapabilityInfo:
    caps = get_capabilities(model)
    return ModelCapabilityInfo(
        context_window=caps.context_window,
        max_output_tokens=caps.max_output_tokens,
        supports_tools=caps.supports_tools,
        reasoning_efforts=list(caps.reasoning_efforts) if caps.supports_reasoning_effort else [],
        known=caps.known,
    )


def _error_code(status_code: int) -> str:
    if status_code == 429:
        return "rate_limited"
    if status_code in (401, 403):
        return "model_auth"
    if 400 <= status_code < 500:
        return "model_request_invalid"
    return "upstream_error"


def _upstream_message(response: httpx.Response) -> str:
    # Google은 오류를 {"error": {...}} 또는 [{"error": {...}}]로 준다.
    try:
        payload: Any = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return f"HTTP {response.status_code}"
