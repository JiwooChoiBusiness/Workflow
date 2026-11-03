"""리미터의 토큰 버킷(KB_EMBED_TPM). 기본은 꺼져 있고, 켜면 항목 수보다 먼저 걸릴 수 있다(6.6)."""

from typing import Any

import pytest

from app.llm.embeddings import EmbedLimiter, TierLimits

# rpm 90·예약 10이면 예약 토큰은 9000 × 10 / 90 = 1000, bulk 몫은 8000, 조각마다 2667이다.
TPM = TierLimits(rpm=90, tpm=9000, reserve=10)


def limiter(fake_time: Any, **options: Any) -> EmbedLimiter:
    return EmbedLimiter(clock=fake_time.clock, sleep=fake_time.sleep, **options)


async def test_token_bucket_binds_before_the_item_bucket(fake_time: Any) -> None:
    bulk = limiter(fake_time, free=TPM)

    first = await bulk.acquire("free", "bulk", [1000] * 27)
    second = await bulk.acquire("free", "bulk", [1000] * 27)

    # 항목 몫은 27건이지만 토큰 몫(2667)이 먼저 걸려 2건씩 나간다.
    assert (first, second) == (2, 2)
    assert fake_time.now == pytest.approx(1020, abs=1e-3)


async def test_bulk_tokens_stay_within_the_shared_part_per_minute(fake_time: Any) -> None:
    bulk = limiter(fake_time, free=TPM)
    grants: list[tuple[float, int]] = []
    while fake_time.now < 1180:
        count = await bulk.acquire("free", "bulk", [500] * 50)
        grants.append((fake_time.now, count * 500))

    for start, _ in grants:
        assert sum(tokens for at, tokens in grants if start <= at < start + 60) <= 8000
        assert sum(tokens for at, tokens in grants if start <= at < start + 20) <= 2667


async def test_interactive_can_use_the_whole_token_budget(fake_time: Any) -> None:
    alone = limiter(fake_time, free=TPM)

    for _ in range(9):
        assert await alone.acquire("free", "interactive", [1000]) == 1
    assert fake_time.sleeps == []

    await alone.acquire("free", "interactive", [1000])
    assert fake_time.now == pytest.approx(1060, abs=1e-3)


async def test_token_bucket_is_off_by_default(fake_time: Any) -> None:
    bulk = limiter(fake_time)

    assert await bulk.acquire("free", "bulk", [100_000] * 27) == 27


async def test_an_item_larger_than_a_slice_still_goes_out(fake_time: Any) -> None:
    # 조각 몫(2667)보다 큰 청크 하나는 조각이 비어 있을 때 혼자 나간다(영영 기다리지 않는다).
    bulk = limiter(fake_time, free=TPM)

    assert await bulk.acquire("free", "bulk", [5000, 10]) == 1
    assert await bulk.acquire("free", "bulk", [5000]) == 1
    assert fake_time.now == pytest.approx(1020, abs=1e-3)


async def test_paid_tier_has_its_own_token_bucket(fake_time: Any) -> None:
    both = limiter(fake_time, free=TPM, paid=TierLimits(rpm=90, tpm=900_000, reserve=10))
    await both.acquire("free", "bulk", [1000] * 27)

    assert await both.acquire("paid", "bulk", [1000] * 27) == 27
    assert both.usage("paid") == (27, 27_000)
