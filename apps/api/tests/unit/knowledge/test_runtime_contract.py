"""M23과 함께 쓰는 계약: doc_id_of, KnowledgeRuntime, get_knowledge_runtime(3단계 설계 5.2, 6.6)."""

import uuid
from collections.abc import AsyncGenerator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request

from app.config import Settings
from app.knowledge.index_db import IndexUnavailable
from app.knowledge.units import doc_id_of
from app.llm.embeddings import EmbeddingConfig
from app.llm.fake_adapter import FakeAdapter, FakeEmbedder
from app.llm.gemini_adapter import GeminiAdapter
from app.services.errors import ServiceError
from app.services.knowledge_runtime import (
    KnowledgeRuntime,
    build_knowledge_runtime,
    get_knowledge_runtime,
    optional_knowledge_runtime,
)

KEYS = ["test-key-0000-aaaa", "test-key-1111-bbbb"]


def settings_for(tmp_path: Path, **values: Any) -> Settings:
    return Settings.model_validate({"data_dir": tmp_path / "data", **values})


def request_with(knowledge: object) -> Request:
    state = SimpleNamespace() if knowledge is None else SimpleNamespace(knowledge=knowledge)
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))


class EmbeddingChatTransport:
    """채팅 전송 자리에 넣는 가짜. embed가 있어 임베딩 클라이언트가 같은 객체를 쓴다."""

    def __init__(self) -> None:
        self.embedder = FakeEmbedder()

    async def stream_chat(
        self, *, api_key: str, body: Mapping[str, Any]
    ) -> AsyncGenerator[dict[str, Any], None]:
        raise AssertionError("채팅은 부르지 않는다")
        yield {}

    async def list_models(self, *, api_key: str) -> list[str]:
        return []

    async def embed(
        self, *, api_key: str, model: str, inputs: Sequence[str], dimensions: int | None
    ) -> list[list[float]]:
        return await self.embedder.embed(
            api_key=api_key, model=model, inputs=inputs, dimensions=dimensions
        )

    async def aclose(self) -> None:
        return None


def test_doc_id_is_one_canonical_string() -> None:
    document_id = uuid.UUID("0196f3a2-7b1c-7d2e-9f00-123456789abc")

    assert doc_id_of(document_id) == "0196f3a2-7b1c-7d2e-9f00-123456789abc"
    # app.db에서 읽은 글, hex, 대문자도 같은 값이 된다.
    assert doc_id_of(str(document_id)) == doc_id_of(document_id)
    assert doc_id_of(document_id.hex) == doc_id_of(document_id)
    assert doc_id_of(str(document_id).upper()) == doc_id_of(document_id)
    with pytest.raises(ValueError):
        doc_id_of("not-a-uuid")


async def test_dependency_refuses_with_503_until_the_index_is_open(tmp_path: Path) -> None:
    runtime = build_knowledge_runtime(settings_for(tmp_path), FakeAdapter())

    for knowledge in (None, runtime):
        with pytest.raises(ServiceError) as refused:
            get_knowledge_runtime(request_with(knowledge))
        assert (refused.value.status, refused.value.code) == (503, "index_unavailable")
        assert optional_knowledge_runtime(request_with(knowledge)) is None

    assert await runtime.open()
    try:
        assert get_knowledge_runtime(request_with(runtime)) is runtime
        assert optional_knowledge_runtime(request_with(runtime)) is runtime
        assert runtime.index_db.path == tmp_path / "data" / "knowledge" / "index.db"
    finally:
        await runtime.close()
    with pytest.raises(ServiceError):
        get_knowledge_runtime(request_with(runtime))
    with pytest.raises(IndexUnavailable):
        await runtime.vectors.count(uuid.uuid4())


async def test_fake_adapter_gets_the_fake_embedder_without_free_caps(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    runtime = build_knowledge_runtime(
        settings_for(tmp_path, kb_embed_rpm=6000, kb_embed_rpd=1000), adapter
    )

    vector = await runtime.embeddings.embed_query("연차 신청", EmbeddingConfig())

    assert len(vector) == 768 * 4
    assert len(adapter.embedder.calls) == 1
    limits = runtime.limiter.limits("free")
    # 가짜에서는 무료 등급 분당 100건 상한과 하루 몫을 걸지 않는다(K2·memcheck가 6,000으로 돈다).
    assert (limits.rpm, limits.rpd) == (6000, 0)


async def test_gemini_adapter_shares_its_key_pool_and_transport(tmp_path: Path) -> None:
    transport = EmbeddingChatTransport()
    adapter = GeminiAdapter(api_keys=KEYS, model_fallback=["m"], transport=transport)
    runtime = build_knowledge_runtime(
        settings_for(tmp_path, kb_embed_rpm=500, kb_embed_reserve=10), adapter
    )

    await runtime.embeddings.embed_query("연차 신청", EmbeddingConfig())

    assert [call.api_key for call in transport.embedder.calls] == [KEYS[0]]
    # 같은 KeyPool이라 채팅의 키 상태 화면에 임베딩 호출도 보인다.
    assert adapter.key_status()[0].success_count == 1
    limits = runtime.limiter.limits("free")
    assert (limits.rpm, limits.reserve, limits.rpd) == (100, 10, 1000)


def test_runtime_fields_are_the_contract() -> None:
    assert list(KnowledgeRuntime.__dataclass_fields__) == [
        "index_db",
        "vectors",
        "fts",
        "embeddings",
        "limiter",
        "cache",
    ]
