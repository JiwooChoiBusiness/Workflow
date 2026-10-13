from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from app.llm.gemini_adapter import GeminiAdapter, TransportError
from app.llm.key_pool import KeyPool
from app.llm.types import CanonicalMessage, ChatRequest, ErrorEvent, MessageEnd, TextBlock

KEYS = ["test-key-0000-aaaa", "test-key-1111-bbbb", "test-key-2222-cccc"]
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_pool(clock: Clock | None = None, keys: list[str] | None = None) -> KeyPool:
    return KeyPool(keys or KEYS, cooldown_sec=60, clock=clock or Clock(), now=lambda: NOW)


def test_pick_starts_at_cursor_and_skips_excluded_keys() -> None:
    pool = make_pool()

    assert pool.order() == [0, 1, 2]
    assert pool.pick("m") == 0
    assert pool.pick("m", exclude={0}) == 1
    assert pool.pick("m", exclude={0, 1, 2}) is None


def test_rate_limited_key_cools_down_for_that_model_only() -> None:
    pool = make_pool()

    pool.mark_rate_limited(0, "gemini-3.8-flash")

    assert pool.cursor == 1
    assert pool.pick("gemini-3.8-flash") == 1
    assert pool.is_cooling_down(0, "gemini-3.8-flash")
    # 한도는 모델별이라 다른 모델에서는 같은 키를 바로 쓸 수 있다.
    assert not pool.is_cooling_down(0, "gemini-3.7-flash")
    assert pool.pick("gemini-3.7-flash", exclude={1, 2}) == 0


def test_cooldown_expires_after_configured_seconds() -> None:
    clock = Clock()
    pool = make_pool(clock)
    pool.mark_rate_limited(0, "m")

    clock.now += 59
    assert pool.is_cooling_down(0, "m")
    clock.now += 1
    assert not pool.is_cooling_down(0, "m")


def test_all_keys_cooling_down_signals_model_fallback() -> None:
    pool = make_pool()

    for index in range(len(KEYS)):
        pool.mark_rate_limited(index, "m")

    assert pool.pick("m") is None
    assert pool.pick("other") is not None


def test_success_moves_cursor_to_the_working_key() -> None:
    pool = make_pool()

    pool.mark_success(2)

    assert pool.cursor == 2
    assert pool.order() == [2, 0, 1]


def test_cursor_is_not_moved_twice_by_concurrent_rate_limits() -> None:
    pool = make_pool()
    pool.mark_rate_limited(0, "m")  # 커서 0 → 1
    pool.mark_rate_limited(0, "other")  # 커서가 이미 1이므로 그대로

    assert pool.cursor == 1


def test_status_shows_counts_and_last4_only() -> None:
    clock = Clock()
    pool = make_pool(clock)
    pool.mark_success(0)
    pool.mark_rate_limited(1, "gemini-3.8-flash")
    pool.mark_failure(1)
    clock.now += 15

    status = pool.status()

    assert [entry.last4 for entry in status] == ["aaaa", "bbbb", "cccc"]
    assert (status[0].success_count, status[0].failure_count) == (1, 0)
    assert (status[1].failure_count, status[1].rate_limited_count) == (2, 1)
    assert status[1].last_429_at == NOW
    assert status[1].cooldown_remaining == {"gemini-3.8-flash": 45.0}
    assert status[2].cooldown_remaining == {}
    rendered = repr(status) + repr(pool)
    assert not any(key in rendered for key in KEYS)


def test_auth_failed_key_is_skipped_for_every_model_until_restart() -> None:
    clock = Clock()
    pool = make_pool(clock)

    pool.mark_auth_failed(0)
    clock.now += 10_000

    assert pool.is_disabled(0)
    assert pool.cursor == 1
    assert pool.pick("model-a") == 1
    assert pool.pick("model-b", exclude={1, 2}) is None
    assert not pool.all_disabled()
    status = pool.status()[0]
    assert (status.disabled, status.failure_count, status.rate_limited_count) == (True, 1, 0)

    pool.mark_auth_failed(1)
    pool.mark_auth_failed(2)
    assert pool.all_disabled()
    assert pool.pick("model-a") is None


def test_short_and_blank_keys() -> None:
    pool = KeyPool(["short", "", SecretStr("")], cooldown_sec=60)

    assert len(pool) == 1
    assert pool.status()[0].last4 == ""


def test_redact_masks_known_keys_and_common_patterns() -> None:
    pool = make_pool()
    # 비밀 탐지 도구와 키 접두어 검사(grep)에 걸리지 않도록 가짜 키는 실행 시에 만든다.
    google_style = "AIza" + "C" * 35
    token_style = "AQ" + "." + "D" * 40
    text = (
        f"key {KEYS[1]} failed; {google_style} {token_style} "
        "Authorization: Bearer abc.def ?key=zzz&alt=sse"
    )

    redacted = pool.redact(text)

    for secret in (KEYS[1], google_style, token_style, "abc.def", "zzz"):
        assert secret not in redacted
    assert "Bearer ***" in redacted
    assert "?key=***&alt=sse" in redacted


class RateLimitTransport:
    """(모델, 키 index)별 결과를 정해 둔 전송 계층. 정하지 않은 조합은 정상 응답."""

    def __init__(self, outcomes: Mapping[tuple[str, int], int]) -> None:
        self.outcomes = dict(outcomes)
        self.calls: list[tuple[str, int]] = []

    async def stream_chat(
        self, *, api_key: str, body: Mapping[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        call = (str(body["model"]), KEYS.index(api_key))
        self.calls.append(call)
        status = self.outcomes.get(call)
        if status is not None:
            raise TransportError(f"status {status}", status=status)
        yield {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}

    async def list_models(self, *, api_key: str) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None


class NoSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def request() -> ChatRequest:
    return ChatRequest(
        system="s", messages=[CanonicalMessage(role="user", blocks=[TextBlock(text="hi")])]
    )


def make_adapter(transport: RateLimitTransport, clock: Clock, sleep: NoSleep) -> GeminiAdapter:
    return GeminiAdapter(
        api_keys=KEYS,
        model_fallback=["model-a", "model-b", "model-c"],
        key_cooldown_sec=60,
        transport=transport,
        sleep=sleep,
        clock=clock,
    )


async def run(adapter: GeminiAdapter) -> list[Any]:
    return [event async for event in adapter.stream(request())]


async def test_429_rotates_keys_then_falls_back_and_400_fails_immediately() -> None:
    transport = RateLimitTransport(
        {("model-a", 0): 429, ("model-a", 1): 429, ("model-a", 2): 429, ("model-b", 0): 400}
    )
    sleep = NoSleep()
    adapter = make_adapter(transport, Clock(), sleep)

    events = await run(adapter)

    # model-a에서 키 3개가 모두 429 → model-b로 폴백 → 첫 키에서 400 → model-c는 시도하지 않는다.
    assert transport.calls == [("model-a", 0), ("model-a", 1), ("model-a", 2), ("model-b", 0)]
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert (error.code, error.status, error.model) == ("model_request_invalid", 400, "model-b")
    assert sleep.calls == []
    status = adapter.key_status()
    assert all("model-a" in entry.cooldown_remaining for entry in status)
    assert [entry.rate_limited_count for entry in status] == [1, 1, 1]
    assert status[0].failure_count == 2


async def test_exhausted_model_is_skipped_until_its_cooldown_ends() -> None:
    clock = Clock()
    transport = RateLimitTransport({("model-a", i): 429 for i in range(len(KEYS))})
    adapter = make_adapter(transport, clock, NoSleep())

    first = await run(adapter)
    assert transport.calls[-1] == ("model-b", 0)
    assert isinstance(first[-1], MessageEnd)
    assert first[-1].model == "model-b"

    transport.calls.clear()
    second = await run(adapter)
    # 쿨다운 중인 model-a는 요청을 보내지 않고 건너뛴다.
    assert transport.calls == [("model-b", 0)]
    assert isinstance(second[-1], MessageEnd)

    clock.now += 61
    transport.outcomes.clear()
    transport.calls.clear()
    await run(adapter)
    assert transport.calls == [("model-a", 0)]


async def test_auth_error_disables_the_key_and_moves_to_the_next_key() -> None:
    transport = RateLimitTransport({("model-a", 0): 401})
    adapter = make_adapter(transport, Clock(), NoSleep())

    first = await run(adapter)
    transport.calls.clear()
    second = await run(adapter)

    # 401을 받은 키 0은 빼고 같은 모델을 키 1로 다시 보낸다. 다음 요청부터는 키 0을 쓰지 않는다.
    assert isinstance(first[-1], MessageEnd)
    assert (first[-1].model, first[-1].key_index) == ("model-a", 1)
    assert transport.calls == [("model-a", 1)]
    assert isinstance(second[-1], MessageEnd)
    status = adapter.key_status()
    assert [entry.disabled for entry in status] == [True, False, False]


async def test_auth_errors_on_every_key_fail_as_model_auth_without_fallback() -> None:
    transport = RateLimitTransport({("model-a", i): 403 for i in range(len(KEYS))})
    adapter = make_adapter(transport, Clock(), NoSleep())

    events = await run(adapter)
    first_calls = list(transport.calls)
    transport.calls.clear()
    again = await run(adapter)

    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert (error.code, error.retryable, error.status) == ("model_auth", False, 403)
    assert "status 403" in error.message
    # 키 문제라 다른 모델로 폴백하지 않고, 키가 모두 빠진 뒤의 요청은 보내지도 않는다.
    assert first_calls == [("model-a", 0), ("model-a", 1), ("model-a", 2)]
    assert transport.calls == []
    assert isinstance(again[-1], ErrorEvent)
    assert (again[-1].code, again[-1].status) == ("model_auth", None)


async def test_auth_and_rate_limit_mix_falls_back_to_the_next_model() -> None:
    # 키 0은 401로 빠지고 나머지는 model-a에서 429. model-b에서는 쿨다운이 없는 키 1이 쓰인다.
    transport = RateLimitTransport({("model-a", 0): 401, ("model-a", 1): 429, ("model-a", 2): 429})
    adapter = make_adapter(transport, Clock(), NoSleep())

    events = await run(adapter)

    assert transport.calls == [("model-a", 0), ("model-a", 1), ("model-a", 2), ("model-b", 1)]
    assert isinstance(events[-1], MessageEnd)
    assert events[-1].model == "model-b"


async def test_rate_limits_on_every_model_end_as_model_unavailable() -> None:
    transport = RateLimitTransport(
        {(model, i): 429 for model in ("model-a", "model-b", "model-c") for i in range(len(KEYS))}
    )
    sleep = NoSleep()
    adapter = make_adapter(transport, Clock(), sleep)

    events = await run(adapter)

    assert len(transport.calls) == 9
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert (error.code, error.retryable) == ("model_unavailable", True)
    # 429는 기다리지 않고 바로 다음 키로 넘어간다.
    assert sleep.calls == []
