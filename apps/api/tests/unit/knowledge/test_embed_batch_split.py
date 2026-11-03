"""400은 입력 문제다. 묶음 하나 때문에 전체가 실패하지 않게 반으로 나눠 범인만 error로 둔다(6.6)."""

from typing import Any

import pytest

from app.knowledge.tokens import estimate_tokens
from app.llm.embeddings import (
    DOCUMENT_PREFIX,
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingError,
    EmbedLimiter,
)
from app.llm.fake_adapter import FAKE_EMBED_INVALID, FakeEmbedder
from app.llm.key_pool import KeyPool
from app.llm.types import ERR_MODEL_REQUEST_INVALID

KEYS = ["test-key-0000-aaaa", "test-key-1111-bbbb"]
CONFIG = EmbeddingConfig()
BAD = f"깨진 입력 {FAKE_EMBED_INVALID}"


def setup(fake_time: Any) -> tuple[EmbeddingClient, FakeEmbedder, EmbedLimiter]:
    embedder = FakeEmbedder()
    limiter = EmbedLimiter(clock=fake_time.clock, sleep=fake_time.sleep)
    pool = KeyPool(KEYS, cooldown_sec=60, clock=fake_time.clock)
    client = EmbeddingClient(
        embedder,
        pool,
        limiter,
        None,
        estimate_tokens=estimate_tokens,
        sleep=fake_time.sleep,
    )
    return client, embedder, limiter


def texts_with_bad(size: int, bad: set[int]) -> list[str]:
    return [BAD if index in bad else f"문단 {index}" for index in range(size)]


async def test_only_the_culprit_is_marked(fake_time: Any) -> None:
    client, embedder, _ = setup(fake_time)

    result = await client.embed_documents(texts_with_bad(10, {6}), CONFIG)

    assert list(result.errors) == [6]
    assert all(vector is not None for index, vector in enumerate(result.vectors) if index != 6)
    assert result.vectors[6] is None
    # 10(400) → 0~4, 5~9(400) → 5~6(400) → 5, 6(400) → 7~9
    assert [len(call.inputs) for call in embedder.calls] == [10, 5, 5, 2, 1, 1, 3]
    assert embedder.calls[5].inputs == (DOCUMENT_PREFIX + BAD,)


async def test_several_culprits_in_different_halves(fake_time: Any) -> None:
    client, _, _ = setup(fake_time)

    result = await client.embed_documents(texts_with_bad(12, {1, 9, 10}), CONFIG)

    assert sorted(result.errors) == [1, 9, 10]
    assert sum(vector is not None for vector in result.vectors) == 9


async def test_repeated_bad_text_is_marked_everywhere(fake_time: Any) -> None:
    client, embedder, _ = setup(fake_time)

    result = await client.embed_documents([BAD, "정상", BAD], CONFIG)

    assert sorted(result.errors) == [0, 2]
    assert result.vectors[1] is not None
    assert [len(call.inputs) for call in embedder.calls] == [2, 1, 1]


async def test_a_single_bad_document_is_an_item_error(fake_time: Any) -> None:
    client, _, _ = setup(fake_time)

    result = await client.embed_documents([BAD], CONFIG)

    assert result.vectors == [None] and list(result.errors) == [0]


async def test_a_batch_where_every_item_fails_is_a_request_error(fake_time: Any) -> None:
    # 하나씩 보내도 모두 400이면 입력이 아니라 요청(모델·차원) 문제라 다음 묶음도 같다.
    client, _, _ = setup(fake_time)

    result = await client.embed_documents([BAD + " 1", BAD + " 2", BAD + " 3"], CONFIG)

    assert result.failure is not None
    assert result.failure.code == ERR_MODEL_REQUEST_INVALID and not result.failure.retryable
    assert result.vectors == [None, None, None] and result.errors == {}


async def test_single_edit_with_bad_input_raises(fake_time: Any) -> None:
    client, _, _ = setup(fake_time)

    with pytest.raises(EmbeddingError) as raised:
        await client.embed_single(BAD, CONFIG)

    assert raised.value.code == ERR_MODEL_REQUEST_INVALID


async def test_split_requests_are_counted_by_the_limiter(fake_time: Any) -> None:
    client, embedder, limiter = setup(fake_time)

    await client.embed_documents(texts_with_bad(10, {6}), CONFIG)

    sent = sum(len(call.inputs) for call in embedder.calls)
    assert limiter.usage("free")[0] == sent == 27
