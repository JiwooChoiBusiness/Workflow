"""지식 베이스 검색(3단계 설계 7장). app.db·index.db·임베딩을 잇고, 결합·정렬은
app/knowledge/retrieval이 한다.

검색 시험, 평가, datasets retrieve, 그리고 M24의 knowledge_retrieval 노드와 search_knowledge 도구가
모두 retrieve() 하나를 쓴다. 흐름은 설계 7.1 그대로다.

1. 설정: 부르는 쪽이 준 RetrievalSettings, 없으면 KB 설정. KB가 여럿이면 부르는 쪽 설정이 필수다.
2. 메타데이터 필터 → KB마다 검색할 문서 집합.
3. 질의 임베딩(config_key마다 한 번) → KB마다 벡터·전문 후보.
4. 문턱 → 결합 → 청크로 묶기 → KB 안 top_k → KB 사이 병합 → (LLM 재순위) → top_k.
5. 적재, hit_count, 질의 기록.
"""

import asyncio
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

import structlog
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.engine import Database
from app.db.models import KbQueryLog, KnowledgeBase
from app.db.models.enums import KbDocForm
from app.db.repositories.knowledge import (
    IN_PROGRESS_STATUSES,
    KbMetadataFieldRepository,
    KnowledgeBaseRepository,
)
from app.db.repositories.knowledge_search import (
    KbQueryLogRepository,
    KbSearchChildRepository,
    KbSearchDocumentRepository,
    KbSearchSegmentRepository,
)
from app.db.types import new_id, utcnow
from app.knowledge.index_db import IndexUnavailable
from app.knowledge.retrieval.filters import (
    FieldInfo,
    FilterError,
    FilterPlan,
    check_fields,
    common_fields,
    plan,
)
from app.knowledge.retrieval.rerank import ModelCall, auto_filter, rerank
from app.knowledge.retrieval.resources import (
    ChildInfo,
    DocumentInfo,
    KbInfo,
    RetrievedRecord,
    SegmentInfo,
)
from app.knowledge.retrieval.service import (
    KbHits,
    RankedSegment,
    SearchChannel,
    UnitRef,
    apply_relevance,
    candidate_depth,
    kb_keep,
    merge,
    rank_kb,
    rerank_pool,
)
from app.knowledge.tokens import char_weight, token_weight, tokens_from_weight
from app.knowledge.units import FtsHit, Hit, doc_id_of
from app.knowledge.vector_index import MAX_K
from app.llm.base import ModelAdapter
from app.llm.embeddings import EmbeddingConfig, EmbeddingError
from app.llm.key_pool import KeyTier
from app.schemas.knowledge import RetrievalSettings
from app.schemas.retrieval import (
    WARN_FILTER_IGNORED,
    WARN_MIXED_KB_RRF,
    WARN_PARTIAL_INDEX,
    WARN_QUERY_TRUNCATED,
    WARN_RERANK_FAILED,
    WARN_VECTOR_UNAVAILABLE,
    MetadataFieldSummary,
    MetadataFilter,
    QueryLogList,
    QueryLogOut,
    QueryLogResult,
    QuerySource,
    RetrievalWarning,
    RetrievalWarningCode,
    RetrieveQuery,
    RetrieveResponse,
    RetrieverResource,
)
from app.services.errors import Problems, ServiceError
from app.services.knowledge import active_kb, parse_retrieval
from app.services.knowledge_metadata import document_metadata
from app.services.knowledge_runtime import (
    KnowledgeRuntime,
    embedding_config_of,
    index_unavailable,
    key_tier_of,
)

log = structlog.get_logger("app.services.knowledge_search")

# 노드·도구는 KB를 5개까지 고른다(7.6).
MAX_KBS = 5
# 메타데이터 필터로 걸러진 문서가 이보다 많으면 vec0의 doc_id IN(500개씩 KNN 한 번)을 여러 번 도는
# 대신 넉넉히 뽑아 app.db에서 거른다.
DOC_FILTER_LIMIT = 1000
# 걸러서 후보가 모자라면 이만큼씩 늘려 다시 뽑는다.
WIDEN_FACTOR = 4
LOG_RESULTS = 20
MAX_LOG_QUERY = 1000
# 검색 시험·평가·datasets retrieve(Dify의 test retrieval)는 hit_count를 세지 않는다.
COUNTED_SOURCES = frozenset({"node", "tool"})

ERR_KB_REQUIRED = "kb_required"
ERR_TOO_MANY_KBS = "too_many_knowledge_bases"
ERR_KB_NOT_FOUND = "knowledge_base_not_found"
ERR_SETTINGS_REQUIRED = "retrieval_settings_required"
# 질의 임베딩 오류 코드 → HTTP 상태. 다시 해 볼 수 없는 오류(설정·요청 문제)는 422와 503이다.
_EMBEDDING_STATUS = {
    "rate_limited": 429,
    "rate_limited_daily": 429,
    "paid_key_missing": 422,
    "model_request_invalid": 422,
    "model_auth": 503,
    "model_unavailable": 503,
}

VectorState = Literal["ready", "missing", "stale"]


class RetrievalFailed(ServiceError):
    """검색 전체가 실패했다. 노드·도구는 retryable과 retry_after(초)를 본다.

    벡터 전용 검색의 질의 임베딩 실패, 다시 해 볼 수 없는 임베딩 오류(유료 키 없음 등),
    색인 파일을 쓸 수 없는 경우(index_unavailable, 503)다.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
        reason: str | None = None,
    ) -> None:
        details: dict[str, Any] = {"retryable": retryable}
        if retry_after is not None:
            details["retry_after"] = round(retry_after, 1)
        if reason is not None:
            details["reason"] = reason
        super().__init__(status, code, message, details=details)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class RetrievalResult:
    query: str
    settings: RetrievalSettings
    records: list[RetrievedRecord]
    warnings: list[RetrievalWarning]
    # 자동 필터·LLM 재순위의 채팅 호출. 노드가 node.usage로 낸다.
    model_calls: list[ModelCall]
    latency_ms: int
    # 자동 필터가 만든 조건(모델 호출이 실패했으면 None)과 그 조건을 실제로 걸었는지. 필드가 없거나
    # 값이 틀려 버린 조건도 질의 기록에서 볼 수 있게 남긴다.
    auto_filter: MetadataFilter | None = None
    auto_filter_applied: bool = False

    def resources(self) -> list[RetrieverResource]:
        return [record.resource() for record in self.records]

    def response(self) -> RetrieveResponse:
        return RetrieveResponse(
            query=RetrieveQuery(content=self.query),
            records=[record.record() for record in self.records],
            warnings=self.warnings,
            latency_ms=self.latency_ms,
        )


@dataclass(frozen=True)
class _Target:
    id: uuid.UUID
    name: str
    parent_child: bool
    config: EmbeddingConfig | None
    tier: KeyTier
    completed_only: bool

    @classmethod
    def of(cls, kb: KnowledgeBase) -> "_Target":
        return cls(
            id=kb.id,
            name=kb.name,
            parent_child=kb.doc_form == KbDocForm.HIERARCHICAL,
            config=embedding_config_of(kb),
            tier=key_tier_of(kb),
            completed_only=not kb.search_while_indexing,
        )


@dataclass
class _Scope:
    """KB 하나에서 검색할 문서.

    질의마다 문서 목록을 읽지 않는다. 지운·끈·보관한 문서는 후보 유닛을 app.db에서 읽을 때 SQL
    조건으로 빠진다. 문서 id를 직접 드는 것은 메타데이터 필터에 맞는 문서이거나, 검색 못 하는 문서가
    있고 검색할 문서가 DOC_FILTER_LIMIT 이하라 벡터 검색에 doc_id IN을 걸 때뿐이다.
    """

    # 검색할 문서가 하나라도 있는지.
    searchable: bool
    allowed: set[uuid.UUID] | None
    # 메타데이터 필터나 검색 못 하는 문서가 있어 index.db 결과를 걸러야 하는지.
    restricted: bool
    # 색인이 진행 중인 문서(waiting~indexing)가 있는지. error·paused는 세지 않는다.
    in_progress: bool

    def admits(self, document_id: uuid.UUID) -> bool:
        return self.allowed is None or document_id in self.allowed


@dataclass(frozen=True)
class _Searched:
    hits: KbHits
    # 벡터 색인이 색인 설정과 어긋나(재임베딩 중) 전문 검색으로 돌았는지.
    stale: bool


def _warning(
    code: RetrievalWarningCode,
    message: str,
    kb_ids: Sequence[uuid.UUID] = (),
    retry_after: float | None = None,
) -> RetrievalWarning:
    return RetrievalWarning(
        code=code,
        message=message,
        kb_ids=list(dict.fromkeys(kb_ids)),
        retry_after=round(retry_after, 1) if retry_after is not None else None,
    )


def _higher(first: KeyTier, second: KeyTier) -> KeyTier:
    return "paid" if "paid" in (first, second) else "free"


def _embedding_failure(exc: EmbeddingError) -> RetrievalFailed:
    return RetrievalFailed(
        _EMBEDDING_STATUS.get(exc.code, 503),
        exc.code,
        f"질의 임베딩에 실패했다: {exc.message}",
        retryable=exc.retryable,
        retry_after=exc.retry_after,
    )


def _unavailable(reason: str | None = None) -> RetrievalFailed:
    base = index_unavailable(reason)
    return RetrievalFailed(base.status, base.code, base.message, retryable=True, reason=reason)


def _leaf(error: BaseException) -> BaseException:
    # TaskGroup은 실패를 예외 묶음으로 낸다. 부르는 쪽(API·노드)은 원래 예외를 본다.
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def _field_info(row: Any) -> FieldInfo:
    return FieldInfo(id=row.id, name=row.name, type=row.type)


def fit_query(text: str, config: EmbeddingConfig) -> str:
    """접두어를 붙인 질의가 임베딩 입력 상한(추정 토큰) 안에 들도록 앞부분만 남긴다.

    상한을 넘는 입력은 API가 오류 없이 잘라 버리므로 어디가 잘렸는지 알 수 있게 여기서 자른다.
    """
    limit = config.input_token_limit
    weight = token_weight(config.query_input(""))
    for index, char in enumerate(text):
        weight += char_weight(char)
        if tokens_from_weight(weight) > limit:
            return text[:index]
    return text


def _cut[HitT: (Hit, FtsHit)](
    kept: Sequence[HitT], units: Mapping[int, UnitRef], depth: int, keep: int, parent_child: bool
) -> tuple[bool, int]:
    """후보가 충분한지와 남길 길이. Parent-child는 유닛이 아니라 묶일 부모 수로 본다.

    부모 하나에 자식이 많으면(full_doc) 유닛 깊이만으로는 부모가 남길 개수보다 적게 잡힌다.
    """
    if not parent_child:
        return len(kept) >= depth, depth
    parents: set[uuid.UUID] = set()
    for index, hit in enumerate(kept):
        parents.add(units[hit.unit_no].segment_id)
        if len(parents) >= keep and index + 1 >= depth:
            return True, index + 1
    return False, len(kept)


class KnowledgeSearchService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        runtime: KnowledgeRuntime | None,
        *,
        llm: ModelAdapter | None = None,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._db = db
        self._settings = settings
        self._runtime = runtime
        self._llm = llm
        self._clock = clock
        self._monotonic = monotonic

    @property
    def _index(self) -> KnowledgeRuntime:
        # 기록 조회·평가셋 관리는 index.db 없이도 되므로 실행 객체가 없을 수 있다.
        if self._runtime is None:
            raise _unavailable()
        return self._runtime

    async def retrieve(
        self,
        workspace_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
        query: str,
        settings: RetrievalSettings | None = None,
        *,
        metadata_filter: MetadataFilter | None = None,
        source: QuerySource,
        tier: KeyTier = "free",
    ) -> RetrievalResult:
        """kb_ids(최대 5개, 중복은 하나로)를 함께 검색한다. 결과가 없으면 빈 목록이다.

        - 설정: settings가 없으면 KB 설정이고, KB가 여럿이면 settings가 필요하다(422
          retrieval_settings_required). top_k가 KB_TOP_K_MAX를 넘으면 422다.
        - 질의: NFC로 맞추고 앞뒤 공백을 뗀다. 빈 질의는 아무것도 부르지 않고 빈 결과다. 임베딩
          입력 상한을 넘으면 앞부분만 쓰고 query_truncated를 단다(result.query가 실제로 쓴 질의).
        - 등급: tier는 부르는 런의 키 등급이다. 유료 KB가 하나라도 있으면 채팅 호출(자동 필터·
          재순위)은 유료 키로 가고, 질의 임베딩은 KB마다 그 KB와 런 중 높은 등급을 쓴다(14.4).
        - 실패: 색인 파일을 쓸 수 없으면 질의 임베딩 전에 RetrievalFailed(503 index_unavailable,
          retryable)다. 질의 임베딩이 다시 해 볼 수 있는 오류(한도, 장애)로 실패하면 하이브리드는
          전문 검색만으로 결과를 내고 vector_unavailable을 단다. 이때 score는 bm25를 목록 최대값으로
          나눈 값으로 뜻이 바뀐다. 벡터 전용 검색이거나 다시 해 볼 수 없는 오류(유료 키 없음, 인증,
          요청 거절)는 RetrievalFailed다.
        - 경고: partial_index(색인 진행 중 문서, 재임베딩 중이라 전문 검색으로 돈 KB),
          mixed_kb_rrf, rerank_failed(원래 순서), filter_ignored(필터 없이), vector_unavailable,
          query_truncated.
        - 시간 상한은 부르는 쪽이 asyncio.timeout으로 건다. 도중에 취소되면 이미 받은 리미터
          허가(임베딩 몫)는 돌아오지 않고, 질의 기록과 hit_count는 남지 않는다.
        """
        started = self._monotonic()
        runtime = self._index
        text = unicodedata.normalize("NFC", query).strip()
        targets, effective, fields = await self._prepare(workspace_id, kb_ids, settings)
        chat_tier: KeyTier = tier
        for target in targets:
            chat_tier = _higher(chat_tier, target.tier)
        warnings: list[RetrievalWarning] = []
        calls: list[ModelCall] = []
        if not text:
            return RetrievalResult(text, effective, [], warnings, calls, 0)
        try:
            # 임베딩 몫과 채팅 호출을 쓰기 전에 색인을 쓸 수 있는지 먼저 본다.
            runtime.index_db.ensure_available()
        except IndexUnavailable as exc:
            raise _unavailable(str(exc)) from exc
        text = self._fit(text, targets, effective, warnings)
        filter_plan, auto_filter = await self._filter_plan(
            targets, fields, metadata_filter, text, chat_tier, warnings, calls
        )
        scopes = await self._scopes(workspace_id, targets, fields, filter_plan)
        try:
            vectors = await self._query_vectors(targets, effective, text, tier, warnings)
            # 한 KB가 실패하면 나머지 KB 검색도 멈춘다.
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(
                        self._search(
                            workspace_id, target, scopes[target.id], effective, text, vectors
                        )
                    )
                    for target in targets
                ]
        except IndexUnavailable as exc:
            raise _unavailable(str(exc)) from exc
        except BaseExceptionGroup as caught:
            error = _leaf(caught)
            if isinstance(error, IndexUnavailable):
                raise _unavailable(str(error)) from error
            raise error from caught
        searched = [task.result() for task in tasks]
        rankings = [rank_kb(item.hits, effective) for item in searched]
        merged, mixed = merge(rankings, effective.reranking_mode)
        if mixed:
            message = (
                "reranking_mode가 rrf라 지식 베이스 사이도 순위(RRF)로 섞었다"
                if effective.reranking_mode == "rrf"
                else "검색 방식이나 임베딩 설정이 다른 지식 베이스라 점수 대신 순위(RRF)로 섞었다"
            )
            warnings.append(_warning(WARN_MIXED_KB_RRF, message, [t.id for t in targets]))
        llm = effective.reranking_mode == "llm"
        pool = merged[: rerank_pool(effective) if llm else effective.top_k]
        records = await self._load(workspace_id, {t.id: t for t in targets}, pool)
        if llm and records:
            records = await self._rerank(records, text, effective, chat_tier, warnings, calls)
        records = [
            replace(record, position=position)
            for position, record in enumerate(records[: effective.top_k], start=1)
        ]
        partial = [target.id for target in targets if scopes[target.id].in_progress]
        partial += [item.hits.kb_id for item in searched if item.stale]
        if partial:
            warnings.append(
                _warning(
                    WARN_PARTIAL_INDEX,
                    "색인이 끝나지 않은 문서가 있어 일부는 전문 검색으로만 잡히거나 빠질 수 있다",
                    partial,
                )
            )
        latency = int((self._monotonic() - started) * 1000)
        result = RetrievalResult(
            text,
            effective,
            records,
            warnings,
            calls,
            latency,
            auto_filter=auto_filter,
            auto_filter_applied=auto_filter is not None and filter_plan is not None,
        )
        await self._bookkeep(workspace_id, targets, result, metadata_filter, source)
        return result

    async def list_fields(
        self, workspace_id: uuid.UUID, kb_ids: Sequence[uuid.UUID]
    ) -> list[MetadataFieldSummary]:
        """모든 KB에 같은 이름·타입으로 있는 메타데이터 필드(자동 필터와 노드 폼이 쓴다)."""
        targets, _, fields = await self._prepare(workspace_id, kb_ids, RetrievalSettings())
        common = common_fields([fields[target.id] for target in targets])
        return [MetadataFieldSummary(name=name, type=kind) for name, kind in common.items()]

    async def list_query_logs(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, *, page: int = 1, limit: int = 20
    ) -> QueryLogList:
        offset = (page - 1) * limit
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            logs = KbQueryLogRepository(session, workspace_id)
            total = await logs.count(kb.id)
            rows = await logs.list_page(kb.id, limit=limit, offset=offset)
        items = [
            QueryLogOut(
                id=row.id,
                source=row.source,  # pyright: ignore[reportArgumentType]
                query=row.query,
                settings=row.settings or {},
                results=[QueryLogResult.model_validate(item) for item in row.results or []],
                result_count=row.result_count,
                latency_ms=row.latency_ms,
                created_at=row.created_at,
            )
            for row in rows
        ]
        return QueryLogList(
            items=items,
            total=total,
            page=page,
            limit=limit,
            has_more=offset + len(rows) < total,
        )

    async def _prepare(
        self,
        workspace_id: uuid.UUID,
        kb_ids: Sequence[uuid.UUID],
        settings: RetrievalSettings | None,
    ) -> tuple[list[_Target], RetrievalSettings, dict[uuid.UUID, list[FieldInfo]]]:
        ids = list(dict.fromkeys(kb_ids))
        if not ids:
            raise ServiceError(422, ERR_KB_REQUIRED, "검색할 지식 베이스를 하나 이상 고른다")
        if len(ids) > MAX_KBS:
            raise ServiceError(
                422,
                ERR_TOO_MANY_KBS,
                f"지식 베이스는 {MAX_KBS}개까지 함께 검색한다",
                details={"max": MAX_KBS},
            )
        async with self._db.read() as session:
            rows = await KnowledgeBaseRepository(session, workspace_id).get_many_active(ids)
            by_id = {row.id: row for row in rows}
            missing = [kb_id for kb_id in ids if kb_id not in by_id]
            if missing:
                raise ServiceError(
                    404,
                    ERR_KB_NOT_FOUND,
                    "지식 베이스를 찾을 수 없다",
                    details={"kb_ids": [str(kb_id) for kb_id in missing]},
                )
            fields_repo = KbMetadataFieldRepository(session, workspace_id)
            fields = {
                kb_id: [_field_info(row) for row in await fields_repo.list_for_kb(kb_id)]
                for kb_id in ids
            }
            stored = parse_retrieval(by_id[ids[0]].retrieval_model)
        targets = [_Target.of(by_id[kb_id]) for kb_id in ids]
        return targets, self._effective(targets, settings, stored), fields

    def _effective(
        self,
        targets: Sequence[_Target],
        settings: RetrievalSettings | None,
        stored: RetrievalSettings,
    ) -> RetrievalSettings:
        top_k_max = self._settings.kb_top_k_max
        if settings is None:
            # KB 설정은 후보 집합이고 병합·절단은 부르는 쪽 설정이다(Dify의 두 층, 7.1).
            if len(targets) > 1:
                raise ServiceError(
                    422,
                    ERR_SETTINGS_REQUIRED,
                    "여러 지식 베이스를 함께 검색하려면 검색 설정을 준다",
                )
            # 저장한 뒤 KB_TOP_K_MAX를 낮췄을 수 있다.
            return stored.model_copy(update={"top_k": min(stored.top_k, top_k_max)})
        if settings.top_k > top_k_max:
            problems = Problems()
            problems.add(
                ("retrieval_model", "top_k"),
                "top_k_too_large",
                f"top_k는 {top_k_max} 이하여야 한다",
            )
            problems.raise_if_any()
        return settings

    def _fit(
        self,
        text: str,
        targets: Sequence[_Target],
        settings: RetrievalSettings,
        warnings: list[RetrievalWarning],
    ) -> str:
        if settings.search_method == "full_text_search":
            return text
        configs = [target.config for target in targets if target.config is not None]
        if not configs:
            return text
        config = min(configs, key=lambda item: item.input_token_limit)
        fitted = fit_query(text, config)
        if fitted != text:
            warnings.append(
                _warning(
                    WARN_QUERY_TRUNCATED,
                    f"질의가 임베딩 입력 상한({config.input_token_limit}토큰)을 넘어 앞부분 "
                    f"{len(fitted)}자만 썼다",
                    [target.id for target in targets if target.config is not None],
                )
            )
        return fitted

    async def _filter_plan(
        self,
        targets: Sequence[_Target],
        fields: Mapping[uuid.UUID, list[FieldInfo]],
        metadata_filter: MetadataFilter | None,
        query: str,
        tier: KeyTier,
        warnings: list[RetrievalWarning],
        calls: list[ModelCall],
    ) -> tuple[FilterPlan | None, MetadataFilter | None]:
        """(걸 조건, 자동 필터가 만든 조건). 자동 필터가 아니면 두 번째는 None이다."""
        if metadata_filter is None or metadata_filter.mode == "disabled":
            return None, None
        kb_ids = [target.id for target in targets]
        per_kb = {target.id: fields[target.id] for target in targets}
        if metadata_filter.mode == "manual":
            if not metadata_filter.conditions:
                return None, None
            try:
                types = check_fields(metadata_filter.conditions, per_kb)
                return plan(metadata_filter, types), None
            except FilterError as exc:
                raise ServiceError(422, exc.code, exc.message, details=exc.details) from exc
        common = common_fields([per_kb[kb_id] for kb_id in kb_ids])
        outcome = await auto_filter(
            self._llm,
            query,
            common,
            model_id=metadata_filter.model.model_id,
            tier=tier,
            today=self._clock().astimezone().date(),
        )
        calls.extend(outcome.calls)
        reason = outcome.error
        built = outcome.metadata_filter
        if built is not None:
            try:
                return (plan(built, common) if built.conditions else None), built
            except FilterError as exc:
                reason = exc.message
        warnings.append(
            _warning(
                WARN_FILTER_IGNORED,
                f"메타데이터 자동 필터를 만들지 못해 필터 없이 검색했다: {reason}"[:500],
                kb_ids,
            )
        )
        log.info("knowledge.auto_filter_ignored", kb_ids=[str(k) for k in kb_ids], reason=reason)
        return None, built

    async def _scopes(
        self,
        workspace_id: uuid.UUID,
        targets: Sequence[_Target],
        fields: Mapping[uuid.UUID, list[FieldInfo]],
        filter_plan: FilterPlan | None,
    ) -> dict[uuid.UUID, _Scope]:
        scopes: dict[uuid.UUID, _Scope] = {}
        async with self._db.read() as session:
            documents = KbSearchDocumentRepository(session, workspace_id)
            for target in targets:
                if filter_plan is not None:
                    statuses = await documents.searchable(
                        target.id,
                        completed_only=target.completed_only,
                        plan=filter_plan,
                        field_ids={field.name: field.id for field in fields[target.id]},
                    )
                    scopes[target.id] = _Scope(
                        searchable=bool(statuses),
                        allowed=set(statuses),
                        restricted=True,
                        in_progress=any(
                            status in IN_PROGRESS_STATUSES for status in statuses.values()
                        ),
                    )
                    continue
                count, rows, in_progress = await documents.summary(
                    target.id, completed_only=target.completed_only
                )
                # 지우는 중인 문서도 행으로 센다. index.db에 아직 그 유닛이 남아 있을 수 있다.
                restricted = count < rows
                allowed: set[uuid.UUID] | None = None
                if restricted and 0 < count <= DOC_FILTER_LIMIT:
                    allowed = set(
                        await documents.searchable(target.id, completed_only=target.completed_only)
                    )
                scopes[target.id] = _Scope(
                    searchable=count > 0,
                    allowed=allowed,
                    restricted=restricted,
                    in_progress=in_progress,
                )
        return scopes

    async def _query_vectors(
        self,
        targets: Sequence[_Target],
        settings: RetrievalSettings,
        query: str,
        tier: KeyTier,
        warnings: list[RetrievalWarning],
    ) -> dict[str, bytes | None]:
        """config_key마다 질의 벡터. 실패한 설정은 None이다(하이브리드는 전문 검색만 돈다).

        한도·장애처럼 다시 해 볼 수 있는 오류만 전문 검색으로 낮춘다. 유료 키 없음·인증·요청
        거절은 기다려도 그대로라 검색 방식과 관계없이 실패로 올린다.
        """
        if settings.search_method == "full_text_search":
            return {}
        wanted: dict[str, tuple[EmbeddingConfig, KeyTier, list[uuid.UUID]]] = {}
        for target in targets:
            if target.config is None:
                continue
            key = target.config.key()
            config, key_tier, members = wanted.get(key, (target.config, tier, []))
            members.append(target.id)
            wanted[key] = (config, _higher(key_tier, target.tier), members)
        vectors: dict[str, bytes | None] = {}
        for key, (config, key_tier, members) in wanted.items():
            try:
                vectors[key] = await self._index.embeddings.embed_query(
                    query, config, tier=key_tier
                )
            except EmbeddingError as exc:
                if settings.search_method == "semantic_search" or not exc.retryable:
                    raise _embedding_failure(exc) from exc
                vectors[key] = None
                warnings.append(
                    _warning(
                        WARN_VECTOR_UNAVAILABLE,
                        f"질의 임베딩에 실패해 전문 검색만으로 찾았다({exc.code}): {exc.message}",
                        members,
                        exc.retry_after,
                    )
                )
                log.warning(
                    "knowledge.query_embedding_failed",
                    code=exc.code,
                    kb_ids=[str(kb_id) for kb_id in members],
                )
        return vectors

    async def _vector_state(self, target: _Target) -> VectorState:
        # 색인 설정과 맞는 벡터 테이블이 있어야 한다. 차원이 같고 모델만 다르면 vec0가 오류 없이
        # 엉뚱한 결과를 낸다(5.4). 어긋나 있으면 재임베딩 중이다.
        config = target.config
        if config is None:
            return "missing"
        meta = await self._index.vectors.describe(target.id)
        if meta is None or meta.vec_table is None:
            return "missing"
        same = (
            meta.config_key == config.key()
            and meta.embedding_version == config.version
            and meta.dims == config.dimensions
        )
        return "ready" if same else "stale"

    async def _search(
        self,
        workspace_id: uuid.UUID,
        target: _Target,
        scope: _Scope,
        settings: RetrievalSettings,
        query: str,
        vectors: Mapping[str, bytes | None],
    ) -> _Searched:
        """KB 하나의 후보. 벡터 색인이 없거나 재임베딩 중이면 벡터 전용 검색도 전문 검색으로
        돈다(4.4)."""
        method = settings.search_method
        wants_vector = target.config is not None and method != "full_text_search"
        wants_keyword = target.config is None or method != "semantic_search"
        query_vector = vectors.get(target.config.key()) if target.config is not None else None
        channels: set[SearchChannel] = set()
        vector: list[Hit] = []
        keyword: list[FtsHit] = []
        units: dict[int, UnitRef] = {}
        stale = False
        if not scope.searchable:
            return _Searched(KbHits(target.id, frozenset(), [], [], {}), stale)
        depth = candidate_depth(settings.top_k, self._settings.kb_search_candidates)
        keep = kb_keep(settings)
        if wants_vector and query_vector is not None:
            state = await self._vector_state(target)
            if state == "ready":
                channels.add("vector")
                vector = await self._vector_hits(
                    workspace_id, target, scope, query_vector, depth, keep, units
                )
            else:
                wants_keyword = True
                stale = state == "stale"
        if wants_keyword:
            channels.add("keyword")
            keyword = await self._keyword_hits(
                workspace_id, target, scope, query, depth, keep, units
            )
        config_key = target.config.key() if target.config is not None else None
        return _Searched(
            KbHits(target.id, frozenset(channels), vector, keyword, units, config_key), stale
        )

    async def _vector_hits(
        self,
        workspace_id: uuid.UUID,
        target: _Target,
        scope: _Scope,
        query_vector: bytes,
        depth: int,
        keep: int,
        units: dict[int, UnitRef],
    ) -> list[Hit]:
        doc_ids: list[str] | None = None
        k = depth
        if (
            scope.restricted
            and scope.allowed is not None
            and len(scope.allowed) <= DOC_FILTER_LIMIT
        ):
            doc_ids = [doc_id_of(doc_id) for doc_id in scope.allowed]
        elif scope.restricted:
            k = min(depth * WIDEN_FACTOR, MAX_K)

        async def fetch(size: int) -> list[Hit]:
            return await self._index.vectors.search(target.id, query_vector, size, doc_ids=doc_ids)

        return await self._widen(fetch, workspace_id, target, scope, depth, keep, k, units)

    async def _keyword_hits(
        self,
        workspace_id: uuid.UUID,
        target: _Target,
        scope: _Scope,
        query: str,
        depth: int,
        keep: int,
        units: dict[int, UnitRef],
    ) -> list[FtsHit]:
        # 전문 색인(contentless)에는 문서 열이 없어 넉넉히 뽑아 app.db에서 거른다(17.4).
        k = min(depth * WIDEN_FACTOR, MAX_K) if scope.restricted else depth

        async def fetch(size: int) -> list[FtsHit]:
            return await self._index.fts.search(target.id, query, size)

        return await self._widen(fetch, workspace_id, target, scope, depth, keep, k, units)

    async def _widen[HitT: (Hit, FtsHit)](
        self,
        fetch: Callable[[int], Awaitable[list[HitT]]],
        workspace_id: uuid.UUID,
        target: _Target,
        scope: _Scope,
        depth: int,
        keep: int,
        k: int,
        units: dict[int, UnitRef],
    ) -> list[HitT]:
        """거르고 나서(Parent-child는 부모로 묶고 나서) 모자라면 k를 늘려 다시 뽑는다."""
        while True:
            found = await fetch(k)
            kept = await self._keep(workspace_id, target, scope, found, units)
            enough, size = _cut(kept, units, depth, keep, target.parent_child)
            if enough or len(found) < k or k >= MAX_K:
                return kept[:size]
            k = min(k * WIDEN_FACTOR, MAX_K)

    async def _keep[HitT: (Hit, FtsHit)](
        self,
        workspace_id: uuid.UUID,
        target: _Target,
        scope: _Scope,
        found: Sequence[HitT],
        units: dict[int, UnitRef],
    ) -> list[HitT]:
        """검색할 수 있는 유닛만 남긴다. app.db에 없는 유닛(고아)은 조용히 버린다(5.4)."""
        unknown = [hit.unit_no for hit in found if hit.unit_no not in units]
        if unknown:
            async with self._db.read() as session:
                if target.parent_child:
                    rows = await KbSearchChildRepository(session, workspace_id).units(
                        target.id, unknown, completed_only=target.completed_only
                    )
                    refs = [UnitRef(unit, seg, doc, child) for unit, child, seg, doc in rows]
                else:
                    plain = await KbSearchSegmentRepository(session, workspace_id).units(
                        target.id, unknown, completed_only=target.completed_only
                    )
                    refs = [UnitRef(unit, seg, doc) for unit, seg, doc in plain]
            for ref in refs:
                if scope.admits(ref.document_id):
                    units[ref.unit_no] = ref
        return [hit for hit in found if hit.unit_no in units]

    async def _load(
        self,
        workspace_id: uuid.UUID,
        targets: Mapping[uuid.UUID, _Target],
        ranked: Sequence[RankedSegment],
    ) -> list[RetrievedRecord]:
        if not ranked:
            return []
        segment_ids = [item.segment_id for item in ranked]
        document_ids = list(dict.fromkeys(item.document_id for item in ranked))
        child_ids = [hit.child_id for item in ranked for hit in item.children]
        async with self._db.read() as session:
            segments = {
                row.id: row
                for row in await KbSearchSegmentRepository(session, workspace_id).get_many(
                    segment_ids
                )
            }
            documents = {
                row.id: row
                for row in await KbSearchDocumentRepository(session, workspace_id).get_many(
                    document_ids
                )
            }
            children = {
                row.id: row
                for row in await KbSearchChildRepository(session, workspace_id).get_many(child_ids)
            }
            metadata: dict[uuid.UUID, dict[str, Any]] = {}
            for kb_id in dict.fromkeys(item.kb_id for item in ranked):
                ids = [doc_id for doc_id in document_ids if documents.get(doc_id) is not None]
                ids = [doc_id for doc_id in ids if documents[doc_id].kb_id == kb_id]
                values = await document_metadata(session, workspace_id, kb_id, ids)
                for doc_id, items in values.items():
                    metadata[doc_id] = {item.name: item.value for item in items}
        records: list[RetrievedRecord] = []
        for item in ranked:
            segment = segments.get(item.segment_id)
            document = documents.get(item.document_id)
            if segment is None or document is None:
                continue
            target = targets[item.kb_id]
            records.append(
                RetrievedRecord(
                    position=len(records) + 1,
                    kb=KbInfo(id=target.id, name=target.name),
                    ranked=item,
                    segment=SegmentInfo(
                        id=segment.id,
                        document_id=segment.document_id,
                        position=segment.position,
                        content=segment.content,
                        answer=segment.answer,
                        word_count=segment.word_count,
                        tokens=segment.tokens,
                        hit_count=segment.hit_count,
                        enabled=segment.enabled,
                        status=segment.status,
                    ),
                    document=DocumentInfo(
                        id=document.id,
                        name=document.name,
                        data_source_type=document.data_source_type,
                        indexing_status=document.indexing_status,  # pyright: ignore[reportArgumentType]
                        doc_metadata=metadata.get(document.id, {}),
                    ),
                    children=tuple(
                        ChildInfo(id=child.id, content=child.content, position=child.position)
                        for hit in item.children
                        if (child := children.get(hit.child_id)) is not None
                    ),
                )
            )
        return records

    async def _rerank(
        self,
        records: list[RetrievedRecord],
        query: str,
        settings: RetrievalSettings,
        tier: KeyTier,
        warnings: list[RetrievalWarning],
        calls: list[ModelCall],
    ) -> list[RetrievedRecord]:
        outcome = await rerank(
            self._llm,
            query,
            [record.passage() for record in records],
            model_id=settings.rerank.model_id,
            tier=tier,
        )
        calls.extend(outcome.calls)
        if outcome.relevance is None:
            # 실패·시간 초과·파싱 실패는 원래 순서로 돌려준다(7.4).
            warnings.append(
                _warning(
                    WARN_RERANK_FAILED,
                    f"LLM 재순위에 실패해 결합 순서 그대로 돌려준다: {outcome.error}"[:500],
                    list(dict.fromkeys(record.kb.id for record in records)),
                )
            )
            log.info("knowledge.rerank_failed", reason=outcome.error)
            return records
        by_segment = {record.ranked.segment_id: record for record in records}
        reordered = apply_relevance([record.ranked for record in records], outcome.relevance)
        return [replace(by_segment[item.segment_id], ranked=item) for item in reordered]

    async def _bookkeep(
        self,
        workspace_id: uuid.UUID,
        targets: Sequence[_Target],
        result: RetrievalResult,
        metadata_filter: MetadataFilter | None,
        source: QuerySource,
    ) -> None:
        counted = source in COUNTED_SOURCES and result.records
        logged = self._settings.kb_query_log_days > 0
        if not counted and not logged:
            return
        try:
            async with self._db.write() as session:
                if counted:
                    await KbSearchSegmentRepository(session, workspace_id).bump_hits(
                        [record.segment.id for record in result.records]
                    )
                    await KbSearchDocumentRepository(session, workspace_id).bump_hits(
                        list(dict.fromkeys(record.document.id for record in result.records))
                    )
                if logged:
                    self._add_logs(session, workspace_id, targets, result, metadata_filter, source)
        except SQLAlchemyError as exc:
            # 기록 실패로 검색 결과를 버리지 않는다.
            log.warning("knowledge.query_log_failed", error=str(exc))

    def _add_logs(
        self,
        session: Any,
        workspace_id: uuid.UUID,
        targets: Sequence[_Target],
        result: RetrievalResult,
        metadata_filter: MetadataFilter | None,
        source: QuerySource,
    ) -> None:
        logs = KbQueryLogRepository(session, workspace_id)
        snapshot: dict[str, Any] = {
            "retrieval_model": result.settings.model_dump(mode="json"),
            "kb_ids": [str(target.id) for target in targets],
        }
        if metadata_filter is not None and metadata_filter.mode != "disabled":
            snapshot["metadata_filter"] = metadata_filter.model_dump(mode="json")
        if result.auto_filter is not None:
            snapshot["auto_filter"] = {
                **result.auto_filter.model_dump(
                    mode="json", include={"logical_operator", "conditions"}
                ),
                "applied": result.auto_filter_applied,
            }
        if result.warnings:
            snapshot["warnings"] = [warning.code for warning in result.warnings]
        now = self._clock()
        for target in targets:
            mine = [record for record in result.records if record.kb.id == target.id]
            logs.add(
                KbQueryLog(
                    id=new_id(),
                    kb_id=target.id,
                    source=source,
                    query=result.query[:MAX_LOG_QUERY],
                    settings=snapshot,
                    results=[
                        {"segment_id": str(record.segment.id), "score": record.score}
                        for record in mine[:LOG_RESULTS]
                    ],
                    result_count=len(mine),
                    latency_ms=result.latency_ms,
                    created_at=now,
                )
            )
