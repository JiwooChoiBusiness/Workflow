"""키 등급(무료·유료), 같은 키로 기다리기(hold), 일일 한도(exhaust)와 채팅 어댑터의 429 처리
(3단계 설계 Z1, Z2, 14.4).
"""

from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.llm.errors import TransportError
from app.llm.gemini_adapter import GeminiAdapter
from app.llm.key_pool import KeyPool
from app.llm.types import CanonicalMessage, ChatRequest, ErrorEvent, MessageEnd, TextBlock

FREE = ["test-key-0000-aaaa", "test-key-1111-bbbb", "test-key-2222-cccc"]
PAID = "test-paid-9999-zzzz"
KEYS = [*FREE, PAID]
PAID_INDEX = 3
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_pool(clock: Clock | None = None, *, paid: list[str] | None = None) -> KeyPool:
    return KeyPool(
        FREE,
        cooldown_sec=60,
        clock=clock or Clock(),
        now=lambda: NOW,
        paid_keys=[PAID] if paid is None else paid,
    )


def test_paid_keys_follow_the_free_keys() -> None:
    pool = make_pool()

    assert len(pool) == 4
    assert (pool.count("free"), pool.count("paid")) == (3, 1)
    assert [pool.tier_of(i) for i in range(4)] == ["free", "free", "free", "paid"]
    assert pool.secret(PAID_INDEX) == PAID
    assert pool.order() == [0, 1, 2]
    assert pool.order("paid") == [PAID_INDEX]
    assert [entry.tier for entry in pool.status()] == ["free", "free", "free", "paid"]


def test_pick_only_returns_keys_of_the_requested_tier() -> None:
    pool = make_pool()

    assert pool.pick("m") == 0
    assert pool.pick("m", tier="paid") == PAID_INDEX
    assert pool.pick("m", exclude={0, 1, 2}) is None
    assert pool.pick("m", exclude={PAID_INDEX}, tier="paid") is None
    no_paid = make_pool(paid=[])
    assert no_paid.pick("m", tier="paid") is None
    assert no_paid.count("paid") == 0
    assert no_paid.order("paid") == []
    assert not no_paid.all_disabled("paid")


def test_pick_prefers_a_usable_key_over_the_cursor() -> None:
    clock = Clock()
    pool = make_pool(clock)
    pool.mark_rate_limited(0, "chat")

    assert pool.cursor == 1
    assert pool.pick("embed", prefer=0) == 0
    pool.hold(0, "embed", 30)
    assert pool.pick("embed", prefer=0) == 1
    # 다른 등급의 키는 고르지 않는다.
    assert pool.pick("embed", prefer=PAID_INDEX) == 1
    assert pool.pick("embed", tier="paid", prefer=0) == PAID_INDEX


def test_paid_calls_do_not_move_the_free_cursor() -> None:
    pool = make_pool()
    pool.mark_success(1)

    pool.mark_success(PAID_INDEX)
    pool.mark_rate_limited(PAID_INDEX, "m")

    assert pool.cursor == 1
    assert pool.order() == [1, 2, 0]
    assert pool.order("paid") == [PAID_INDEX]


def test_free_cursor_rotation_is_unchanged_by_paid_keys() -> None:
    pool = make_pool()

    pool.mark_rate_limited(2, "m")
    assert pool.cursor == 0
    pool.mark_rate_limited(0, "m")
    assert pool.cursor == 1
    pool.mark_auth_failed(1)
    assert pool.cursor == 2
    pool.mark_rate_limited(2, "other")
    # 무료 키 셋 안에서만 돈다. 유료 키(3)로 넘어가지 않는다.
    assert pool.cursor == 0


def test_hold_keeps_the_cursor_and_blocks_only_that_key_and_model() -> None:
    clock = Clock()
    pool = make_pool(clock)

    pool.hold(0, "m", 45)

    assert pool.cursor == 0
    assert pool.is_cooling_down(0, "m")
    assert pool.pick("m") == 1
    assert pool.pick("other") == 0
    status = pool.status()[0]
    # 같은 키로 기다릴 뿐 429 횟수는 세지 않는다. 남은 시간은 쿨다운으로 보인다.
    assert (status.failure_count, status.rate_limited_count) == (0, 0)
    assert status.cooldown_remaining == {"m": 45.0}
    clock.now += 45
    assert pool.pick("m") == 0


def test_hold_never_shortens_a_longer_block() -> None:
    clock = Clock()
    pool = make_pool(clock)
    pool.mark_rate_limited(0, "m")

    pool.hold(0, "m", 5)
    clock.now += 30

    assert pool.is_cooling_down(0, "m")


def test_exhaust_removes_the_key_for_that_model_until_it_resets() -> None:
    clock = Clock()
    pool = make_pool(clock)

    pool.exhaust(0, "m", 3600)

    assert pool.cursor == 0
    assert pool.is_exhausted(0, "m")
    assert not pool.is_cooling_down(0, "m")
    assert pool.pick("m") == 1
    assert pool.pick("other") == 0
    assert pool.any_exhausted("m")
    assert not pool.any_exhausted("m", "paid")
    assert not pool.any_exhausted("other")
    status = pool.status()[0]
    assert status.exhausted_remaining == {"m": 3600.0}
    assert (status.failure_count, status.rate_limited_count, status.last_429_at) == (1, 1, NOW)
    assert pool.status()[1].exhausted_remaining == {}

    clock.now += 3600
    assert not pool.any_exhausted("m")
    assert pool.pick("m") == 0
    assert pool.status()[0].exhausted_remaining == {}


def test_paid_key_is_redacted_and_never_shown() -> None:
    pool = make_pool()

    redacted = pool.redact(f"denied: {PAID} and {FREE[0]}")

    assert PAID not in redacted and FREE[0] not in redacted
    assert pool.status()[PAID_INDEX].last4 == "zzzz"
    assert PAID not in repr(pool) + repr(pool.status())


def test_all_disabled_is_per_tier() -> None:
    pool = make_pool()

    pool.mark_auth_failed(PAID_INDEX)

    assert pool.all_disabled("paid")
    assert not pool.all_disabled()
    for index in range(3):
        pool.mark_auth_failed(index)
    assert pool.all_disabled()


def quota_payload(quota_id: str, retry_delay: str | None) -> dict[str, Any]:
    details: list[dict[str, Any]] = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": quota_id, "quotaValue": "10"}],
        }
    ]
    if retry_delay is not None:
        details.append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
        )
    return {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": details}


Outcome = int | tuple[str, str | None]


class QuotaTransport:
    """(모델, 키 index)별 결과를 정해 둔 전송 계층. 정하지 않은 조합은 정상 응답.

    결과가 (quotaId, retryDelay)면 그 본문의 429, 정수면 그 상태 코드다. 목록이면 호출마다 하나씩
    꺼낸다.
    """

    def __init__(self, outcomes: Mapping[tuple[str, int], Outcome | list[Outcome]]) -> None:
        self.outcomes = {key: value for key, value in outcomes.items()}
        self.calls: list[tuple[str, int]] = []

    async def stream_chat(
        self, *, api_key: str, body: Mapping[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        call = (str(body["model"]), KEYS.index(api_key))
        self.calls.append(call)
        outcome = self.outcomes.get(call)
        if isinstance(outcome, list):
            outcome = outcome.pop(0) if outcome else None
        if isinstance(outcome, int):
            raise TransportError(f"status {outcome}", status=outcome)
        if outcome is not None:
            quota_id, delay = outcome
            raise TransportError(
                "RESOURCE_EXHAUSTED: quota", status=429, payload=quota_payload(quota_id, delay)
            )
        yield {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}

    async def list_models(self, *, api_key: str) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_adapter(
    transport: QuotaTransport,
    *,
    clock: Clock | None = None,
    sleep: Sleeps | None = None,
    paid: bool = True,
) -> GeminiAdapter:
    return GeminiAdapter(
        api_keys=FREE,
        paid_api_keys=[PAID] if paid else [],
        model_fallback=["model-a", "model-b"],
        key_cooldown_sec=60,
        transport=transport,
        sleep=sleep or Sleeps(),
        clock=clock or Clock(),
        now=lambda: NOW,
    )


def request(tier: str = "free") -> ChatRequest:
    return ChatRequest.model_validate(
        {
            "system": "s",
            "messages": [CanonicalMessage(role="user", blocks=[TextBlock(text="hi")])],
            "tier": tier,
        }
    )


async def run(adapter: GeminiAdapter, tier: str = "free") -> list[Any]:
    return [event async for event in adapter.stream(request(tier))]


def test_tier_defaults_to_free_and_is_not_sent() -> None:
    from app.llm.catalog import get_capabilities
    from app.llm.gemini_adapter import build_request_body

    req = request()
    body = build_request_body(request("paid"), model="m", caps=get_capabilities("m"), max_tokens=10)

    assert req.tier == "free"
    assert "tier" not in body


async def test_minute_429_moves_to_the_next_key() -> None:
    transport = QuotaTransport({("model-a", 0): (MINUTE, "45s")})
    sleep = Sleeps()
    adapter = make_adapter(transport, sleep=sleep)

    events = await run(adapter)

    assert transport.calls == [("model-a", 0), ("model-a", 1)]
    end = events[-1]
    assert isinstance(end, MessageEnd)
    assert (end.model, end.key_index) == ("model-a", 1)
    assert sleep.calls == []
    status = adapter.key_status()[0]
    assert status.cooldown_remaining == {"model-a": 60.0}
    assert status.exhausted_remaining == {}


async def test_daily_429_goes_to_the_next_model_without_trying_other_keys() -> None:
    clock = Clock()
    transport = QuotaTransport({("model-a", 0): (DAY, "17437s")})
    adapter = make_adapter(transport, clock=clock)

    first = await run(adapter)
    first_calls = list(transport.calls)
    transport.calls.clear()
    second = await run(adapter)

    assert first_calls == [("model-a", 0), ("model-b", 0)]
    assert isinstance(first[-1], MessageEnd) and first[-1].model == "model-b"
    status = adapter.key_status()[0]
    # retryDelay(17437초)가 짧아도 다음 태평양 자정(23시간 뒤)까지 막는다(M21 검토 2-5).
    assert status.exhausted_remaining == {"model-a": 23 * 3600.0}
    assert status.cooldown_remaining == {}
    # 다음 요청도 다른 키로 model-a를 쓰지 않는다. 하루 총량이 키 수만큼 늘어나기 때문이다.
    assert transport.calls == [("model-b", 0)]
    assert isinstance(second[-1], MessageEnd)

    clock.now += 17437
    transport.calls.clear()
    await run(adapter)
    assert transport.calls == [("model-b", 0)]

    clock.now += 23 * 3600 - 17437
    transport.outcomes.clear()
    transport.calls.clear()
    await run(adapter)
    assert transport.calls == [("model-a", 0)]


async def test_daily_429_without_retry_delay_waits_until_pacific_midnight() -> None:
    transport = QuotaTransport({("model-a", 0): (DAY, None)})
    adapter = make_adapter(transport)

    await run(adapter)

    # NOW는 2026-11-02 09:00 UTC(PST)라 다음 태평양 자정은 2026-11-03 08:00 UTC다.
    assert adapter.key_status()[0].exhausted_remaining == {"model-a": 23 * 3600.0}


async def test_daily_429_on_every_model_fails_as_model_unavailable() -> None:
    transport = QuotaTransport({("model-a", 0): (DAY, "100s"), ("model-b", 0): (DAY, "100s")})
    adapter = make_adapter(transport)

    events = await run(adapter)
    again = await run(adapter)

    assert transport.calls == [("model-a", 0), ("model-b", 0)]
    for result in (events, again):
        error = result[-1]
        assert isinstance(error, ErrorEvent)
        assert (error.code, error.retryable) == ("model_unavailable", True)
        assert "일일 한도" in error.message


async def test_unknown_429_still_rotates_like_before() -> None:
    transport = QuotaTransport({("model-a", 0): 429})
    adapter = make_adapter(transport)

    events = await run(adapter)

    assert transport.calls == [("model-a", 0), ("model-a", 1)]
    assert isinstance(events[-1], MessageEnd)


async def test_free_requests_never_use_the_paid_key() -> None:
    transport = QuotaTransport({("model-a", i): (MINUTE, "10s") for i in range(3)})
    adapter = make_adapter(transport)

    events = await run(adapter)

    assert transport.calls == [("model-a", 0), ("model-a", 1), ("model-a", 2), ("model-b", 0)]
    assert all(index != PAID_INDEX for _, index in transport.calls)
    assert isinstance(events[-1], MessageEnd)


async def test_paid_request_waits_and_retries_the_same_key() -> None:
    transport = QuotaTransport({("model-a", PAID_INDEX): [(MINUTE, "45s"), (MINUTE, None)]})
    sleep = Sleeps()
    adapter = make_adapter(transport, sleep=sleep)

    events = await run(adapter, "paid")

    assert transport.calls == [("model-a", PAID_INDEX)] * 3
    assert sleep.calls == [45.0, 30.0]
    end = events[-1]
    assert isinstance(end, MessageEnd)
    assert (end.model, end.key_index) == ("model-a", PAID_INDEX)
    status = adapter.key_status()
    # 유료 키 429는 같은 키로 기다리기라 커서를 옮기는 쿨다운이 아니다.
    assert status[PAID_INDEX].rate_limited_count == 0
    assert status[PAID_INDEX].failure_count == 2
    assert adapter.key_pool.cursor == 0


async def test_paid_request_never_falls_back_to_free_keys() -> None:
    transport = QuotaTransport(
        {("model-a", PAID_INDEX): (MINUTE, "600s"), ("model-b", PAID_INDEX): 503}
    )
    sleep = Sleeps()
    adapter = make_adapter(transport, sleep=sleep)

    events = await run(adapter, "paid")

    # 세 번 기다린 뒤 다음 모델로 간다. 기다림은 상한(60초)까지만이다. 5xx도 같은 키로 30초씩
    # 세 번 기다린다(M21 검토 2-4).
    assert transport.calls == [("model-a", PAID_INDEX)] * 4 + [("model-b", PAID_INDEX)] * 4
    assert sleep.calls == [60.0, 60.0, 60.0, 30.0, 30.0, 30.0]
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "model_unavailable"


async def test_paid_server_error_waits_on_the_same_key_like_a_429() -> None:
    transport = QuotaTransport({("model-a", PAID_INDEX): [503, 500]})
    sleep = Sleeps()
    adapter = make_adapter(transport, sleep=sleep)

    events = await run(adapter, "paid")

    assert transport.calls == [("model-a", PAID_INDEX)] * 3
    assert sleep.calls == [30.0, 30.0]
    assert isinstance(events[-1], MessageEnd) and events[-1].key_index == PAID_INDEX
    # 무료 요청의 5xx는 지금처럼 2초 뒤 한 번 더, 그다음 모델이다.
    free_transport = QuotaTransport({("model-a", 0): [503, 503]})
    free_sleep = Sleeps()
    await run(make_adapter(free_transport, sleep=free_sleep))
    assert free_transport.calls == [("model-a", 0), ("model-a", 0), ("model-b", 0)]
    assert free_sleep.calls == [2.0]


async def test_concurrent_paid_request_waits_for_the_held_key() -> None:
    # 요청 A가 429로 유료 키를 hold해 둔 동안 온 요청 B는 바로 다음 모델로 가지 않고 기다렸다가
    # 같은 키로 보낸다(M21 검토 2-3).
    clock = Clock()

    async def sleep(seconds: float) -> None:
        clock.now += seconds

    transport = QuotaTransport({("model-a", PAID_INDEX): [(MINUTE, "45s")]})
    adapter = make_adapter(transport, clock=clock, sleep=cast(Sleeps, sleep))
    adapter.key_pool.hold(PAID_INDEX, "model-a", 45)

    events = await run(adapter, "paid")

    assert clock.now == pytest.approx(1000 + 45.1 + 45)
    assert transport.calls == [("model-a", PAID_INDEX)] * 2
    assert isinstance(events[-1], MessageEnd) and events[-1].model == "model-a"


async def test_concurrent_paid_request_gives_up_after_three_waits() -> None:
    clock = Clock()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        # 다른 요청이 그동안 계속 hold를 새로 건다.
        adapter.key_pool.hold(PAID_INDEX, "model-a", 600)

    transport = QuotaTransport({})
    adapter = make_adapter(transport, clock=clock, sleep=cast(Sleeps, sleep))
    adapter.key_pool.hold(PAID_INDEX, "model-a", 600)

    events = await run(adapter, "paid")

    assert len(waits) == 3 and all(wait == 60.0 for wait in waits)
    assert transport.calls == [("model-b", PAID_INDEX)]
    assert isinstance(events[-1], MessageEnd) and events[-1].model == "model-b"


async def test_paid_daily_429_moves_to_the_next_model_on_the_paid_key() -> None:
    transport = QuotaTransport({("model-a", PAID_INDEX): (DAY, "3600s")})
    sleep = Sleeps()
    adapter = make_adapter(transport, sleep=sleep)

    events = await run(adapter, "paid")
    free = await run(adapter)

    assert transport.calls == [("model-a", PAID_INDEX), ("model-b", PAID_INDEX), ("model-a", 0)]
    assert sleep.calls == []
    assert isinstance(events[-1], MessageEnd) and events[-1].key_index == PAID_INDEX
    # 유료 키의 일일 한도는 무료 요청과 상관이 없다.
    assert isinstance(free[-1], MessageEnd) and free[-1].model == "model-a"


async def test_paid_request_without_a_paid_key_fails_without_calls() -> None:
    transport = QuotaTransport({})
    adapter = make_adapter(transport, paid=False)

    events = await run(adapter, "paid")

    assert transport.calls == []
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert (error.code, error.retryable) == ("model_auth", False)
    assert "GEMINI_PAID_API_KEY" in error.message


async def test_paid_auth_error_ends_as_model_auth() -> None:
    transport = QuotaTransport({("model-a", PAID_INDEX): 401})
    adapter = make_adapter(transport)

    events = await run(adapter, "paid")

    assert transport.calls == [("model-a", PAID_INDEX)]
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.code == "model_auth"
    assert not adapter.key_pool.all_disabled()


def test_adapter_from_settings_registers_the_paid_key() -> None:
    settings = Settings.model_validate(
        {"gemini_api_keys": ",".join(FREE), "gemini_paid_api_key": SecretStr(PAID)}
    )
    without = Settings.model_validate({"gemini_api_keys": ",".join(FREE)})

    adapter = GeminiAdapter.from_settings(settings)

    assert adapter.key_pool.count("paid") == 1
    assert adapter.key_pool.secret(PAID_INDEX) == PAID
    assert GeminiAdapter.from_settings(without).key_pool.count("paid") == 0
