"""문서의 청크와 자식 청크 보기·추가·편집·켜기·끄기·삭제(3단계 설계 4.1, 4.4, 5.4, 12.1).

청크를 고치면 요청 안에서 index.db까지 맞춘다. app.db에 pending을 적고 → 전문 색인 행과 벡터(단건
임베딩, 리미터 interactive)를 쓰고 → ok를 적는다. 끄거나 지우면 색인 행부터 지우고 벡터는 임베딩
캐시로 옮긴다(되살리거나 같은 글을 다시 넣을 때 API를 부르지 않게). 색인 파일이 없거나
임베딩이 실패하면(429, 일일 한도) app.db 쪽만 해 두고 문서를 지식 레인에 넣는다. 레인은 pending
유닛만 잇는다.

유닛 번호(unit_no)는 쓰기 락 안에서 kb_counter로 받는다. Parent-child의 부모는 유닛이 아니라 번호가
없고 색인은 자식 청크가 갖는다. 부모를 새로 넣으면 문서의 자식 규칙으로 자식을 나눠 붙이고, 부모의
글을 고칠 때는 자식을 그대로 둔다(Dify의 regenerate_child_chunks 기본값과 같다).

색인 중인 문서의 청크는 고칠 수 없고(작업이 청크를 다시 쓴다) 보관 문서는 읽기 전용이다(4.4).
"""

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import Database
from app.db.models import KbChildChunk, KbDocument, KbSegment, KnowledgeBase
from app.db.models.enums import KbCreatedBy, KbDocForm, KbIndexingStatus, KbSegmentStatus
from app.db.repositories.knowledge import (
    KbChildChunkRepository,
    KbCounterRepository,
    KbDocumentRepository,
    KbSegmentRepository,
    KnowledgeBaseRepository,
)
from app.db.types import new_id, utcnow
from app.knowledge.chunking import GeneralRule, split_general
from app.knowledge.cleaning import normalize
from app.knowledge.index_db import IndexUnavailable
from app.knowledge.tokens import estimate_tokens
from app.llm.embeddings import content_hash
from app.schemas.knowledge import (
    ChildChunkCreate,
    ChildChunking,
    ChildChunkList,
    ChildChunkOut,
    ChildChunkPatch,
    SegmentCreate,
    SegmentList,
    SegmentOut,
    SegmentPatch,
)
from app.services.errors import Problems, ServiceError, not_found
from app.services.knowledge import (
    chunk_token_limit,
    live_document,
    parent_index_state,
    parse_process_rule,
    unit_index_state,
)
from app.services.knowledge_documents import check_writable
from app.services.knowledge_index_executor import (
    KnowledgeLane,
    PendingUnit,
    UnitIndexer,
    refresh_document_totals,
    settle_parent,
    unit_of_child,
    unit_of_segment,
)
from app.services.knowledge_runtime import (
    KnowledgeRuntime,
    drop_units,
    embedding_config_of,
)

log = structlog.get_logger("app.services.knowledge_segments")

# 청크를 고칠 수 있는 문서 상태. 그 밖(대기·파싱~임베딩)은 작업이 청크를 다시 쓰는 중이다.
_EDITABLE = frozenset({KbIndexingStatus.COMPLETED, KbIndexingStatus.ERROR, KbIndexingStatus.PAUSED})
# 요청 안의 색인이 실패하면 문서를 레인에 넣는다. 멈춘 문서는 재개할 때 pending을 잇는다.
_REQUEUE_FROM = (KbIndexingStatus.COMPLETED, KbIndexingStatus.ERROR)
_INDEX_ERRORS = (IndexUnavailable, LookupError, ValueError, sqlite3.Error)


def child_out(child: KbChildChunk) -> ChildChunkOut:
    return ChildChunkOut(
        id=child.id,
        segment_id=child.segment_id,
        position=child.position,
        unit_no=child.unit_no,
        content=child.content,
        tokens=child.tokens,
        index_state=child.index_state,
        created_by=child.created_by,  # pyright: ignore[reportArgumentType]
        created_at=child.created_at,
        updated_at=child.updated_at,
    )


def segment_out(segment: KbSegment, children: list[KbChildChunk] | None = None) -> SegmentOut:
    return SegmentOut(
        id=segment.id,
        document_id=segment.document_id,
        position=segment.position,
        unit_no=segment.unit_no,
        content=segment.content,
        answer=segment.answer,
        word_count=segment.word_count,
        tokens=segment.tokens,
        enabled=segment.enabled,
        status=segment.status,  # pyright: ignore[reportArgumentType]
        index_state=segment.index_state,
        created_by=segment.created_by,  # pyright: ignore[reportArgumentType]
        edited=segment.edited,
        hit_count=segment.hit_count,
        error=segment.error,
        child_chunks=[child_out(child) for child in children or []],
        created_at=segment.created_at,
        updated_at=segment.updated_at,
    )


def _check_editable(document: KbDocument) -> None:
    check_writable(document)
    if document.indexing_status not in _EDITABLE:
        raise ServiceError(
            400,
            "document_indexing",
            "색인 중인 문서의 청크는 고칠 수 없다. 끝나거나 일시 정지한 뒤 고친다",
            details={"indexing_status": document.indexing_status},
        )


def clean_chunk(
    text: str, limit: int, loc: tuple[str | int, ...], problems: Problems
) -> tuple[str, int]:
    """항상 하는 정리(NFC·줄바꿈·제어 문자)를 거친 글과 추정 토큰. 분할기와 같은 함수를 써서
    분할기가 상한에 맞춰 만든 청크를 오타만 고쳐도 상한에 걸리지 않는다(M21 검토 2-1, 2-2)."""
    normalized = normalize(text)
    tokens = estimate_tokens(normalized)
    if not normalized.strip():
        problems.add(loc, "content_blank", "내용이 비어 있다")
    elif tokens > limit:
        problems.add(
            loc,
            "content_too_long",
            f"청크는 추정 {limit}토큰 이하여야 한다(지금 {tokens})",
            max_tokens=limit,
            tokens=tokens,
        )
    return normalized, tokens


def _word_count(content: str, answer: str | None) -> int:
    return len(content) + len(answer or "")


def _pending_state(kb: KnowledgeBase, *, fts: bool = True, vector: bool = True) -> dict[str, str]:
    fresh = unit_index_state(kb.indexing_technique)
    return {
        **({"fts": fresh["fts"]} if fts else {}),
        **({"vector": fresh["vector"]} if vector else {}),
    }


class KnowledgeSegmentService:
    def __init__(
        self,
        db: Database,
        *,
        runtime: KnowledgeRuntime | None = None,
        lane: KnowledgeLane | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        # 없으면(index.db를 열지 못함) app.db만 고치고 문서를 레인에 넣는다.
        self._runtime = runtime
        self._lane = lane
        self._clock = clock

    async def list_segments(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        keyword: str | None = None,
        enabled: bool | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> SegmentList:
        offset = (page - 1) * limit
        async with self._db.read() as session:
            kb, document = await live_document(session, workspace_id, kb_id, document_id)
            segments = KbSegmentRepository(session, workspace_id)
            total = await segments.count(document.id, keyword=keyword, enabled=enabled)
            rows = await segments.list_page(
                document.id, keyword=keyword, enabled=enabled, limit=limit, offset=offset
            )
            children = await self._children(session, workspace_id, kb, rows)
        return SegmentList(
            items=[segment_out(row, children.get(row.id)) for row in rows],
            total=total,
            page=page,
            limit=limit,
            has_more=offset + len(rows) < total,
        )

    async def _children(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        kb: KnowledgeBase,
        segments: list[KbSegment],
    ) -> dict[uuid.UUID, list[KbChildChunk]]:
        found: dict[uuid.UUID, list[KbChildChunk]] = {}
        if kb.doc_form != KbDocForm.HIERARCHICAL or not segments:
            return found
        rows = await KbChildChunkRepository(session, workspace_id).list_for_segments(
            [segment.id for segment in segments]
        )
        for row in rows:
            found.setdefault(row.segment_id, []).append(row)
        return found

    async def _segment(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        segment_id: uuid.UUID,
    ) -> tuple[KnowledgeBase, KbDocument, KbSegment]:
        kb, document = await live_document(session, workspace_id, kb_id, document_id)
        segment = await KbSegmentRepository(session, workspace_id).get_in_document(
            document.id, segment_id
        )
        if segment is None:
            raise not_found("청크")
        return kb, document, segment

    async def get_segment(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        segment_id: uuid.UUID,
    ) -> SegmentOut:
        async with self._db.read() as session:
            kb, _, segment = await self._segment(
                session, workspace_id, kb_id, document_id, segment_id
            )
            children = await self._children(session, workspace_id, kb, [segment])
        return segment_out(segment, children.get(segment.id))

    async def create_segment(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        body: SegmentCreate,
    ) -> SegmentOut:
        units: list[PendingUnit] = []
        async with self._db.write() as session:
            kb, document = await live_document(session, workspace_id, kb_id, document_id)
            _check_editable(document)
            problems = Problems()
            rule = parse_process_rule(document.process_rule)
            content, tokens = clean_chunk(
                body.content, chunk_token_limit(kb, rule), ("content",), problems
            )
            answer = self._answer(kb, body.answer, required=True, problems=problems)
            problems.raise_if_any()
            hierarchical = kb.doc_form == KbDocForm.HIERARCHICAL
            pieces = self._child_pieces(kb, rule.child, content) if hierarchical else []
            count = len(pieces) if hierarchical else 1
            numbers = iter(await KbCounterRepository(session).allocate(count))
            segments = KbSegmentRepository(session, workspace_id)
            segment = segments.add(
                KbSegment(
                    id=new_id(),
                    kb_id=kb.id,
                    document_id=document.id,
                    position=await segments.next_position(document.id),
                    unit_no=None if hierarchical else next(numbers),
                    content=content,
                    answer=answer,
                    word_count=_word_count(content, answer),
                    tokens=tokens,
                    content_hash=content_hash(content),
                    enabled=True,
                    status=KbSegmentStatus.WAITING,
                    index_state=(
                        parent_index_state()
                        if hierarchical
                        else unit_index_state(kb.indexing_technique)
                    ),
                    created_by=KbCreatedBy.CUSTOMIZED,
                    edited=False,
                    hit_count=0,
                )
            )
            # 관계(relationship)를 두지 않아 SQLAlchemy가 부모를 먼저 넣는다는 보장이 없다.
            await segments.flush()
            children = KbChildChunkRepository(session, workspace_id)
            for position, (piece, piece_tokens) in enumerate(pieces, start=1):
                child = children.add(
                    KbChildChunk(
                        id=new_id(),
                        kb_id=kb.id,
                        document_id=document.id,
                        segment_id=segment.id,
                        position=position,
                        unit_no=next(numbers),
                        content=piece,
                        tokens=piece_tokens,
                        content_hash=content_hash(piece),
                        index_state=unit_index_state(kb.indexing_technique),
                        created_by=KbCreatedBy.CUSTOMIZED,
                    )
                )
                units.append(unit_of_child(child, True))
            if hierarchical and not pieces:
                segment.status = KbSegmentStatus.COMPLETED
            found = unit_of_segment(segment)
            if found is not None:
                units.append(found)
            await segments.flush()
            await self._refresh_totals(session, workspace_id, kb, document)
        await self._index_now(workspace_id, kb, document, units)
        log.info(
            "knowledge.segment_created", document_id=str(document_id), segment_id=str(segment.id)
        )
        return await self.get_segment(workspace_id, kb_id, document_id, segment.id)

    def _child_pieces(
        self, kb: KnowledgeBase, rule: ChildChunking, content: str
    ) -> list[tuple[str, int]]:
        # 손으로 넣는 부모도 분할과 같은 자식 규칙으로 나눈다(부모만 있으면 검색에 잡히지 않는다).
        config = embedding_config_of(kb)
        chunks = split_general(
            content,
            GeneralRule(tuple(rule.separators), rule.max_tokens, 0),
            input_limit=config.input_token_limit if config is not None else None,
        )
        return [(chunk.content, chunk.tokens) for chunk in chunks]

    def _answer(
        self, kb: KnowledgeBase, answer: str | None, *, required: bool, problems: Problems
    ) -> str | None:
        if kb.doc_form != KbDocForm.QA:
            if answer is not None:
                problems.add(("answer",), "answer_not_allowed", "answer는 Q&A 문서에만 쓴다")
            return None
        if answer is None:
            if required:
                problems.add(("answer",), "answer_required", "Q&A 청크에는 answer가 필요하다")
            return None
        normalized = normalize(answer)
        if not normalized.strip():
            problems.add(("answer",), "content_blank", "내용이 비어 있다")
        return normalized

    async def update_segment(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        segment_id: uuid.UUID,
        patch: SegmentPatch,
    ) -> SegmentOut:
        dropped: dict[int, str] = {}
        stale: dict[int, str] = {}
        async with self._db.write() as session:
            kb, document, segment = await self._segment(
                session, workspace_id, kb_id, document_id, segment_id
            )
            _check_editable(document)
            toggled = patch.enabled is not None and patch.enabled != segment.enabled
            if toggled:
                # 색인이 끝나지 않은 청크는 켜고 끄지 못한다(4.4, Dify 1.18).
                if segment.status != KbSegmentStatus.COMPLETED:
                    raise ServiceError(
                        400,
                        "segment_not_indexed",
                        "색인이 끝난 청크만 켜고 끌 수 있다",
                        details={"status": segment.status},
                    )
            problems = Problems()
            limit = chunk_token_limit(kb, parse_process_rule(document.process_rule))
            content, tokens = segment.content, segment.tokens
            if patch.content is not None:
                content, tokens = clean_chunk(patch.content, limit, ("content",), problems)
            answer = segment.answer
            if patch.answer is not None or kb.doc_form != KbDocForm.QA:
                answer = self._answer(kb, patch.answer, required=False, problems=problems)
            problems.raise_if_any()
            now = self._clock()
            unit_rows = await self._units_of(session, workspace_id, kb, segment)
            if toggled:
                segment.enabled = bool(patch.enabled)
                if not segment.enabled:
                    # 끄면 색인 행을 지운다. 다시 켤 때 색인하도록 pending으로 둔다(캐시에서 복원).
                    for row in unit_rows:
                        if row.unit_no is not None:
                            dropped[row.unit_no] = row.content_hash
                        row.index_state = {**row.index_state, **_pending_state(kb)}
                        row.updated_at = now
            content_changed = content != segment.content
            answer_changed = answer != segment.answer
            if content_changed or answer_changed:
                if content_changed and segment.unit_no is not None:
                    stale[segment.unit_no] = segment.content_hash
                segment.content = content
                segment.answer = answer
                segment.tokens = tokens
                segment.word_count = _word_count(content, answer)
                segment.content_hash = content_hash(content)
                segment.edited = True
                if segment.unit_no is not None:
                    # Q&A의 답만 바뀌면 벡터(질문 임베딩)는 그대로이고 전문 색인만 다시 만든다.
                    segment.index_state = {
                        **segment.index_state,
                        **_pending_state(kb, vector=content_changed),
                    }
                    if content_changed:
                        segment.index_state.update(embedding_version=None, embedded_at=None)
                    segment.status = KbSegmentStatus.WAITING
            segment.updated_at = now
            await KbSegmentRepository(session, workspace_id).flush()
            await self._refresh_totals(session, workspace_id, kb, document)
            units = (
                []
                if not segment.enabled
                else await self._pending_units(session, workspace_id, kb, segment)
            )
        await self._drop(kb, dropped)
        await self._retire_vectors(kb, stale)
        await self._index_now(workspace_id, kb, document, units)
        return await self.get_segment(workspace_id, kb_id, document_id, segment_id)

    async def _units_of(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        kb: KnowledgeBase,
        segment: KbSegment,
    ) -> list[KbSegment] | list[KbChildChunk]:
        if kb.doc_form == KbDocForm.HIERARCHICAL:
            return await KbChildChunkRepository(session, workspace_id).list_for_segments(
                [segment.id]
            )
        return [segment] if segment.unit_no is not None else []

    async def _pending_units(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        kb: KnowledgeBase,
        segment: KbSegment,
    ) -> list[PendingUnit]:
        found: list[PendingUnit] = []
        for row in await self._units_of(session, workspace_id, kb, segment):
            unit = (
                unit_of_segment(row)
                if isinstance(row, KbSegment)
                else unit_of_child(row, segment.enabled)
            )
            if unit is not None and (unit.fts or unit.vector):
                found.append(unit)
        return found

    async def delete_segment(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        segment_id: uuid.UUID,
    ) -> None:
        """색인 행 → 자식 청크·청크 행 순으로 지운다(5.4). 색인 파일이 없으면 고아 정리가 맡는다."""
        async with self._db.read() as session:
            kb, document, segment = await self._segment(
                session, workspace_id, kb_id, document_id, segment_id
            )
            _check_editable(document)
            units = {
                row.unit_no: row.content_hash
                for row in await self._units_of(session, workspace_id, kb, segment)
                if row.unit_no is not None
            }
        await self._drop(kb, units)
        async with self._db.write() as session:
            kb, document, segment = await self._segment(
                session, workspace_id, kb_id, document_id, segment_id
            )
            _check_editable(document)
            await KbChildChunkRepository(session, workspace_id).delete_for_segment(segment.id)
            await KbSegmentRepository(session, workspace_id).delete(segment.id)
            await self._refresh_totals(session, workspace_id, kb, document)
        log.info(
            "knowledge.segment_deleted", document_id=str(document_id), segment_id=str(segment_id)
        )

    async def _refresh_totals(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        kb: KnowledgeBase,
        document: KbDocument,
    ) -> None:
        await refresh_document_totals(session, workspace_id, document, self._clock())
        await KbDocumentRepository(session, workspace_id).flush()
        await KnowledgeBaseRepository(session, workspace_id).refresh_counts(kb.id)

    async def _drop(self, kb: KnowledgeBase, units: dict[int, str]) -> None:
        if not units or self._runtime is None:
            return
        try:
            await drop_units(self._runtime, kb.id, units, embedding_config_of(kb))
        except _INDEX_ERRORS as exc:
            # 남은 행은 매시 고아 정리가 지운다. 검색은 app.db에 없는·꺼진 유닛을 버린다.
            log.warning("knowledge.unit_drop_failed", kb_id=str(kb.id), error=str(exc))

    async def _retire_vectors(self, kb: KnowledgeBase, units: dict[int, str]) -> None:
        """글을 고친 유닛의 옛 벡터 행을 임베딩하기 전에 지운다(옛 벡터는 캐시로).

        검색은 index_state를 보지 않고 index.db 행으로 무엇을 찾을지 정한다. 새 글의 임베딩이
        실패해 문서가 레인으로 넘어가도 그동안 옛 글의 벡터로 검색되지 않게 한다. 되돌리면(편집
        취소) 캐시에서 다시 쓰므로 API를 부르지 않는다. 전문 색인 행은 새 글로 바로 바뀐다.
        """
        config = embedding_config_of(kb)
        if not units or self._runtime is None or config is None:
            return
        try:
            vectors = await self._runtime.vectors.get_many(kb.id, list(units))
            await self._runtime.cache.store(
                config.key(), {units[unit_no]: blob for unit_no, blob in vectors.items()}
            )
            await self._runtime.vectors.delete(kb.id, list(units))
        except _INDEX_ERRORS as exc:
            # 지우지 못한 옛 행은 레인이 그 유닛을 다시 임베딩할 때 새 벡터로 덮인다.
            log.warning("knowledge.unit_retire_failed", kb_id=str(kb.id), error=str(exc))

    async def _index_now(
        self,
        workspace_id: uuid.UUID,
        kb: KnowledgeBase,
        document: KbDocument,
        units: Sequence[PendingUnit],
    ) -> None:
        if not units:
            return
        failed: str | None = "index_unavailable" if self._runtime is None else None
        if self._runtime is not None:
            try:
                report = await UnitIndexer(self._db, self._runtime, kb, clock=self._clock).index(
                    units, priority="interactive"
                )
                if report.failure is not None:
                    failed = report.failure.code
            except _INDEX_ERRORS as exc:
                failed = type(exc).__name__
        async with self._db.write() as session:
            fresh = await KbDocumentRepository(session, workspace_id).get(document.id)
            if fresh is not None:
                await self._refresh_totals(session, workspace_id, kb, fresh)
        if failed is not None:
            await self._queue(workspace_id, document, failed)

    async def _queue(self, workspace_id: uuid.UUID, document: KbDocument, reason: str) -> None:
        """요청 안의 색인이 실패했다. 분할을 마친 문서면 레인에 넣어 pending 유닛만 잇게 한다.

        분할 전에 실패한 문서(파싱 오류)는 넣지 않는다. 다시 돌면 처음부터 나눠 손으로 넣은 청크가
        지워지기 때문이다. 멈춘 문서는 재개할 때 잇는다.
        """
        if document.splitting_completed_at is None:
            log.warning(
                "knowledge.segment_index_deferred", document_id=str(document.id), reason=reason
            )
            return
        async with self._db.write() as session:
            moved = await KbDocumentRepository(session, workspace_id).transition(
                document.id,
                _REQUEUE_FROM,
                {
                    "indexing_status": KbIndexingStatus.WAITING,
                    "completed_at": None,
                    "updated_at": self._clock(),
                },
            )
        if moved and self._lane is not None:
            self._lane.index(workspace_id, document.id)
        log.warning(
            "knowledge.segment_index_queued",
            document_id=str(document.id),
            reason=reason,
            queued=moved,
        )

    async def _parent(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        segment_id: uuid.UUID,
    ) -> tuple[KnowledgeBase, KbDocument, KbSegment]:
        kb, document, segment = await self._segment(
            session, workspace_id, kb_id, document_id, segment_id
        )
        if kb.doc_form != KbDocForm.HIERARCHICAL:
            raise ServiceError(
                400, "not_hierarchical", "자식 청크는 Parent-child 지식 베이스에만 있다"
            )
        return kb, document, segment

    async def list_children(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        segment_id: uuid.UUID,
    ) -> ChildChunkList:
        async with self._db.read() as session:
            _, _, segment = await self._parent(
                session, workspace_id, kb_id, document_id, segment_id
            )
            rows = await KbChildChunkRepository(session, workspace_id).list_for_segments(
                [segment.id]
            )
        return ChildChunkList(items=[child_out(row) for row in rows])

    async def _child_result(
        self, workspace_id: uuid.UUID, segment_id: uuid.UUID, child_id: uuid.UUID
    ) -> ChildChunkOut:
        async with self._db.read() as session:
            child = await KbChildChunkRepository(session, workspace_id).get_in_segment(
                segment_id, child_id
            )
        if child is None:
            raise not_found("자식 청크")
        return child_out(child)

    async def create_child(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        segment_id: uuid.UUID,
        body: ChildChunkCreate,
    ) -> ChildChunkOut:
        async with self._db.write() as session:
            kb, document, segment = await self._parent(
                session, workspace_id, kb_id, document_id, segment_id
            )
            _check_editable(document)
            problems = Problems()
            limit = chunk_token_limit(kb, parse_process_rule(document.process_rule), child=True)
            content, tokens = clean_chunk(body.content, limit, ("content",), problems)
            problems.raise_if_any()
            children = KbChildChunkRepository(session, workspace_id)
            child = children.add(
                KbChildChunk(
                    id=new_id(),
                    kb_id=kb.id,
                    document_id=document.id,
                    segment_id=segment.id,
                    position=await children.next_position(segment.id),
                    unit_no=(await KbCounterRepository(session).allocate(1))[0],
                    content=content,
                    tokens=tokens,
                    content_hash=content_hash(content),
                    index_state=unit_index_state(kb.indexing_technique),
                    created_by=KbCreatedBy.CUSTOMIZED,
                )
            )
            await children.flush()
            await settle_parent(session, workspace_id, segment.id, self._clock())
            await self._refresh_totals(session, workspace_id, kb, document)
            units = [unit_of_child(child, segment.enabled)]
        await self._index_now(workspace_id, kb, document, units)
        return await self._child_result(workspace_id, segment.id, child.id)

    async def update_child(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        segment_id: uuid.UUID,
        child_id: uuid.UUID,
        patch: ChildChunkPatch,
    ) -> ChildChunkOut:
        stale: dict[int, str] = {}
        units: list[PendingUnit] = []
        async with self._db.write() as session:
            kb, document, segment = await self._parent(
                session, workspace_id, kb_id, document_id, segment_id
            )
            _check_editable(document)
            children = KbChildChunkRepository(session, workspace_id)
            child = await children.get_in_segment(segment.id, child_id)
            if child is None:
                raise not_found("자식 청크")
            problems = Problems()
            limit = chunk_token_limit(kb, parse_process_rule(document.process_rule), child=True)
            content, tokens = clean_chunk(patch.content, limit, ("content",), problems)
            problems.raise_if_any()
            if content != child.content:
                stale[child.unit_no] = child.content_hash
                child.content = content
                child.tokens = tokens
                child.content_hash = content_hash(content)
                child.index_state = unit_index_state(kb.indexing_technique)
                child.created_by = KbCreatedBy.CUSTOMIZED
                child.updated_at = self._clock()
                await children.flush()
                await settle_parent(session, workspace_id, segment.id, self._clock())
                await self._refresh_totals(session, workspace_id, kb, document)
                units.append(unit_of_child(child, segment.enabled))
        await self._retire_vectors(kb, stale)
        await self._index_now(workspace_id, kb, document, units)
        return await self._child_result(workspace_id, segment.id, child_id)

    async def delete_child(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        segment_id: uuid.UUID,
        child_id: uuid.UUID,
    ) -> None:
        async with self._db.read() as session:
            kb, document, segment = await self._parent(
                session, workspace_id, kb_id, document_id, segment_id
            )
            _check_editable(document)
            child = await KbChildChunkRepository(session, workspace_id).get_in_segment(
                segment.id, child_id
            )
            if child is None:
                raise not_found("자식 청크")
        await self._drop(kb, {child.unit_no: child.content_hash})
        async with self._db.write() as session:
            kb, document, segment = await self._parent(
                session, workspace_id, kb_id, document_id, segment_id
            )
            _check_editable(document)
            children = KbChildChunkRepository(session, workspace_id)
            if await children.get_in_segment(segment.id, child_id) is None:
                raise not_found("자식 청크")
            await children.delete(child_id)
            await settle_parent(session, workspace_id, segment.id, self._clock())
            await self._refresh_totals(session, workspace_id, kb, document)
