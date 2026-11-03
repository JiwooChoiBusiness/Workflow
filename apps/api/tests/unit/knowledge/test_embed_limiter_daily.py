"""리미터의 등급별 하루 몫(KB_EMBED_RPD). 태평양 자정에 풀리고 index.db에 남겨 재기동해도 잇는다
(M21 검토 1-3, 3단계 설계 Z2).
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.llm.embeddings import DailyLimitReached, EmbedLimiter, TierLimits
from app.llm.errors import seconds_until_daily_reset

# 2026-11-02 03:00 UTC(PST)다. 다음 태평양 자정은 08:00 UTC.
START = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
LIMITS = TierLimits(rpm=90, reserve=10, rpd=30)


class Wall:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.saves = 0

    async def load(self, key: str) -> str | None:
        return self.values.get(key)

    async def save(self, key: str, value: str) -> None:
        self.saves += 1
        self.values[key] = value


def limiter(fake_time: Any, wall: Wall, store: MemoryStore | None = None) -> EmbedLimiter:
    return EmbedLimiter(
        free=LIMITS,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        now=wall,
        store=store,
    )


async def test_bulk_stops_at_its_daily_share_and_leaves_the_reserve(fake_time: Any) -> None:
    wall = Wall()
    daily = limiter(fake_time, wall)
    # 하루 30건 중 분당 예약 비율(10/90)만큼인 4건은 interactive 몫이다.
    assert LIMITS.daily_reserve == 4

    assert await daily.acquire("free", "bulk", [1] * 20) == 20
    assert await daily.acquire("free", "bulk", [1] * 20) == 6
    with pytest.raises(DailyLimitReached) as stopped:
        await daily.acquire("free", "bulk", [1])

    assert stopped.value.retry_after == pytest.approx(5 * 3600)
    assert daily.daily_remaining("free", "bulk") == 0
    assert daily.daily_remaining("free", "interactive") == 4
    for _ in range(4):
        assert await daily.acquire("free", "interactive", [1]) == 1
    with pytest.raises(DailyLimitReached):
        await daily.acquire("free", "interactive", [1])
    usage = daily.daily_usage("free")
    assert usage is not None
    assert (usage.limit, usage.bulk, usage.interactive, usage.used) == (30, 26, 4, 30)
    # 하루 몫을 세지 않는 등급(paid 기본)은 걸리지 않는다.
    assert daily.daily_usage("paid") is None
    assert await daily.acquire("paid", "bulk", [1] * 5) == 5


async def test_a_retry_that_does_not_fit_is_refused_whole(fake_time: Any) -> None:
    wall = Wall()
    daily = limiter(fake_time, wall)
    await daily.acquire("free", "bulk", [1] * 24)

    with pytest.raises(DailyLimitReached):
        await daily.acquire("free", "bulk", [1] * 5, partial=False)
    assert await daily.acquire("free", "bulk", [1] * 2, partial=False) == 2


async def test_the_share_comes_back_at_pacific_midnight(fake_time: Any) -> None:
    wall = Wall()
    daily = limiter(fake_time, wall)
    await daily.acquire("free", "bulk", [1] * 26)
    with pytest.raises(DailyLimitReached):
        await daily.acquire("free", "bulk", [1])

    wall.now = START + timedelta(seconds=seconds_until_daily_reset(START))
    fake_time.now += 3600

    assert daily.daily_remaining("free", "bulk") == 26
    assert await daily.acquire("free", "bulk", [1] * 3) == 3


async def test_usage_survives_a_restart_on_the_same_day(fake_time: Any) -> None:
    wall = Wall()
    store = MemoryStore()
    first = limiter(fake_time, wall, store)
    await first.acquire("free", "bulk", [1] * 20)
    await first.acquire("free", "interactive", [1])
    await first.flush()

    assert store.saves >= 1
    again = limiter(fake_time, wall, store)
    await again.load()
    usage = again.daily_usage("free")
    assert usage is not None and (usage.bulk, usage.interactive) == (20, 1)
    assert await again.acquire("free", "bulk", [1] * 20) == 6

    # 날짜가 지난 기록은 버린다.
    wall.now = START + timedelta(days=1)
    later = limiter(fake_time, wall, store)
    await later.load()
    usage = later.daily_usage("free")
    assert usage is not None and usage.used == 0


async def test_a_failing_store_does_not_stop_counting(fake_time: Any) -> None:
    class Broken(MemoryStore):
        async def save(self, key: str, value: str) -> None:
            raise RuntimeError("index.db가 닫혔다")

    wall = Wall()
    daily = limiter(fake_time, wall, Broken())

    assert await daily.acquire("free", "bulk", [1] * 10) == 10
    await daily.flush()
    assert daily.daily_remaining("free", "bulk") == 16
