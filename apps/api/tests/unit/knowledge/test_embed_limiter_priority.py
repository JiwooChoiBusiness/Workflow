"""리미터의 항목 수 버킷: 분당 90건, interactive 예약 10건, bulk 20초 조각(설계 6.6, K13)."""

from collections.abc import Sequence
from typing import Any

import pytest

from app.llm.embeddings import EmbedLimiter, TierLimits

Grant = tuple[float, int]


def limiter(fake_time: Any, **options: Any) -> EmbedLimiter:
    return EmbedLimiter(clock=fake_time.clock, sleep=fake_time.sleep, **options)


def max_in_window(grants: Sequence[Grant], width: float) -> int:
    return max(sum(n for at, n in grants if start <= at < start + width) for start, _ in grants)


async def drain_bulk(limiter: EmbedLimiter, fake_time: Any, times: int) -> list[Grant]:
    # 인제스트처럼 묶음 하나가 끝나면 바로 다음 묶음을 달라고 한다.
    grants: list[Grant] = []
    for _ in range(times):
        count = await limiter.acquire("free", "bulk", [100] * 100)
        grants.append((fake_time.now, count))
    return grants


async def test_bulk_spreads_its_share_over_three_20_second_slices(fake_time: Any) -> None:
    bulk = limiter(fake_time)

    grants = await drain_bulk(bulk, fake_time, 9)

    # 공용분 80건을 조각마다 27·27·26으로 보낸다. 한 번에 80개를 쏘고 60초를 비우지 않는다.
    assert [count for _, count in grants[:6]] == [27, 27, 26, 27, 27, 26]
    assert [round(at - 1000) for at, _ in grants[:6]] == [0, 20, 40, 60, 80, 100]
    assert max_in_window(grants, 60) <= 80
    assert max_in_window(grants, 20) <= 27
    assert bulk.batch_items("free", "bulk") == 27


async def test_interactive_is_not_held_back_by_bulk(fake_time: Any) -> None:
    shared = limiter(fake_time)
    await drain_bulk(shared, fake_time, 3)
    assert shared.usage("free")[0] == 80
    waited = len(fake_time.sleeps)

    for _ in range(10):
        assert await shared.acquire("free", "interactive", [10]) == 1

    # 예약분 10건은 bulk가 공용분을 다 써도 바로 나간다. 11번째는 분당 90건에 걸린다.
    assert len(fake_time.sleeps) == waited
    started = fake_time.now
    assert await shared.acquire("free", "interactive", [10]) == 1
    assert fake_time.now == pytest.approx(1060, abs=1e-3)
    assert fake_time.now > started


async def test_interactive_borrows_the_shared_part_when_bulk_is_idle(fake_time: Any) -> None:
    alone = limiter(fake_time)

    for _ in range(90):
        assert await alone.acquire("free", "interactive", [1]) == 1
    assert fake_time.sleeps == []

    await alone.acquire("free", "interactive", [1])
    assert fake_time.now == pytest.approx(1060, abs=1e-3)


async def test_interactive_overflow_shrinks_the_bulk_share(fake_time: Any) -> None:
    mixed = limiter(fake_time)
    for _ in range(15):
        await mixed.acquire("free", "interactive", [1])

    grants = await drain_bulk(mixed, fake_time, 3)

    # 예약분을 5건 넘겼으므로 bulk의 분당 몫은 80 - 5 = 75다.
    assert [count for _, count in grants] == [27, 27, 21]


async def test_retry_waits_for_the_whole_batch(fake_time: Any) -> None:
    bulk = limiter(fake_time)
    assert await bulk.acquire("free", "bulk", [1] * 20) == 20

    # 남은 조각 몫은 7건이다. 다시 보내는 20건은 나누지 않고 다음 조각까지 기다린다.
    assert await bulk.acquire("free", "bulk", [1] * 20, partial=False) == 20
    assert fake_time.now == pytest.approx(1020, abs=1e-3)


async def test_oversized_retry_goes_out_alone_in_an_empty_slice(fake_time: Any) -> None:
    bulk = limiter(fake_time)

    assert await bulk.acquire("free", "bulk", [1] * 40, partial=False) == 40
    assert fake_time.sleeps == []


async def test_free_tier_is_capped_at_the_measured_quota(fake_time: Any) -> None:
    capped = limiter(fake_time, free=TierLimits(rpm=200, reserve=10))
    uncapped = limiter(fake_time, free=TierLimits(rpm=6000, reserve=10), free_rpm_cap=None)
    paid = limiter(fake_time, paid=TierLimits(rpm=300, reserve=10))

    assert capped.limits("free").rpm == 100
    assert uncapped.limits("free").rpm == 6000
    assert uncapped.batch_items("free", "bulk") == 100
    assert uncapped.batch_items("free", "interactive") == 100
    assert paid.limits("paid").rpm == 300


async def test_tiers_have_separate_buckets(fake_time: Any) -> None:
    both = limiter(fake_time)
    for _ in range(90):
        await both.acquire("free", "interactive", [1])

    assert await both.acquire("paid", "interactive", [1]) == 1
    assert fake_time.sleeps == []


@pytest.mark.parametrize(("rpm", "reserve"), [(0, 0), (10, 10), (10, -1)])
def test_invalid_limits_are_rejected(rpm: int, reserve: int) -> None:
    with pytest.raises(ValueError):
        TierLimits(rpm=rpm, reserve=reserve)
