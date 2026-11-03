"""EmbeddingClient의 키 선택과 429·5xx 처리(설계 6.6, Z2 (b)). 가짜 전송과 진짜 KeyPool로 돈다."""

import math
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from app.knowledge.tokens import estimate_tokens
from app.llm.embeddings import (
    DOCUMENT_PREFIX,
    ERR_PAID_KEY_MISSING,
    ERR_RATE_LIMITED,
    ERR_RATE_LIMITED_DAILY,
    QUERY_PREFIX,
    EmbedCache,
    EmbeddedDocuments,
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    EmbedLimiter,
    TierLimits,
    pack_vector,
    unpack_vector,
)
from app.llm.errors import TransportError, seconds_until_daily_reset
from app.llm.fake_adapter import (
    FakeEmbedder,
    FakeEmbedFault,
    fake_embedding,
    fake_rate_limit_error,
    fake_server_error,
)
from app.llm.key_pool import KeyPool
from app.llm.types import ERR_MODEL_AUTH, ERR_MODEL_REQUEST_INVALID, ERR_MODEL_UNAVAILABLE

KEYS = ["test-key-0000-aaaa", "test-key-1111-bbbb", "test-key-2222-cccc"]
PAID_KEY = "test-paid-key-9999-zzzz"
ALL_KEYS = [*KEYS, PAID_KEY]
MODEL = "gemini-embedding-2"
CONFIG = EmbeddingConfig()
NOW = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)


@dataclass
class Harness:
    client: EmbeddingClient
    pool: KeyPool
    limiter: EmbedLimiter
    embedder: FakeEmbedder

    @property
    def keys(self) -> list[int]:
        return [ALL_KEYS.index(call.api_key) for call in self.embedder.calls]

    @property
    def sizes(self) -> list[int]:
        return [len(call.inputs) for call in self.embedder.calls]


def harness(
    fake_time: Any,
    faults: Iterable[FakeEmbedFault] = (),
    *,
    keys: Sequence[str] = KEYS,
    paid: Sequence[str] = (),
    cache: EmbedCache | None = None,
    free: TierLimits | None = None,
    free_rpm_cap: int | None = 100,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    query_cache_size: int = 256,
) -> Harness:
    pool = KeyPool(keys, cooldown_sec=60, clock=fake_time.clock, paid_keys=paid)
    limiter = EmbedLimiter(
        free=free, free_rpm_cap=free_rpm_cap, clock=fake_time.clock, sleep=fake_time.sleep
    )
    embedder = FakeEmbedder(faults)
    client = EmbeddingClient(
        embedder,
        pool,
        limiter,
        cache,
        estimate_tokens=estimate_tokens,
        sleep=sleep or fake_time.sleep,
        now=lambda: NOW,
        query_cache_size=query_cache_size,
    )
    return Harness(client, pool, limiter, embedder)


async def failure_of(call: Awaitable[EmbeddedDocuments]) -> EmbeddingError:
    # embed_documents는 예외 대신 failure를 채워 돌려준다(받은 벡터를 버리지 않으려고).
    result = await call
    assert result.failure is not None
    return result.failure


def unknown_429() -> TransportError:
    # quotaId도 retryDelay도 없는 429.
    return TransportError("RESOURCE_EXHAUSTED: quota", status=429, payload={"code": 429})


def http_error(status: int, message: str = "error") -> TransportError:
    return TransportError(message, status=status, payload={"code": status, "message": message})


async def test_documents_get_the_document_prefix_and_unit_vectors(fake_time: Any) -> None:
    run = harness(fake_time)

    result = await run.client.embed_documents(["회의록 작성", "출장비 정산"], CONFIG)

    call = run.embedder.calls[0]
    assert call.inputs == (DOCUMENT_PREFIX + "회의록 작성", DOCUMENT_PREFIX + "출장비 정산")
    assert (call.model, call.dimensions) == (MODEL, 768)
    assert result.errors == {} and result.cached == 0
    for text, blob in zip(call.inputs, result.vectors, strict=True):
        assert blob == pack_vector(fake_embedding(text))
        assert blob is not None and len(blob) == 768 * 4
        assert math.fsum(value * value for value in unpack_vector(blob)) == pytest.approx(1.0)


async def test_the_same_text_is_sent_once(fake_time: Any) -> None:
    run = harness(fake_time)

    result = await run.client.embed_documents(["가", "나", "가"], CONFIG)

    assert run.sizes == [2]
    assert result.vectors[0] == result.vectors[2] != result.vectors[1]


async def test_prefix_rule_none_sends_the_text_as_is(fake_time: Any) -> None:
    run = harness(fake_time)

    await run.client.embed_documents(["회의록"], EmbeddingConfig(prefix="none"))
    await run.client.embed_query("회의록", EmbeddingConfig(prefix="none"))

    assert [call.inputs for call in run.embedder.calls] == [("회의록",), ("회의록",)]


async def test_query_uses_the_query_prefix_and_is_remembered(fake_time: Any) -> None:
    run = harness(fake_time, query_cache_size=2)

    first = await run.client.embed_query("연차 며칠?", CONFIG)
    again = await run.client.embed_query("연차 며칠?", CONFIG)
    used = run.limiter.usage("free")
    await run.client.embed_query("병가", CONFIG)
    await run.client.embed_query("출장", CONFIG)
    await run.client.embed_query("연차 며칠?", CONFIG)

    assert first == again
    assert run.embedder.calls[0].inputs == (QUERY_PREFIX + "연차 며칠?",)
    assert used == (1, estimate_tokens(QUERY_PREFIX + "연차 며칠?"))
    # LRU 2개라 세 질의 뒤의 첫 질의는 다시 부른다.
    assert len(run.embedder.calls) == 4


async def test_bulk_batches_follow_the_limiter_and_the_api_cap(fake_time: Any) -> None:
    limited = harness(fake_time)
    texts = [f"문단 {index}" for index in range(60)]

    await limited.client.embed_documents(texts, CONFIG)

    assert limited.sizes == [27, 27, 6]
    assert fake_time.now == pytest.approx(1040, abs=1e-3)

    unlimited = harness(fake_time, free=TierLimits(rpm=6000, reserve=10), free_rpm_cap=None)
    await unlimited.client.embed_documents([f"문장 {index}" for index in range(250)], CONFIG)
    assert unlimited.sizes == [100, 100, 50]


async def test_bulk_minute_429_waits_on_the_same_key(fake_time: Any) -> None:
    held: list[bool] = []

    async def sleep(seconds: float) -> None:
        held.append(run.pool.is_cooling_down(0, MODEL))
        await fake_time.sleep(seconds)

    run = harness(fake_time, [fake_rate_limit_error("minute", retry_after=45)], sleep=sleep)

    result = await run.client.embed_documents(["회의록"], CONFIG)

    assert result.vectors[0] is not None
    assert run.keys == [0, 0]
    assert fake_time.sleeps == [45]
    # 기다리는 동안 그 키를 막아 두되(hold) 커서는 옮기지 않는다.
    assert held == [True]
    assert run.pool.cursor == 0


async def test_bulk_minute_429_three_times_waits_once_more_then_fails(fake_time: Any) -> None:
    recovers = harness(fake_time, [fake_rate_limit_error("minute", retry_after=45)] * 3)
    await recovers.client.embed_documents(["회의록"], CONFIG)
    assert recovers.keys == [0, 0, 0, 0]

    fails = harness(fake_time, [fake_rate_limit_error("minute", retry_after=45)] * 4)
    failure = await failure_of(fails.client.embed_documents(["회의록"], CONFIG))

    assert failure.code == ERR_RATE_LIMITED and failure.retryable
    assert fails.keys == [0, 0, 0, 0]
    assert fake_time.sleeps == [45] * 6


async def test_bulk_unknown_429_waits_once(fake_time: Any) -> None:
    run = harness(fake_time, [unknown_429(), unknown_429()])

    failure = await failure_of(run.client.embed_documents(["회의록"], CONFIG))

    assert failure.code == ERR_RATE_LIMITED
    assert run.keys == [0, 0]
    assert fake_time.sleeps == [60]


async def test_interactive_minute_429_moves_to_the_next_key(fake_time: Any) -> None:
    run = harness(fake_time, [fake_rate_limit_error("minute", retry_after=45)])

    await run.client.embed_query("연차", CONFIG)

    assert run.keys == [0, 1]
    assert fake_time.sleeps == []
    assert run.pool.cursor == 1
    assert run.pool.is_cooling_down(0, MODEL)


async def test_interactive_waits_for_the_shortest_retry_when_every_key_is_limited(
    fake_time: Any,
) -> None:
    faults = [fake_rate_limit_error("minute", retry_after=wait) for wait in (50, 20, 40)]
    recovers = harness(fake_time, faults)

    await recovers.client.embed_query("연차", CONFIG)

    assert recovers.keys == [0, 1, 2, 1]
    assert fake_time.sleeps == [20]

    fails = harness(fake_time, [*faults, fake_rate_limit_error("minute", retry_after=20)])
    with pytest.raises(EmbeddingError) as raised:
        await fails.client.embed_single("청크", CONFIG)
    assert raised.value.code == ERR_RATE_LIMITED
    assert fails.keys == [0, 1, 2, 1]


@pytest.mark.parametrize("priority", ["bulk", "interactive"])
async def test_daily_429_stops_without_rotating_keys(fake_time: Any, priority: str) -> None:
    run = harness(fake_time, [fake_rate_limit_error("day", retry_after=17437)])
    # NOW는 2026-11-02 03:00 UTC(PST)라 다음 태평양 자정까지 5시간이다.
    until_midnight = seconds_until_daily_reset(NOW)
    assert until_midnight == 5 * 3600

    failure = await failure_of(
        run.client.embed_documents(["회의록"], CONFIG, priority=priority)  # type: ignore[arg-type]
    )

    assert failure.code == ERR_RATE_LIMITED_DAILY
    # retryDelay(17437초)가 자정보다 짧아도 자정까지 막는다(M21 검토 2-5).
    assert failure.retryable and failure.retry_after == until_midnight
    assert run.pool.is_exhausted(0, MODEL)
    assert run.client.daily_exhausted(MODEL) == pytest.approx(until_midnight)
    # 다른 키가 남아 있어도 보내지 않는다. 하루 총량이 키 수만큼 늘어나기 때문이다.
    again = await failure_of(run.client.embed_documents(["다른 글"], CONFIG))
    assert again.code == ERR_RATE_LIMITED_DAILY
    with pytest.raises(EmbeddingError) as query:
        await run.client.embed_query("질의", CONFIG)
    assert query.value.code == ERR_RATE_LIMITED_DAILY
    assert run.keys == [0]


async def test_daily_429_without_retry_delay_lasts_until_pacific_midnight(fake_time: Any) -> None:
    run = harness(fake_time, [fake_rate_limit_error("day", retry_after=None)])

    failure = await failure_of(run.client.embed_documents(["회의록"], CONFIG))

    assert failure.retry_after == pytest.approx(seconds_until_daily_reset(NOW))


async def test_paid_tier_waits_on_its_single_key(fake_time: Any) -> None:
    faults = [fake_rate_limit_error("minute", retry_after=None), fake_server_error(503)]
    run = harness(fake_time, faults, paid=[PAID_KEY])

    result = await run.client.embed_documents(["기밀 문서"], CONFIG, tier="paid")

    assert result.vectors[0] is not None
    assert run.keys == [3, 3, 3]
    assert fake_time.sleeps == [30, 30]
    # 유료 호출은 무료 키의 커서를 건드리지 않는다.
    assert run.pool.cursor == 0


async def test_paid_tier_gives_up_after_three_waits_and_never_uses_free_keys(
    fake_time: Any,
) -> None:
    run = harness(fake_time, [fake_rate_limit_error("minute", retry_after=10)] * 4, paid=[PAID_KEY])

    with pytest.raises(EmbeddingError) as raised:
        await run.client.embed_query("질의", CONFIG, tier="paid")

    assert raised.value.code == ERR_RATE_LIMITED
    assert run.keys == [3, 3, 3, 3]


async def test_paid_tier_without_a_paid_key_is_refused(fake_time: Any) -> None:
    run = harness(fake_time)

    failure = await failure_of(run.client.embed_documents(["문서"], CONFIG, tier="paid"))

    assert failure.code == ERR_PAID_KEY_MISSING
    assert run.embedder.calls == []


async def test_server_errors_retry_the_same_key_then_another(fake_time: Any) -> None:
    recovers = harness(fake_time, [fake_server_error(503), fake_server_error(500)])
    await recovers.client.embed_documents(["회의록"], CONFIG)
    assert recovers.keys == [0, 0, 1]
    assert fake_time.sleeps == [2.0]

    fails = harness(fake_time, [fake_server_error(503)] * 3)
    failure = await failure_of(fails.client.embed_documents(["회의록"], CONFIG))
    assert failure.code == ERR_MODEL_UNAVAILABLE and failure.retryable
    assert fails.keys == [0, 0, 1]

    alone = harness(fake_time, [fake_server_error(503)] * 2, keys=KEYS[:1])
    await failure_of(alone.client.embed_documents(["회의록"], CONFIG))
    assert alone.keys == [0, 0]


async def test_rejected_keys_are_skipped_until_none_is_left(fake_time: Any) -> None:
    recovers = harness(fake_time, [http_error(401)])
    await recovers.client.embed_documents(["회의록"], CONFIG)
    assert recovers.keys == [0, 1]
    assert recovers.pool.is_disabled(0)

    fails = harness(fake_time, [http_error(403)] * 3)
    failure = await failure_of(fails.client.embed_documents(["회의록"], CONFIG))
    assert failure.code == ERR_MODEL_AUTH and not failure.retryable

    empty = harness(fake_time, keys=[])
    with pytest.raises(EmbeddingError) as missing:
        await empty.client.embed_query("질의", CONFIG)
    assert missing.value.code == ERR_MODEL_AUTH


async def test_other_client_errors_are_not_split(fake_time: Any) -> None:
    run = harness(fake_time, [http_error(404, "model not found")])

    failure = await failure_of(run.client.embed_documents(["가", "나"], CONFIG))

    assert failure.code == ERR_MODEL_REQUEST_INVALID and failure.status == 404
    assert run.sizes == [2]


async def test_vectors_of_the_wrong_size_are_rejected(fake_time: Any) -> None:
    class ShortVectors(FakeEmbedder):
        async def embed(
            self, *, api_key: str, model: str, inputs: Sequence[str], dimensions: int | None
        ) -> list[list[float]]:
            return [[1.0, 0.0, 0.0] for _ in inputs]

    run = harness(fake_time)
    client = EmbeddingClient(
        ShortVectors(),
        run.pool,
        run.limiter,
        None,
        estimate_tokens=estimate_tokens,
        sleep=fake_time.sleep,
    )

    failure = await failure_of(client.embed_documents(["회의록"], CONFIG))
    assert failure.code == ERR_MODEL_REQUEST_INVALID


async def test_error_messages_do_not_carry_key_values(fake_time: Any) -> None:
    leak = TransportError(f"bad key {KEYS[0]} in url ?key={KEYS[0]}", status=404)
    run = harness(fake_time, [leak])

    failure = await failure_of(run.client.embed_documents(["회의록"], CONFIG))

    assert KEYS[0] not in failure.message
    assert KEYS[0] not in str(failure)


async def test_a_failure_midway_keeps_the_vectors_already_received(fake_time: Any) -> None:
    # 27개씩 보내다 세 번째 묶음에서 일일 429가 나도 앞 두 묶음(이미 한도에서 센 호출)은 남는다
    # (M21 검토 1-2).
    run = harness(fake_time, [None, None, fake_rate_limit_error("day", retry_after=None)])
    texts = [f"문단 {index}" for index in range(60)]

    result = await run.client.embed_documents(texts, CONFIG)

    assert run.sizes == [27, 27, 6]
    assert result.failure is not None and result.failure.code == ERR_RATE_LIMITED_DAILY
    assert all(vector is not None for vector in result.vectors[:54])
    assert result.vectors[54:] == [None] * 6
    assert result.errors == {}
    assert result.api_tokens == sum(estimate_tokens(DOCUMENT_PREFIX + text) for text in texts[:54])


async def test_a_failure_after_a_400_split_keeps_the_left_half(fake_time: Any) -> None:
    # 400으로 반을 나눈 뒤 오른쪽 반이 일일 429면 왼쪽 반의 벡터는 살린다.
    faults: list[FakeEmbedFault] = [
        fake_server_error(400),
        None,
        fake_rate_limit_error("day", retry_after=None),
    ]
    run = harness(fake_time, faults)

    result = await run.client.embed_documents(["가", "나", "다", "라"], CONFIG)

    assert run.sizes == [4, 2, 2]
    assert result.failure is not None and result.failure.code == ERR_RATE_LIMITED_DAILY
    assert result.vectors[0] is not None and result.vectors[1] is not None
    assert result.vectors[2:] == [None, None]


async def test_embeddings_stay_on_the_last_key_when_chat_moves_the_cursor(
    fake_time: Any,
) -> None:
    # 채팅의 분당 429가 공용 커서를 옮겨도 임베딩은 마지막으로 성공한 키에 머문다(M21 검토 1-3).
    run = harness(fake_time)
    await run.client.embed_documents(["첫 글"], CONFIG)

    run.pool.mark_rate_limited(0, "gemini-3.8-flash")
    assert run.pool.cursor == 1
    await run.client.embed_documents(["둘째 글"], CONFIG)

    assert run.keys == [0, 0]


async def test_chat_429s_do_not_raise_the_daily_total(fake_time: Any) -> None:
    # 키가 셋이어도 하루 몫(rpd)이 프로젝트 총량을 묶는다. 커서가 옮겨 다녀도 늘지 않는다.
    run = harness(fake_time, free=TierLimits(rpm=90, reserve=10, rpd=20))
    first = await run.client.embed_documents([f"글 {index}" for index in range(10)], CONFIG)
    for index in range(3):
        run.pool.mark_rate_limited(index, "gemini-3.8-flash")
        run.pool.mark_rate_limited(index, "gemini-3.6-flash")
    second = await run.client.embed_documents([f"뒤 {index}" for index in range(30)], CONFIG)

    assert first.failure is None
    # bulk 몫은 rpd 20에서 interactive 예약 3을 뺀 17이다.
    assert run.embedder.embedded_items == 17
    assert second.failure is not None and second.failure.code == ERR_RATE_LIMITED_DAILY
    assert sum(vector is not None for vector in second.vectors) == 7
    usage = run.limiter.daily_usage("free")
    assert usage is not None and usage.used == 17
    # 예약분이 남아 있어 질의 임베딩은 된다.
    await run.client.embed_query("질의", CONFIG)
    assert run.embedder.embedded_items == 18
