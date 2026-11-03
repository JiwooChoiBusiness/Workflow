"""지식 실행 객체(3단계 설계 5장, 6.6). index.db, 벡터·전문 색인, 임베딩 클라이언트의 묶음이다.

lifespan이 Settings와 채팅 어댑터로 하나 만들어 app.state.knowledge에 두고, 검색(M23)·청크 편집·지식
레인이 같은 객체를 쓴다. 임베딩은 채팅 어댑터와 같은 KeyPool과 전송(SDK 연결)을 쓴다. LLM_FAKE이거나
어댑터가 가짜면 가짜 임베더를 쓰고 무료 등급 상한(분당 100건, 하루 몫)을 걸지 않는다.

유닛 단위 색인 쓰기(전문 색인 행, 벡터 행, 지울 때 캐시로 옮기기)도 여기 둔다. 청크 서비스와 레인
작업이 같은 순서(5.4)로 index.db를 다루게 하려는 것이다.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from fastapi import Request
from pydantic import ValidationError

from app.config import Settings
from app.db.models import KnowledgeBase
from app.db.models.enums import KbIndexingTechnique
from app.knowledge.fts_index import SqliteFtsIndex
from app.knowledge.index_db import IndexDb, IndexUnavailable, get_meta, set_meta
from app.knowledge.tokens import estimate_tokens
from app.knowledge.units import FtsRow, UnitKind, VecRow, doc_id_of
from app.knowledge.vector_index import SqliteVecIndex
from app.llm.base import ModelAdapter
from app.llm.embeddings import (
    FREE_EMBED_RPM_CAP,
    EmbedCache,
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingTransport,
    EmbedLimiter,
    TierLimits,
)
from app.llm.fake_adapter import FakeAdapter, FakeEmbedder
from app.llm.gemini_adapter import GeminiAdapter
from app.llm.key_pool import KeyPool, KeyTier
from app.services.errors import ServiceError

log = structlog.get_logger("app.services.knowledge")

INDEX_DIR = "knowledge"
INDEX_FILE = "index.db"
ERR_INDEX_UNAVAILABLE = "index_unavailable"
# 가짜 임베더에 줄 키. 값은 쓰이지 않고 KeyPool이 키 하나는 있어야 해서 둔다.
_FAKE_KEY = "fake-embedding-key"
_FAKE_PAID_KEY = "fake-embedding-paid-key"


@dataclass
class KnowledgeRuntime:
    index_db: IndexDb
    vectors: SqliteVecIndex
    fts: SqliteFtsIndex
    embeddings: EmbeddingClient
    limiter: EmbedLimiter
    cache: EmbedCache

    @property
    def available(self) -> bool:
        return self.index_db.available

    async def open(self) -> bool:
        """index.db를 열고 저장해 둔 오늘 임베딩 사용량을 읽는다. 실패는 available=False다."""
        if not await self.index_db.open():
            return False
        try:
            await self.limiter.load()
        except IndexUnavailable as exc:
            log.warning("knowledge.embed_daily_load_failed", error=str(exc))
        return True

    async def close(self) -> None:
        await self.limiter.flush()
        await self.index_db.close()


class _IndexMetaStore:
    """리미터의 하루 사용량을 index.db meta에 둔다(재기동해도 이어 센다)."""

    def __init__(self, db: IndexDb) -> None:
        self._db = db

    async def load(self, key: str) -> str | None:
        if not self._db.available:
            return None
        return await get_meta(self._db, key)

    async def save(self, key: str, value: str) -> None:
        await set_meta(self._db, key, value)


def index_path(settings: Settings) -> Path:
    return settings.data_dir / INDEX_DIR / INDEX_FILE


def _tier_limits(rpm: int, tpm: int, reserve: int, rpd: int) -> TierLimits:
    # RPM을 아주 작게 준 설정에서도 기동이 실패하지 않게 예약분을 RPM 아래로 맞춘다.
    return TierLimits(rpm=rpm, tpm=tpm, reserve=min(reserve, rpm - 1), rpd=rpd)


def build_knowledge_runtime(settings: Settings, llm: ModelAdapter) -> KnowledgeRuntime:
    """열지 않은 실행 객체. open()은 lifespan이 부른다."""
    index_db = IndexDb(index_path(settings), cache_kb=settings.kb_index_cache_kb)
    fake = settings.llm_fake or isinstance(llm, FakeAdapter)
    transport: EmbeddingTransport
    if isinstance(llm, GeminiAdapter) and not fake:
        pool = llm.key_pool
        found = llm.embedding_transport
        transport = found if found is not None else FakeEmbedder()
    else:
        paid = [_FAKE_PAID_KEY] if settings.gemini_paid_api_key is not None else []
        pool = KeyPool([_FAKE_KEY], cooldown_sec=settings.gemini_key_cooldown_sec, paid_keys=paid)
        transport = llm if isinstance(llm, FakeAdapter) else FakeEmbedder()
    reserve = settings.kb_embed_reserve
    limiter = EmbedLimiter(
        free=_tier_limits(
            settings.kb_embed_rpm,
            settings.kb_embed_tpm,
            reserve,
            0 if fake else settings.kb_embed_rpd,
        ),
        paid=_tier_limits(
            settings.kb_embed_rpm_paid,
            settings.kb_embed_tpm_paid,
            reserve,
            0 if fake else settings.kb_embed_rpd_paid,
        ),
        free_rpm_cap=None if fake else FREE_EMBED_RPM_CAP,
        store=_IndexMetaStore(index_db),
    )
    cache = EmbedCache(index_db)
    embeddings = EmbeddingClient(transport, pool, limiter, cache, estimate_tokens=estimate_tokens)
    return KnowledgeRuntime(
        index_db=index_db,
        vectors=SqliteVecIndex(index_db),
        fts=SqliteFtsIndex(index_db),
        embeddings=embeddings,
        limiter=limiter,
        cache=cache,
    )


def index_unavailable(reason: str | None = None) -> ServiceError:
    return ServiceError(
        503,
        ERR_INDEX_UNAVAILABLE,
        "지식 색인 파일(data/knowledge/index.db)을 쓸 수 없다. 서버 로그와 platform doctor를 본다",
        details={"reason": reason} if reason else None,
    )


def get_knowledge_runtime(request: Request) -> KnowledgeRuntime:
    """FastAPI 의존성. index.db를 열지 못했으면 503 index_unavailable이다(5.2)."""
    runtime = getattr(request.app.state, "knowledge", None)
    if not isinstance(runtime, KnowledgeRuntime):
        raise index_unavailable()
    if not runtime.index_db.available:
        raise index_unavailable(runtime.index_db.unavailable_reason)
    return runtime


def optional_knowledge_runtime(request: Request) -> KnowledgeRuntime | None:
    # 청크 편집처럼 index.db 없이도 app.db 쪽은 해 둘 수 있는 경로가 쓴다. 색인은 레인이 나중에
    # 한다.
    runtime = getattr(request.app.state, "knowledge", None)
    if isinstance(runtime, KnowledgeRuntime) and runtime.index_db.available:
        return runtime
    return None


def embedding_config_of(kb: KnowledgeBase) -> EmbeddingConfig | None:
    """저장 JSON(EmbeddingSettings)을 라이브러리 설정으로. economy이거나 값이 깨졌으면 None."""
    if kb.indexing_technique != KbIndexingTechnique.HIGH_QUALITY or not kb.embedding:
        return None
    try:
        return EmbeddingConfig.model_validate(kb.embedding)
    except ValidationError:
        log.warning("knowledge.embedding_invalid", kb_id=str(kb.id))
        return None


def key_tier_of(kb: KnowledgeBase) -> KeyTier:
    policy: Mapping[str, Any] = kb.data_policy or {}
    return "paid" if policy.get("key_tier") == "paid" else "free"


@dataclass(frozen=True)
class IndexUnit:
    """index.db에 넣을 유닛 하나. text는 임베딩 입력(Q&A는 질문만), body는 전문 색인 글이다."""

    unit_no: int
    document_id: uuid.UUID
    kind: UnitKind
    text: str
    body: str
    content_hash: str


async def write_fts(
    runtime: KnowledgeRuntime, kb: KnowledgeBase, units: Sequence[IndexUnit]
) -> None:
    if not units:
        return
    await runtime.fts.ensure(kb.id, workspace_id=kb.workspace_id)
    await runtime.fts.upsert(kb.id, [FtsRow(unit.unit_no, unit.body) for unit in units])


async def write_vectors(
    runtime: KnowledgeRuntime,
    kb: KnowledgeBase,
    config: EmbeddingConfig,
    rows: Sequence[tuple[IndexUnit, bytes]],
) -> None:
    if not rows:
        return
    await runtime.vectors.ensure(kb.id, config, workspace_id=kb.workspace_id)
    await runtime.vectors.upsert(
        kb.id,
        [
            VecRow(unit.unit_no, vector, doc_id_of(unit.document_id), unit.kind)
            for unit, vector in rows
        ],
    )


async def drop_units(
    runtime: KnowledgeRuntime,
    kb_id: uuid.UUID,
    units: Mapping[int, str],
    config: EmbeddingConfig | None,
) -> None:
    """유닛(unit_no → content_hash)의 색인 행을 지운다. 벡터는 먼저 embed_cache로 옮긴다(6.6).

    지운·끈 청크를 되살리거나 같은 글을 다시 넣을 때 API를 다시 부르지 않게 하려는 것이다.
    """
    if not units:
        return
    unit_nos = list(units)
    if config is not None:
        vectors = await runtime.vectors.get_many(kb_id, unit_nos)
        await runtime.cache.store(
            config.key(),
            {units[unit_no]: blob for unit_no, blob in vectors.items() if units.get(unit_no)},
        )
    await runtime.vectors.delete(kb_id, unit_nos)
    await runtime.fts.delete(kb_id, unit_nos)
