import math

import pytest

from app.llm.embeddings import DOCUMENT_PREFIX, QUERY_PREFIX, EmbeddingTransport
from app.llm.errors import TransportError, classify
from app.llm.fake_adapter import (
    FAKE_EMBED_INVALID,
    FakeAdapter,
    FakeEmbedCall,
    FakeEmbedder,
    fake_bad_request_error,
    fake_embedding,
    fake_rate_limit_error,
    fake_server_error,
)
from app.llm.gemini_adapter import OpenAICompatTransport
from app.llm.types import CanonicalMessage, ChatRequest, TextBlock


def cosine(left: list[float], right: list[float]) -> float:
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


def test_fake_vectors_are_deterministic_unit_vectors() -> None:
    vector = fake_embedding("주간 회의록 작성")

    assert len(vector) == 768
    assert vector == fake_embedding("주간 회의록 작성")
    assert math.fsum(value * value for value in vector) == pytest.approx(1.0)
    assert len(fake_embedding("회의록", 1536)) == 1536
    assert math.fsum(value * value for value in fake_embedding("")) == pytest.approx(1.0)


def test_texts_sharing_bigrams_are_closer() -> None:
    query = fake_embedding("출장비 정산 기한")

    assert cosine(query, fake_embedding("출장비 정산은 7일 안에 신청한다")) > cosine(
        query, fake_embedding("비밀번호는 90일마다 바꾼다")
    )


def test_prefixes_do_not_change_the_fake_vector() -> None:
    plain = fake_embedding("회의록")

    assert fake_embedding(DOCUMENT_PREFIX + "회의록") == plain
    assert fake_embedding(QUERY_PREFIX + "회의록") == plain


async def test_faults_are_used_in_order_then_answers_are_normal() -> None:
    limited = fake_rate_limit_error("minute")

    def only_second_key(call: FakeEmbedCall) -> TransportError | None:
        return fake_server_error() if call.api_key == "key-2" else None

    embedder = FakeEmbedder([limited, None, only_second_key, only_second_key])

    with pytest.raises(TransportError) as first:
        await embedder.embed(api_key="key-1", model="m", inputs=["가"], dimensions=8)
    second = await embedder.embed(api_key="key-1", model="m", inputs=["가"], dimensions=8)
    third = await embedder.embed(api_key="key-1", model="m", inputs=["가"], dimensions=8)
    with pytest.raises(TransportError):
        await embedder.embed(api_key="key-2", model="m", inputs=["가"], dimensions=8)
    fifth = await embedder.embed(api_key="key-2", model="m", inputs=["가", "나"], dimensions=None)

    assert first.value is limited
    assert second == third == [fake_embedding("가", 8)]
    assert [len(vector) for vector in fifth] == [768, 768]
    assert embedder.embedded_items == 6


async def test_marked_inputs_and_oversized_batches_are_bad_requests() -> None:
    embedder = FakeEmbedder()

    with pytest.raises(TransportError) as marked:
        await embedder.embed(
            api_key="k", model="m", inputs=["정상", f"x {FAKE_EMBED_INVALID}"], dimensions=8
        )
    with pytest.raises(TransportError) as oversized:
        await embedder.embed(api_key="k", model="m", inputs=["가"] * 101, dimensions=8)

    assert marked.value.status == 400 and oversized.value.status == 400
    assert "at most 100" in oversized.value.message


def test_scripted_errors_classify_like_the_measured_responses() -> None:
    minute = classify(fake_rate_limit_error("minute", retry_after=45))
    day = classify(fake_rate_limit_error("day", retry_after=17437))
    no_delay = classify(fake_rate_limit_error("minute", retry_after=None))

    assert (minute.kind, minute.scope, minute.retry_after) == ("rate_limited", "minute", 45)
    assert (day.kind, day.scope, day.retry_after) == ("rate_limited", "day", 17437)
    assert day.quota_id == "EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier"
    assert no_delay.retry_after is None
    assert classify(fake_bad_request_error()).kind == "invalid"
    assert classify(fake_server_error()).kind == "transient"


async def test_fake_adapter_is_an_embedding_transport_and_records_tiers() -> None:
    adapter = FakeAdapter()
    transport: EmbeddingTransport = adapter
    real: EmbeddingTransport = OpenAICompatTransport("http://127.0.0.1:9/never-called")

    vectors = await transport.embed(api_key="k", model="m", inputs=["회의록"], dimensions=16)
    request = ChatRequest(
        system="s",
        messages=[CanonicalMessage(role="user", blocks=[TextBlock(text="안녕")])],
        tier="paid",
    )
    async for _ in adapter.stream(request):
        pass

    assert vectors == [fake_embedding("회의록", 16)]
    assert adapter.embedder.calls[0].inputs == ("회의록",)
    assert adapter.tiers == ["paid"]
    assert real is not None
