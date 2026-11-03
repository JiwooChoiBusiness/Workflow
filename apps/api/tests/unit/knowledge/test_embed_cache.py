"""index.db embed_cache와 캐시 적중 시 API·리미터를 쓰지 않는 것(설계 6.6, K9)."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from app.knowledge.index_db import IndexDb
from app.knowledge.tokens import estimate_tokens
from app.llm.embeddings import (
    DOCUMENT_PREFIX,
    EmbedCache,
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    EmbedLimiter,
    content_hash,
    pack_vector,
)
from app.llm.fake_adapter import FakeEmbedder, fake_embedding, fake_rate_limit_error
from app.llm.key_pool import KeyPool

CONFIG = EmbeddingConfig(dimensions=4)
KEY = CONFIG.key()
T0 = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def blob(seed: float) -> bytes:
    return pack_vector([seed, 0.0, 0.0, 0.0])


def client_with(cache: EmbedCache, fake_time: Any, embedder: FakeEmbedder) -> EmbeddingClient:
    return EmbeddingClient(
        embedder,
        KeyPool(
            ["test-key-0000-aaaa", "test-key-1111-bbbb"], cooldown_sec=60, clock=fake_time.clock
        ),
        EmbedLimiter(clock=fake_time.clock, sleep=fake_time.sleep),
        cache,
        estimate_tokens=estimate_tokens,
        sleep=fake_time.sleep,
    )


async def test_vectors_round_trip_per_config(index_db: IndexDb) -> None:
    cache = EmbedCache(index_db)

    await cache.store(KEY, {"h1": blob(1.0), "h2": blob(2.0)})

    assert await cache.lookup(KEY, ["h1", "h2", "h3"]) == {"h1": blob(1.0), "h2": blob(2.0)}
    assert await cache.lookup("gemini-embedding-2:768:gemini2:none", ["h1"]) == {}


async def test_store_overwrites_the_same_hash(index_db: IndexDb) -> None:
    cache = EmbedCache(index_db)

    await cache.store(KEY, {"h1": blob(1.0)})
    await cache.store(KEY, {"h1": blob(3.0)})

    assert await cache.lookup(KEY, ["h1"]) == {"h1": blob(3.0)}


async def test_live_units_are_looked_up_before_the_table(index_db: IndexDb) -> None:
    asked: list[tuple[str, list[str]]] = []

    async def live(config_key: str, hashes: Sequence[str]) -> Mapping[str, bytes]:
        asked.append((config_key, list(hashes)))
        return {"h1": blob(9.0)}

    cache = EmbedCache(index_db, live=live)
    await cache.store(KEY, {"h1": blob(1.0), "h2": blob(2.0)})

    found = await cache.lookup(KEY, ["h1", "h2", "h1"])

    # 살아 있는 유닛의 벡터가 앞선다(지운 유닛의 오래된 벡터보다 최신이다).
    assert found == {"h1": blob(9.0), "h2": blob(2.0)}
    assert asked == [(KEY, ["h1", "h2"])]


async def test_purge_drops_old_rows_then_the_oldest_over_the_cap(index_db: IndexDb) -> None:
    clock = Clock()
    cache = EmbedCache(index_db, now=clock)
    for day in range(5):
        clock.now = T0 + timedelta(days=day)
        await cache.store(KEY, {f"day{day}": blob(float(day))})
    clock.now = T0 + timedelta(days=31)

    assert await cache.purge(max_age_days=30, max_rows=50_000) == 1
    assert await cache.purge(max_age_days=30, max_rows=2) == 2
    assert set(await cache.lookup(KEY, [f"day{day}" for day in range(5)])) == {"day3", "day4"}


async def test_cache_hits_do_not_call_the_api_or_the_limiter(
    index_db: IndexDb, fake_time: Any
) -> None:
    cache = EmbedCache(index_db)
    cached = pack_vector(fake_embedding("이미 있는 청크", 4))
    await cache.store(KEY, {content_hash("이미 있는 청크"): cached})
    embedder = FakeEmbedder()
    client = client_with(cache, fake_time, embedder)

    all_hits = await client.embed_documents(["이미 있는 청크"], CONFIG)
    mixed = await client.embed_documents(["이미 있는 청크", "새 청크"], CONFIG)

    assert all_hits.vectors == [cached] and all_hits.cached == 1
    assert mixed.vectors[0] == cached and mixed.cached == 1
    assert [call.inputs for call in embedder.calls] == [(DOCUMENT_PREFIX + "새 청크",)]
    assert client.limiter.usage("free")[0] == 1


async def test_cache_hits_still_work_after_the_daily_limit(
    index_db: IndexDb, fake_time: Any
) -> None:
    cache = EmbedCache(index_db)
    cached = pack_vector(fake_embedding("이미 있는 청크", 4))
    await cache.store(KEY, {content_hash("이미 있는 청크"): cached})
    client = client_with(cache, fake_time, FakeEmbedder([fake_rate_limit_error("day")]))

    try:
        await client.embed_documents(["새 청크"], CONFIG)
    except EmbeddingError:
        pass

    assert client.daily_exhausted(CONFIG.model) is not None
    assert (await client.embed_documents(["이미 있는 청크"], CONFIG)).vectors == [cached]


async def test_cached_vectors_of_another_size_are_ignored(
    index_db: IndexDb, fake_time: Any
) -> None:
    cache = EmbedCache(index_db)
    await cache.store(KEY, {content_hash("청크"): pack_vector([1.0, 0.0])})
    embedder = FakeEmbedder()
    client = client_with(cache, fake_time, embedder)

    result = await client.embed_documents(["청크"], CONFIG)

    assert result.cached == 0 and len(embedder.calls) == 1
    assert result.vectors[0] is not None and len(result.vectors[0]) == 16
