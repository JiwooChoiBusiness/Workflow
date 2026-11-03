"""지식 베이스 만들기·설정·삭제(3단계 설계 4.1, 4.4, 12.1, 14.4).

색인과 검색은 지식 레인(app/knowledge, M22·M23)이 하고 여기서는 app.db의 상태만 맞춘다. 색인
설정(embedding)을 바꾸거나 economy를 high_quality로 올리면 version을 올리고 모든 유닛의 벡터를
pending, 문서를 waiting으로 돌린다(4.4). index.db의 테이블을 다시 만드는 일은 지식 레인이 설정
비교(5.4)로 맡는다.

문서·청크·메타데이터 서비스가 같이 쓰는 해석·검사 함수도 여기에 둔다.
"""

import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

import structlog
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import Database
from app.db.models import KbDocument, KnowledgeBase
from app.db.models.enums import KbDocForm, KbIndexingTechnique
from app.db.repositories.api_keys import ApiKeyRepository
from app.db.repositories.knowledge import (
    IN_PROGRESS_STATUSES,
    KbChildChunkRepository,
    KbDocumentRepository,
    KbSegmentRepository,
    KnowledgeBaseRepository,
)
from app.db.types import new_id, utcnow
from app.knowledge.chunking import (
    FULL_DOC_MAX_TOKENS,
    ChildRule,
    DocForm,
    GeneralRule,
    ParentChildRule,
    ParentRule,
)
from app.knowledge.cleaning import CleaningRules
from app.knowledge.ingest.job import SplitRules
from app.schemas.knowledge import (
    MAX_CHUNK_TOKENS,
    DataPolicy,
    DataPolicyIn,
    EmbeddingSettings,
    EmbeddingSettingsIn,
    KnowledgeBaseCreate,
    KnowledgeBaseList,
    KnowledgeBaseOut,
    KnowledgeBasePatch,
    ProcessRule,
    RetrievalSettings,
)
from app.services.agents import slugify
from app.services.errors import Problems, ServiceError, not_found

if TYPE_CHECKING:
    from app.services.knowledge_index_executor import KnowledgeLane

log = structlog.get_logger("app.services.knowledge")

_SLUG_MAX = 64
_SLUG_ATTEMPTS = 1000


def split_rules(rule: ProcessRule, doc_form: str, *, input_limit: int | None) -> SplitRules:
    """저장 JSON(ProcessRule)을 라이브러리 청크 규칙으로. 값 범위는 스키마가 이미 맞춰 두었다."""
    return SplitRules(
        doc_form=cast(DocForm, doc_form),
        cleaning=CleaningRules(
            remove_extra_spaces=rule.cleaning.remove_extra_spaces,
            remove_urls_emails=rule.cleaning.remove_urls_emails,
        ),
        general=GeneralRule(
            tuple(rule.general.separators), rule.general.max_tokens, rule.general.overlap_tokens
        ),
        parent_child=ParentChildRule(
            ParentRule(rule.parent.mode, tuple(rule.parent.separators), rule.parent.max_tokens),
            ChildRule(tuple(rule.child.separators), rule.child.max_tokens),
        ),
        input_limit=input_limit,
    )


def unit_index_state(indexing_technique: str) -> dict[str, Any]:
    """새로 만들거나 고친 유닛. 전문 색인은 늘, 벡터는 high_quality일 때만 다시 만든다(5.4)."""
    vector = "pending" if indexing_technique == KbIndexingTechnique.HIGH_QUALITY else "skip"
    return {"fts": "pending", "vector": vector, "embedding_version": None, "embedded_at": None}


def parent_index_state() -> dict[str, Any]:
    # Parent-child의 부모는 유닛이 아니다. 색인은 자식 청크가 갖는다.
    return {"fts": "skip", "vector": "skip", "embedding_version": None, "embedded_at": None}


def parse_process_rule(raw: Mapping[str, Any] | None) -> ProcessRule:
    try:
        return ProcessRule.model_validate(raw or {})
    except ValidationError:
        log.warning("knowledge.process_rule_invalid")
        return ProcessRule()


def parse_retrieval(raw: Mapping[str, Any] | None) -> RetrievalSettings:
    try:
        return RetrievalSettings.model_validate(raw or {})
    except ValidationError:
        log.warning("knowledge.retrieval_model_invalid")
        return RetrievalSettings()


def parse_embedding(raw: Mapping[str, Any] | None) -> EmbeddingSettings | None:
    if raw is None:
        return None
    return EmbeddingSettings.model_validate(raw)


def parse_data_policy(raw: Mapping[str, Any] | None) -> DataPolicy:
    return DataPolicy.model_validate(raw or {})


def chunk_token_limit(kb: KnowledgeBase, rule: ProcessRule, *, child: bool = False) -> int:
    """손으로 넣는 청크의 토큰 상한(14.3). 문서 규칙의 max_tokens를 따른다."""
    if child:
        return rule.child.max_tokens
    if kb.doc_form == KbDocForm.HIERARCHICAL:
        return FULL_DOC_MAX_TOKENS if rule.parent.mode == "full_doc" else rule.parent.max_tokens
    if kb.doc_form == KbDocForm.QA:
        # Q&A는 나누지 않아 규칙이 없다. 질문 하나가 General 청크의 최대 상한을 넘지 않게 한다.
        return MAX_CHUNK_TOKENS
    return rule.general.max_tokens


async def active_kb(
    session: AsyncSession, workspace_id: uuid.UUID, kb_id: uuid.UUID
) -> KnowledgeBase:
    kb = await KnowledgeBaseRepository(session, workspace_id).get_active(kb_id)
    if kb is None:
        raise not_found("지식 베이스")
    return kb


async def live_document(
    session: AsyncSession, workspace_id: uuid.UUID, kb_id: uuid.UUID, document_id: uuid.UUID
) -> tuple[KnowledgeBase, KbDocument]:
    kb = await active_kb(session, workspace_id, kb_id)
    document = await KbDocumentRepository(session, workspace_id).get_live(kb.id, document_id)
    if document is None:
        raise not_found("문서")
    return kb, document


def kb_out(kb: KnowledgeBase, in_progress: int = 0) -> KnowledgeBaseOut:
    return KnowledgeBaseOut(
        id=kb.id,
        slug=kb.slug,
        name=kb.name,
        icon=kb.icon,
        description=kb.description,
        doc_form=kb.doc_form,  # pyright: ignore[reportArgumentType]
        indexing_technique=kb.indexing_technique,  # pyright: ignore[reportArgumentType]
        embedding=parse_embedding(kb.embedding),
        process_rule=parse_process_rule(kb.process_rule),
        retrieval_model=parse_retrieval(kb.retrieval_model),
        search_while_indexing=kb.search_while_indexing,
        built_in_fields_enabled=kb.built_in_fields_enabled,
        data_policy=parse_data_policy(kb.data_policy),
        document_count=kb.document_count,
        segment_count=kb.segment_count,
        indexing_document_count=in_progress,
        embedded_tokens_estimated=kb.embedded_tokens_estimated,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


def _slug_taken(slug: str | None) -> ServiceError:
    if slug is None:
        return ServiceError(409, "slug_taken", "slug가 다른 지식 베이스와 겹친다")
    return ServiceError(409, "slug_taken", f"이미 쓰는 slug다: {slug}", details={"slug": slug})


async def _free_slug(kbs: KnowledgeBaseRepository, name: str) -> str:
    base = slugify(name) or "knowledge"
    candidate = base
    for number in range(2, _SLUG_ATTEMPTS):
        if not await kbs.slug_taken(candidate):
            return candidate
        suffix = f"-{number}"
        candidate = base[: _SLUG_MAX - len(suffix)].rstrip("-") + suffix
    raise _slug_taken(candidate)


def _embedding(settings: EmbeddingSettingsIn, version: int) -> dict[str, Any]:
    return EmbeddingSettings(**settings.model_dump(), version=version).model_dump(mode="json")


def _same_embedding(current: EmbeddingSettings, wanted: EmbeddingSettingsIn) -> bool:
    return (current.model, current.dimensions, current.prefix) == (
        wanted.model,
        wanted.dimensions,
        wanted.prefix,
    )


class KnowledgeService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        lane: "KnowledgeLane | None" = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._lane = lane
        self._clock = clock

    def _check_policy(self, policy: DataPolicyIn, problems: Problems) -> dict[str, Any]:
        # 무료 키로 색인·검색한 문서는 Google 제품 개선에 쓰일 수 있다는 안내를 확인해야 한다(14.4).
        if not policy.acknowledged:
            problems.add(
                ("data_policy", "acknowledged"),
                "data_policy_not_acknowledged",
                "데이터 정책 안내를 확인해야 지식 베이스를 저장한다",
            )
        if policy.key_tier == "paid" and self._settings.gemini_paid_api_key is None:
            raise ServiceError(
                422,
                "paid_key_missing",
                "GEMINI_PAID_API_KEY가 없어 유료 키 전용을 고를 수 없다",
            )
        return DataPolicy(key_tier=policy.key_tier, acknowledged_at=self._clock()).model_dump(
            mode="json"
        )

    def _check_retrieval(
        self, retrieval: RetrievalSettings, technique: str, problems: Problems
    ) -> None:
        if retrieval.top_k > self._settings.kb_top_k_max:
            problems.add(
                ("retrieval_model", "top_k"),
                "top_k_too_large",
                f"top_k는 {self._settings.kb_top_k_max} 이하여야 한다",
            )
        if (
            technique == KbIndexingTechnique.ECONOMY
            and retrieval.search_method != "full_text_search"
        ):
            problems.add(
                ("retrieval_model", "search_method"),
                "search_method_unavailable",
                "economy 지식 베이스는 벡터가 없어 full_text_search만 쓸 수 있다",
            )

    async def list_bases(self, workspace_id: uuid.UUID) -> KnowledgeBaseList:
        async with self._db.read() as session:
            rows = await KnowledgeBaseRepository(session, workspace_id).list_active()
            counts = await KbDocumentRepository(session, workspace_id).in_progress_counts(
                [row.id for row in rows]
            )
        return KnowledgeBaseList(items=[kb_out(row, counts.get(row.id, 0)) for row in rows])

    async def get_base(self, workspace_id: uuid.UUID, kb_id: uuid.UUID) -> KnowledgeBaseOut:
        async with self._db.read() as session:
            return await self._out(
                session, workspace_id, await active_kb(session, workspace_id, kb_id)
            )

    async def _out(
        self, session: AsyncSession, workspace_id: uuid.UUID, kb: KnowledgeBase
    ) -> KnowledgeBaseOut:
        counts = await KbDocumentRepository(session, workspace_id).in_progress_counts([kb.id])
        return kb_out(kb, counts.get(kb.id, 0))

    async def create_base(
        self, workspace_id: uuid.UUID, body: KnowledgeBaseCreate
    ) -> KnowledgeBaseOut:
        problems = Problems()
        technique = body.indexing_technique
        if technique == KbIndexingTechnique.ECONOMY and body.embedding is not None:
            problems.add(
                ("embedding",),
                "embedding_not_allowed",
                "economy 지식 베이스는 임베딩을 쓰지 않는다",
            )
        default_method = (
            "full_text_search" if technique == KbIndexingTechnique.ECONOMY else "hybrid_search"
        )
        retrieval = body.retrieval_model or RetrievalSettings(search_method=default_method)
        self._check_retrieval(retrieval, technique, problems)
        policy = self._check_policy(body.data_policy, problems)
        problems.raise_if_any()
        embedding = (
            _embedding(body.embedding or EmbeddingSettingsIn(), 1)
            if technique == KbIndexingTechnique.HIGH_QUALITY
            else None
        )
        try:
            async with self._db.write() as session:
                kbs = KnowledgeBaseRepository(session, workspace_id)
                if body.slug is not None:
                    if await kbs.slug_taken(body.slug):
                        raise _slug_taken(body.slug)
                    slug = body.slug
                else:
                    slug = await _free_slug(kbs, body.name)
                kb = kbs.add(
                    KnowledgeBase(
                        id=new_id(),
                        slug=slug,
                        name=body.name,
                        icon=body.icon,
                        description=body.description,
                        doc_form=body.doc_form,
                        indexing_technique=technique,
                        embedding=embedding,
                        process_rule=(body.process_rule or ProcessRule()).model_dump(mode="json"),
                        retrieval_model=retrieval.model_dump(mode="json"),
                        search_while_indexing=body.search_while_indexing,
                        built_in_fields_enabled=False,
                        data_policy=policy,
                        document_count=0,
                        segment_count=0,
                        embedded_tokens_estimated=0,
                    )
                )
                await kbs.flush()
                out = kb_out(kb)
        except IntegrityError:
            raise _slug_taken(body.slug) from None
        log.info(
            "knowledge.created",
            kb_id=str(out.id),
            doc_form=out.doc_form,
            indexing_technique=out.indexing_technique,
            key_tier=out.data_policy.key_tier,
        )
        return out

    async def update_base(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, patch: KnowledgeBasePatch
    ) -> KnowledgeBaseOut:
        problems = Problems()
        reembed = False
        try:
            async with self._db.write() as session:
                kbs = KnowledgeBaseRepository(session, workspace_id)
                kb = await active_kb(session, workspace_id, kb_id)
                technique = kb.indexing_technique
                current = parse_embedding(kb.embedding)
                if patch.indexing_technique is not None and patch.indexing_technique != technique:
                    if patch.indexing_technique == KbIndexingTechnique.ECONOMY:
                        raise ServiceError(
                            422,
                            "downgrade_not_allowed",
                            "high_quality 지식 베이스는 economy로 되돌릴 수 없다",
                        )
                    # economy → high_quality. 벡터가 처음 생기므로 확인 없이 모든 유닛을 올린다.
                    technique = KbIndexingTechnique.HIGH_QUALITY
                    version = current.version + 1 if current is not None else 1
                    kb.embedding = _embedding(patch.embedding or EmbeddingSettingsIn(), version)
                    reembed = True
                elif patch.embedding is not None:
                    if technique == KbIndexingTechnique.ECONOMY:
                        problems.add(
                            ("embedding",),
                            "embedding_not_allowed",
                            "economy 지식 베이스는 임베딩을 쓰지 않는다."
                            " 먼저 high_quality로 올린다",
                        )
                    elif current is None or not _same_embedding(current, patch.embedding):
                        if not patch.confirm_reembed:
                            raise ServiceError(
                                409,
                                "reembed_confirmation_required",
                                "색인 설정을 바꾸면 모든 청크를 다시 임베딩한다."
                                " confirm_reembed를 참으로 보낸다",
                            )
                        version = current.version + 1 if current is not None else 1
                        kb.embedding = _embedding(patch.embedding, version)
                        reembed = True
                retrieval = patch.retrieval_model
                if retrieval is None and patch.indexing_technique is not None:
                    retrieval = parse_retrieval(kb.retrieval_model)
                if retrieval is not None:
                    self._check_retrieval(retrieval, technique, problems)
                    kb.retrieval_model = retrieval.model_dump(mode="json")
                if patch.data_policy is not None:
                    kb.data_policy = self._check_policy(patch.data_policy, problems)
                problems.raise_if_any()
                if patch.slug is not None and patch.slug != kb.slug:
                    if await kbs.slug_taken(patch.slug, exclude_id=kb.id):
                        raise _slug_taken(patch.slug)
                    kb.slug = patch.slug
                if patch.name is not None:
                    kb.name = patch.name
                if "icon" in patch.model_fields_set:
                    kb.icon = patch.icon
                if patch.description is not None:
                    kb.description = patch.description
                if patch.process_rule is not None:
                    kb.process_rule = patch.process_rule.model_dump(mode="json")
                if patch.search_while_indexing is not None:
                    kb.search_while_indexing = patch.search_while_indexing
                kb.indexing_technique = technique
                now = self._clock()
                kb.updated_at = now
                requeued: list[uuid.UUID] = []
                if reembed:
                    await self._requeue_vectors(session, workspace_id, kb.id, now)
                    requeued = await KbDocumentRepository(session, workspace_id).ids_for_kb(
                        kb.id, statuses=IN_PROGRESS_STATUSES, limit=100_000
                    )
                await kbs.flush()
                out = await self._out(session, workspace_id, kb)
        except IntegrityError:
            raise _slug_taken(patch.slug) from None
        if reembed:
            # 색인 중이던 작업은 상태가 바뀌어 다음 경계에서 빠지고, 새 작업이 새 설정으로 잇는다.
            if self._lane is not None:
                for document_id in requeued:
                    self._lane.index(workspace_id, document_id)
            log.info(
                "knowledge.reembed_requested",
                kb_id=str(kb_id),
                embedding_version=out.embedding.version if out.embedding else None,
                documents=len(requeued),
            )
        return out

    async def _requeue_vectors(
        self, session: AsyncSession, workspace_id: uuid.UUID, kb_id: uuid.UUID, now: datetime
    ) -> None:
        # 분할 결과는 그대로 두고 벡터만 다시 만든다. 지식 레인이 waiting 문서를 집어 pending
        # 유닛만 임베딩한다(6.5).
        await KbSegmentRepository(session, workspace_id).requeue_vectors(kb_id, now)
        await KbChildChunkRepository(session, workspace_id).requeue_vectors(kb_id)
        await KbDocumentRepository(session, workspace_id).requeue_for_embedding(kb_id, now)

    async def delete_base(self, workspace_id: uuid.UUID, kb_id: uuid.UUID) -> None:
        """소프트 삭제. 문서·청크·색인은 매시 정리가 지운다(4.4, 4.5). 그 KB의 키는 바로 폐기.

        대기·색인 중인 문서의 작업은 큐에서 빼고 멈춘다. 작업과 기동 복구는 지운 KB의 문서를 집지
        않는다(M21 검토 2-9).
        """
        async with self._db.write() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            now = self._clock()
            kb.deleted_at = now
            await ApiKeyRepository(session, workspace_id).revoke_all_for_knowledge_base(kb.id, now)
            busy = await KbDocumentRepository(session, workspace_id).ids_for_kb(
                kb.id, statuses=IN_PROGRESS_STATUSES, limit=100_000
            )
        if self._lane is not None:
            for document_id in busy:
                self._lane.stop(workspace_id, document_id, "delete")
        log.info("knowledge.deleted", kb_id=str(kb_id), stopped_jobs=len(busy))
