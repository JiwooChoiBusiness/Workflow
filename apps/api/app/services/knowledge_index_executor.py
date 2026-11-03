"""지식 레인의 실행 함수와 유닛 색인(3단계 설계 5.4, 6.1, 6.5, 6.6).

세 번째 Supervisor(name="knowledge", 동시 1)가 큐 키 index:<워크스페이스>:<문서>와
delete:<워크스페이스>:<문서>를 KnowledgeIndexExecutor.run으로 돌린다. 문서 작업의 순서와 멈춤 규칙은
app/knowledge/ingest/job.py(순수)가 정하고 여기서는 그 포트를 app.db·index.db·저장소·샌드박스로
구현한다.

유닛을 index.db에 쓰는 순서는 늘 같다(5.4). app.db에 pending을 적고 → index.db에 쓰고 → app.db에
ok를 적는다. ok는 그사이 글이 바뀌지 않았을 때만(content_hash가 같을 때만) 적는다. 청크 편집과
레인 작업이 같은 UnitIndexer를 쓴다.
"""

import asyncio
import contextlib
import re
import shutil
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import Database
from app.db.models import KbChildChunk, KbDocument, KbSegment, KnowledgeBase
from app.db.models.enums import (
    KbCreatedBy,
    KbDocForm,
    KbIndexingStatus,
    KbIndexingTechnique,
    KbSegmentStatus,
)
from app.db.repositories.knowledge import (
    IN_PROGRESS_STATUSES,
    KbChildChunkRepository,
    KbCounterRepository,
    KbDocumentMetadataRepository,
    KbDocumentRepository,
    KbEvalCaseRetentionRepository,
    KbEvalRetentionRepository,
    KbMetadataFieldRepository,
    KbQueryLogRetentionRepository,
    KbSegmentRepository,
    KnowledgeBaseRepository,
    UnitState,
)
from app.db.repositories.workspaces import WorkspaceRepository
from app.db.types import new_id, utcnow
from app.knowledge.chunking import Chunk
from app.knowledge.index_db import IndexUnavailable, max_rowid, table_exists
from app.knowledge.ingest.job import (
    RUNNING,
    WAITING,
    BatchOutcome,
    JobDocument,
    JobFailed,
    Stage,
    run_ingest,
)
from app.knowledge.ingest.parse import (
    SANDBOX_KINDS,
    SUFFIXES,
    SourceText,
    cut_utf8,
    extract_in_sandbox,
    file_kind,
    read_local_text,
)
from app.knowledge.ingest.scanned_pdf import (
    SandboxPieceStore,
    ScannedPdfError,
    model_piece_reader,
    read_with_model,
)
from app.knowledge.units import fts_table_name, vec_table_name
from app.llm.base import ModelAdapter
from app.llm.embeddings import EmbeddingConfig, EmbeddingError, content_hash
from app.llm.key_pool import KeyTier
from app.runner.supervisor import CancelSignal, Supervisor, SupervisorClosed
from app.sandbox.base import SandboxBackend, SandboxError, WorkspaceHandle
from app.services.knowledge import (
    parent_index_state,
    parse_process_rule,
    split_rules,
    unit_index_state,
)
from app.services.knowledge_runtime import (
    IndexUnit,
    KnowledgeRuntime,
    drop_units,
    embedding_config_of,
    key_tier_of,
    write_fts,
    write_vectors,
)
from app.storage.base import StorageBackend, StorageError
from app.workflow.extract_runner import ExtractError, ensure_parsers

log = structlog.get_logger("app.services.knowledge")

JobKind = Literal["index", "delete"]
MB = 1024 * 1024
# 유닛 행을 한 번에 읽는 쪽 크기와 삭제·고아 정리의 묶음(4.4, 5.4).
UNIT_PAGE = 200
DELETE_BATCH = 1000
STATE_PAGE = 10_000
EXTRACT_CACHE = ("cache", "wf-extract")
KB_WORK = "kb-work"
# 작업이 끝나면 바로 지우지만, 죽은 작업의 폴더는 이만큼 지나면 매시 정리가 지운다(4.5).
WORK_DIR_KEEP = timedelta(hours=1)
# 문서 행 없이 남은 원본 파일은 이만큼 지나 지운다(4.5).
LOOSE_FILE_KEEP = timedelta(days=7)
EMBED_CACHE_MAX_ROWS = 50_000
# 끝까지 본 뒤 pending이 남았을 때 처음부터 다시 보는 횟수. 끝없이 돌지 않게 묶는다.
MAX_RESCANS = 2
EVAL_RUNS_KEEP = 30
_INDEX_TABLE = re.compile(r"^(vec|fts)_[0-9a-f]{32}$")


def index_key(workspace_id: uuid.UUID, document_id: uuid.UUID) -> str:
    return f"index:{workspace_id}:{document_id}"


def delete_key(workspace_id: uuid.UUID, document_id: uuid.UUID) -> str:
    return f"delete:{workspace_id}:{document_id}"


def parse_job_key(key: str) -> tuple[JobKind, uuid.UUID, uuid.UUID]:
    kind, workspace, document = key.split(":")
    if kind not in ("index", "delete"):
        raise ValueError(f"알 수 없는 지식 작업: {key}")
    return ("index" if kind == "index" else "delete"), uuid.UUID(workspace), uuid.UUID(document)


class KnowledgeLane:
    """지식 레인(세 번째 Supervisor)에 넣고 빼는 창구. 같은 문서가 큐에 두 번 들어가지 않게 한다."""

    def __init__(self, supervisor: Supervisor) -> None:
        self.supervisor = supervisor

    def index(self, workspace_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        key = index_key(workspace_id, document_id)
        if self.supervisor.position(key) is not None:
            return False
        try:
            # 실행 중이어도 넣는다. 지금 작업이 멈춤·취소로 빠지는 중일 수 있고 새 항목은 그
            # 뒤에 돈다.
            self.supervisor.enqueue(key)
        except SupervisorClosed:
            return False
        return True

    def busy(self, workspace_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        key = index_key(workspace_id, document_id)
        return self.supervisor.position(key) is not None or self.supervisor.is_running(key)

    def stop(self, workspace_id: uuid.UUID, document_id: uuid.UUID, reason: str) -> None:
        """큐에 있으면 빼고 실행 중이면 취소한다. 작업은 다음 경계에서 나간다."""
        key = index_key(workspace_id, document_id)
        while self.supervisor.dequeue(key):
            pass
        self.supervisor.cancel(key, reason)

    def delete(self, workspace_id: uuid.UUID, document_id: uuid.UUID) -> None:
        self.stop(workspace_id, document_id, "delete")
        key = delete_key(workspace_id, document_id)
        if self.supervisor.position(key) is None and not self.supervisor.is_running(key):
            with contextlib.suppress(SupervisorClosed):
                self.supervisor.enqueue(key)


def _now_text(now: datetime) -> str:
    return now.isoformat()


def _unit_done(state: Mapping[str, Any]) -> bool:
    return state.get("fts") in ("ok", "skip") and state.get("vector") in ("ok", "skip", "error")


def segment_status(state: Mapping[str, Any]) -> str:
    """유닛인 세그먼트(General·Q&A)의 status. 벡터 400은 error다."""
    if state.get("vector") == "error":
        return KbSegmentStatus.ERROR
    return KbSegmentStatus.COMPLETED if _unit_done(state) else KbSegmentStatus.WAITING


@dataclass(frozen=True)
class PendingUnit:
    row_id: uuid.UUID
    parent_id: uuid.UUID | None
    unit: IndexUnit
    fts: bool
    vector: bool
    enabled: bool = True


def unit_of_segment(segment: KbSegment) -> PendingUnit | None:
    if segment.unit_no is None:
        return None
    state = segment.index_state or {}
    return PendingUnit(
        row_id=segment.id,
        parent_id=None,
        unit=IndexUnit(
            unit_no=segment.unit_no,
            document_id=segment.document_id,
            kind="segment",
            text=segment.content,
            body=f"{segment.content}\n{segment.answer}" if segment.answer else segment.content,
            content_hash=segment.content_hash,
        ),
        fts=state.get("fts") == "pending",
        vector=state.get("vector") == "pending",
        enabled=segment.enabled,
    )


def unit_of_child(child: KbChildChunk, enabled: bool) -> PendingUnit:
    state = child.index_state or {}
    return PendingUnit(
        row_id=child.id,
        parent_id=child.segment_id,
        unit=IndexUnit(
            unit_no=child.unit_no,
            document_id=child.document_id,
            kind="child",
            text=child.content,
            body=child.content,
            content_hash=child.content_hash,
        ),
        fts=state.get("fts") == "pending",
        vector=state.get("vector") == "pending",
        enabled=enabled,
    )


@dataclass
class IndexReport:
    fts: int = 0
    vectors: int = 0
    errors: int = 0
    failure: EmbeddingError | None = None


def job_failure(error: EmbeddingError) -> JobFailed:
    return JobFailed(
        error.code, error.message, retryable=error.retryable, retry_after=error.retry_after
    )


class UnitIndexer:
    """유닛 묶음을 index.db에 쓰고 app.db에 ok를 적는다. 청크 편집과 레인이 같이 쓴다."""

    def __init__(
        self,
        db: Database,
        runtime: KnowledgeRuntime,
        kb: KnowledgeBase,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._runtime = runtime
        self._kb = kb
        self._clock = clock
        self.config = embedding_config_of(kb)
        self.tier: KeyTier = key_tier_of(kb)

    @property
    def hierarchical(self) -> bool:
        return self._kb.doc_form == KbDocForm.HIERARCHICAL

    async def index(
        self, units: Sequence[PendingUnit], *, priority: Literal["interactive", "bulk"]
    ) -> IndexReport:
        report = IndexReport()
        targets = [unit for unit in units if unit.enabled]
        fts_units = [unit for unit in targets if unit.fts]
        if fts_units:
            await write_fts(self._runtime, self._kb, [unit.unit for unit in fts_units])
            await self._mark(fts_units, fts_ok=True)
            report.fts = len(fts_units)
        vector_units = [unit for unit in targets if unit.vector]
        if not vector_units:
            return report
        config = self.config
        if config is None:
            report.failure = EmbeddingError(
                "embedding_invalid", "지식 베이스의 임베딩 설정을 읽지 못했다", retryable=False
            )
            return report
        result = await self._runtime.embeddings.embed_documents(
            [unit.unit.text for unit in vector_units], config, tier=self.tier, priority=priority
        )
        done = [
            (unit, vector)
            for unit, vector in zip(vector_units, result.vectors, strict=True)
            if vector is not None
        ]
        if done:
            await write_vectors(
                self._runtime, self._kb, config, [(unit.unit, vector) for unit, vector in done]
            )
        failed = {vector_units[position]: message for position, message in result.errors.items()}
        await self._mark([unit for unit, _ in done], vector_ok=config, errors=failed)
        if result.api_tokens:
            async with self._db.write() as session:
                await KnowledgeBaseRepository(session, self._kb.workspace_id).add_embedded_tokens(
                    self._kb.id, result.api_tokens
                )
        report.vectors = len(done)
        report.errors = len(failed)
        report.failure = result.failure
        return report

    async def _mark(
        self,
        units: Sequence[PendingUnit],
        *,
        fts_ok: bool = False,
        vector_ok: EmbeddingConfig | None = None,
        errors: Mapping[PendingUnit, str] | None = None,
    ) -> None:
        errors = errors or {}
        everything = [*units, *errors]
        if not everything:
            return
        now = self._clock()
        workspace_id = self._kb.workspace_id
        async with self._db.write() as session:
            if vector_ok is not None:
                # 임베딩하는 사이 KB 색인 설정이 바뀌었으면(재임베딩 요청) 옛 설정의 벡터에 ok를
                # 적지 않는다. 새 작업이 새 테이블에 다시 채운다.
                current = await KnowledgeBaseRepository(session, workspace_id).get_active(
                    self._kb.id
                )
                if current is None or embedding_config_of(current) != vector_ok:
                    return
            segments = KbSegmentRepository(session, workspace_id)
            children = KbChildChunkRepository(session, workspace_id)
            rows: dict[uuid.UUID, KbSegment | KbChildChunk] = {}
            child_ids = [unit.row_id for unit in everything if unit.parent_id is not None]
            segment_ids = [unit.row_id for unit in everything if unit.parent_id is None]
            rows.update((row.id, row) for row in await segments.get_many(segment_ids))
            rows.update((row.id, row) for row in await children.get_many(child_ids))
            parents: set[uuid.UUID] = set()
            for unit in everything:
                row = rows.get(unit.row_id)
                if row is None or row.content_hash != unit.unit.content_hash:
                    # 지웠거나 그사이 글이 바뀌었다. 바뀐 쪽이 pending을 적어 두었으니 그대로 둔다.
                    continue
                state = dict(row.index_state or {})
                if fts_ok:
                    state["fts"] = "ok"
                if unit in errors:
                    state.update(vector="error", embedding_version=None, embedded_at=None)
                    state["error"] = errors[unit][:500]
                elif vector_ok is not None:
                    state.update(
                        vector="ok",
                        embedding_version=vector_ok.version,
                        embedded_at=_now_text(now),
                    )
                    state.pop("error", None)
                row.index_state = state
                row.updated_at = now
                if isinstance(row, KbSegment):
                    row.status = segment_status(state)
                    row.error = (
                        {"code": "model_request_invalid", "message": state["error"]}
                        if state.get("vector") == "error"
                        else None
                    )
                else:
                    parents.add(row.segment_id)
            await children.flush()
            await self._settle_parents(session, parents, now)

    async def _settle_parents(
        self, session: AsyncSession, parent_ids: Collection[uuid.UUID], now: datetime
    ) -> None:
        # Parent-child의 부모는 자식이 모두 끝나야 completed다.
        if not parent_ids:
            return
        workspace_id = self._kb.workspace_id
        segments = KbSegmentRepository(session, workspace_id)
        children = await KbChildChunkRepository(session, workspace_id).list_for_segments(
            list(parent_ids)
        )
        by_parent: dict[uuid.UUID, list[KbChildChunk]] = {}
        for child in children:
            by_parent.setdefault(child.segment_id, []).append(child)
        for parent in await segments.get_many(list(parent_ids)):
            done = all(
                _unit_done(child.index_state or {}) for child in by_parent.get(parent.id, [])
            )
            status = KbSegmentStatus.COMPLETED if done else KbSegmentStatus.WAITING
            if parent.status != status:
                parent.status = status
                parent.updated_at = now
        await segments.flush()


async def settle_parent(
    session: AsyncSession, workspace_id: uuid.UUID, parent_id: uuid.UUID, now: datetime
) -> None:
    segments = KbSegmentRepository(session, workspace_id)
    parent = await segments.get(parent_id)
    if parent is None:
        return
    children = await KbChildChunkRepository(session, workspace_id).list_for_segments([parent_id])
    done = all(_unit_done(child.index_state or {}) for child in children)
    parent.status = KbSegmentStatus.COMPLETED if done else KbSegmentStatus.WAITING
    parent.updated_at = now


async def refresh_document_totals(
    session: AsyncSession, workspace_id: uuid.UUID, document: KbDocument, now: datetime
) -> None:
    total, completed, words, tokens = await KbSegmentRepository(
        session, workspace_id
    ).document_totals(document.id)
    document.total_segments = total
    document.completed_segments = completed
    document.word_count = words
    document.tokens = tokens
    document.updated_at = now


class _IngestPort:
    """job.IngestPort 구현. 작업 하나(문서 하나, 한 번 실행)마다 만든다."""

    def __init__(
        self, executor: "KnowledgeIndexExecutor", workspace_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        self._executor = executor
        self._db = executor.db
        self._runtime = executor.runtime
        self._workspace_id = workspace_id
        self._document_id = document_id
        self._document: KbDocument | None = None
        self._kb: KnowledgeBase | None = None
        self._indexer: UnitIndexer | None = None
        self._cursor = 0
        self._rescans = 0
        self._position = 0
        self._handle: WorkspaceHandle | None = None

    @property
    def document(self) -> KbDocument:
        if self._document is None:
            raise RuntimeError("load() 전이다")
        return self._document

    @property
    def kb(self) -> KnowledgeBase:
        if self._kb is None:
            raise RuntimeError("load() 전이다")
        return self._kb

    @property
    def indexer(self) -> UnitIndexer:
        if self._indexer is None:
            raise RuntimeError("load() 전이다")
        return self._indexer

    def _documents(self, session: AsyncSession) -> KbDocumentRepository:
        return KbDocumentRepository(session, self._workspace_id)

    async def load(self) -> JobDocument | None:
        async with self._db.read() as session:
            document = await self._documents(session).get(self._document_id)
            if document is None or document.deleted_at is not None:
                return None
            # 지운 KB의 문서는 집지 않는다. 남은 행은 매시 정리가 지운다(M21 검토 2-9).
            kb = await KnowledgeBaseRepository(session, self._workspace_id).get_active(
                document.kb_id
            )
        if kb is None:
            return None
        self._document, self._kb = document, kb
        self._indexer = UnitIndexer(self._db, self._runtime, kb, clock=self._executor.clock)
        config = self._indexer.config
        rules = split_rules(
            parse_process_rule(document.process_rule),
            kb.doc_form,
            input_limit=config.input_token_limit if config is not None else None,
        )
        return JobDocument(
            status=document.indexing_status,
            split_done=document.splitting_completed_at is not None,
            economy=kb.indexing_technique == KbIndexingTechnique.ECONOMY,
            rules=rules,
        )

    async def advance(
        self,
        stage: Stage,
        *,
        expected: Collection[str],
        extract: dict[str, Any] | None = None,
        split_done: bool = False,
    ) -> bool:
        now = self._executor.clock()
        values: dict[str, Any] = {"indexing_status": stage, "updated_at": now}
        if stage == "parsing":
            values.update(
                processing_started_at=now,
                parsing_completed_at=None,
                cleaning_completed_at=None,
                splitting_completed_at=None,
                completed_at=None,
                error=None,
            )
        elif stage == "cleaning":
            values["parsing_completed_at"] = now
        elif stage == "splitting":
            values["cleaning_completed_at"] = now
        elif stage == "indexing":
            values["error"] = None
        elif stage == "completed":
            values.update(completed_at=now, error=None)
        if split_done:
            values["splitting_completed_at"] = now
        if extract is not None:
            values["extract"] = extract
        async with self._db.write() as session:
            documents = self._documents(session)
            changed = await documents.transition(self._document_id, expected, values)
            if changed and (split_done or stage == "completed"):
                document = await documents.get(self._document_id)
                if document is not None:
                    await refresh_document_totals(session, self._workspace_id, document, now)
                    await KnowledgeBaseRepository(session, self._workspace_id).refresh_counts(
                        document.kb_id
                    )
        log.info(
            "knowledge.document_stage",
            document_id=str(self._document_id),
            stage=stage,
            changed=changed,
        )
        return changed

    async def clear_units(self) -> None:
        """분할을 마치지 않은 문서의 부분 결과를 지운다(재기동·재색인). index.db 행이 먼저다."""
        await drop_document_index(
            self._db, self._runtime, self._workspace_id, self.kb, self._document_id
        )
        await delete_document_rows(self._db, self._workspace_id, self._document_id)
        async with self._db.write() as session:
            document = await self._documents(session).get(self._document_id)
            if document is not None:
                await refresh_document_totals(
                    session, self._workspace_id, document, self._executor.clock()
                )
        self._cursor = 0
        self._position = 0

    async def read_source(self) -> SourceText:
        document = self.document
        executor = self._executor
        kind = file_kind({"filename": document.filename, "mime": document.mime})
        if kind is None:
            raise JobFailed(
                "unsupported_file_type", f"{document.filename}은(는) 읽을 수 없는 형식이다"
            )
        budget = executor.settings.kb_doc_max_text_mb * MB
        try:
            path = executor.storage.local_path(document.storage_key)
        except StorageError as exc:
            raise JobFailed("file_missing", f"원본 파일을 찾지 못했다: {exc}") from None
        if not await asyncio.to_thread(path.is_file):
            raise JobFailed("file_missing", "원본 파일이 없다. 문서를 다시 올린다")
        if kind not in SANDBOX_KINDS:
            try:
                return await read_local_text(path, kind, budget)
            except OSError as exc:
                raise JobFailed("extract_failed", f"파일을 읽지 못했다: {exc}") from None
        handle = await self._work_handle()
        relative = f"source{SUFFIXES[kind]}"
        try:
            await asyncio.to_thread(shutil.copyfile, path, handle.root / relative)
            prefix = f"kb-{self._document_id.hex[-8:]}"
            mode = (document.extract or {}).get("mode")
            cache = executor.settings.data_dir.joinpath(*EXTRACT_CACHE)
            if kind == "pdf" and mode == "model":
                return await self._read_with_model(handle, relative, budget, cache, prefix)
            return await extract_in_sandbox(
                executor.sandbox,
                handle,
                relative=relative,
                kind=kind,
                budget=budget,
                cache_dir=cache,
                exec_prefix=prefix,
            )
        except ExtractError as exc:
            raise JobFailed(exc.code, exc.message, retryable=exc.retryable) from None
        except ScannedPdfError as exc:
            raise JobFailed(exc.code, exc.message, retryable=exc.retryable) from None
        except (OSError, SandboxError) as exc:
            raise JobFailed(
                "extract_failed", f"파일을 옮기지 못했다: {exc}", retryable=True
            ) from None

    async def _read_with_model(
        self, handle: WorkspaceHandle, relative: str, budget: int, cache: Path, prefix: str
    ) -> SourceText:
        executor = self._executor
        lib = await ensure_parsers(executor.sandbox, handle, cache, exec_id=f"{prefix}-pip")
        store = SandboxPieceStore(
            executor.sandbox, handle, lib=lib, source=relative, exec_prefix=prefix
        )
        pages = await store.page_count()
        reader = model_piece_reader(
            executor.llm,
            tier=self.indexer.tier,
            metadata={"document_id": str(self._document_id)},
        )
        result = await read_with_model(pages, store, reader)
        text, cut = cut_utf8(result.text, budget)
        return SourceText(
            text=text,
            kind="pdf",
            mode="model",
            pages=pages,
            truncated=cut,
            warnings=tuple(result.warnings),
        )

    async def _work_handle(self) -> WorkspaceHandle:
        if self._handle is None:
            try:
                self._handle = await self._executor.sandbox.acquire(self._document_id)
            except SandboxError as exc:
                raise JobFailed(exc.code, exc.message, retryable=True) from None
        return self._handle

    async def write_chunks(self, chunks: Sequence[Chunk]) -> None:
        kb = self.kb
        hierarchical = kb.doc_form == KbDocForm.HIERARCHICAL
        count = sum(len(chunk.children) for chunk in chunks) if hierarchical else len(chunks)
        units: list[PendingUnit] = []
        parents: list[tuple[KbSegment, Chunk]] = []
        now = self._executor.clock()
        async with self._db.write() as session:
            numbers = iter(await KbCounterRepository(session).allocate(count))
            segments = KbSegmentRepository(session, self._workspace_id)
            children = KbChildChunkRepository(session, self._workspace_id)
            for chunk in chunks:
                self._position += 1
                segment = segments.add(
                    KbSegment(
                        id=new_id(),
                        kb_id=kb.id,
                        document_id=self._document_id,
                        position=self._position,
                        unit_no=None if hierarchical else next(numbers),
                        content=chunk.content,
                        answer=chunk.answer,
                        word_count=len(chunk.content) + len(chunk.answer or ""),
                        tokens=chunk.tokens,
                        content_hash=content_hash(chunk.content),
                        enabled=True,
                        status=(
                            KbSegmentStatus.COMPLETED
                            if hierarchical and not chunk.children
                            else KbSegmentStatus.WAITING
                        ),
                        index_state=(
                            parent_index_state()
                            if hierarchical
                            else unit_index_state(kb.indexing_technique)
                        ),
                        created_by=KbCreatedBy.AUTOMATIC,
                        edited=False,
                        hit_count=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                if not hierarchical:
                    found = unit_of_segment(segment)
                    if found is not None:
                        units.append(found)
                    continue
                parents.append((segment, chunk))
            # 관계(relationship)를 두지 않아 SQLAlchemy가 부모를 먼저 넣는다는 보장이 없다.
            await segments.flush()
            for segment, chunk in parents:
                for position, piece in enumerate(chunk.children, start=1):
                    child = children.add(
                        KbChildChunk(
                            id=new_id(),
                            kb_id=kb.id,
                            document_id=self._document_id,
                            segment_id=segment.id,
                            position=position,
                            unit_no=next(numbers),
                            content=piece.content,
                            tokens=piece.tokens,
                            content_hash=content_hash(piece.content),
                            index_state=unit_index_state(kb.indexing_technique),
                            created_by=KbCreatedBy.AUTOMATIC,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    units.append(unit_of_child(child, True))
            await segments.flush()
        # 전문 색인은 분할하면서 바로 쓴다. 벡터가 차기 전에도 전문 검색이 된다(6.7).
        report = await self.indexer.index(
            [PendingUnit(u.row_id, u.parent_id, u.unit, fts=True, vector=False) for u in units],
            priority="bulk",
        )
        log.debug("knowledge.chunks_written", segments=len(chunks), fts=report.fts)

    async def index_batch(self) -> BatchOutcome:
        indexer = self.indexer
        config = indexer.config
        if self.kb.indexing_technique == KbIndexingTechnique.HIGH_QUALITY and config is None:
            return BatchOutcome(
                failure=JobFailed("embedding_invalid", "지식 베이스의 임베딩 설정을 읽지 못했다")
            )
        if await self._settings_changed():
            return BatchOutcome(stopped=True)
        limit = self._runtime.limiter.batch_items(indexer.tier, "bulk") if config else UNIT_PAGE
        units, finished = await self._next_units(limit)
        if units:
            try:
                report = await indexer.index(units, priority="bulk")
            except (IndexUnavailable, LookupError, ValueError) as exc:
                return BatchOutcome(
                    failure=JobFailed("index_write_failed", str(exc), retryable=True)
                )
            await self._refresh_progress()
            if report.failure is not None:
                return BatchOutcome(failure=job_failure(report.failure))
        return BatchOutcome(done=finished and not units)

    async def _settings_changed(self) -> bool:
        # 작업 도중 KB를 지웠거나 색인 설정(임베딩)이 바뀌었으면 새 작업이 잇는다.
        async with self._db.read() as session:
            kb = await KnowledgeBaseRepository(session, self._workspace_id).get_active(self.kb.id)
        return kb is None or embedding_config_of(kb) != self.indexer.config

    async def _next_units(self, limit: int) -> tuple[list[PendingUnit], bool]:
        """커서 다음의 pending 유닛. 벡터가 limit개 모이면 멈춘다. (유닛, 끝까지 봤는지)."""
        collected: list[PendingUnit] = []
        vectors = 0
        while True:
            page = await self._load_page()
            if not page and not collected and await self._rescan():
                continue
            if not page:
                return collected, True
            for unit in page:
                self._cursor = unit.unit.unit_no
                # 끈 청크는 색인하지 않는다. 다시 켤 때 색인한다(M21 검토 2-12).
                if not unit.enabled or not (unit.fts or unit.vector):
                    continue
                collected.append(unit)
                vectors += unit.vector
                if vectors >= limit:
                    return collected, False
            if len(collected) >= UNIT_PAGE:
                return collected, False

    async def _rescan(self) -> bool:
        """끝까지 본 뒤 커서 앞에 pending이 생겼으면(그사이 청크 편집 실패·설정 변경) 처음부터
        한 번 더 본다. 이것 없이 completed를 적으면 그 유닛은 아무도 집지 않는다."""
        if self._rescans >= MAX_RESCANS:
            return False
        async with self._db.read() as session:
            waiting = await KbSegmentRepository(session, self._workspace_id).has_waiting(
                self._document_id
            )
        if not waiting:
            return False
        self._rescans += 1
        self._cursor = 0
        return True

    async def _load_page(self) -> list[PendingUnit]:
        async with self._db.read() as session:
            if self.kb.doc_form == KbDocForm.HIERARCHICAL:
                rows = await KbChildChunkRepository(session, self._workspace_id).units_after(
                    self._document_id, self._cursor, UNIT_PAGE
                )
                return [unit_of_child(child, enabled) for child, enabled in rows]
            segments = await KbSegmentRepository(session, self._workspace_id).units_after(
                self._document_id, self._cursor, UNIT_PAGE
            )
        return [unit for unit in map(unit_of_segment, segments) if unit is not None]

    async def _refresh_progress(self) -> None:
        async with self._db.write() as session:
            document = await self._documents(session).get(self._document_id)
            if document is not None and document.deleted_at is None:
                await refresh_document_totals(
                    session, self._workspace_id, document, self._executor.clock()
                )

    async def fail(self, error: JobFailed) -> None:
        now = self._executor.clock()
        payload: dict[str, Any] = {
            "code": error.code,
            "message": error.message[:1000],
            "retryable": error.retryable,
        }
        if error.retry_after is not None:
            payload["retry_after"] = (now + timedelta(seconds=error.retry_after)).isoformat()
        async with self._db.write() as session:
            documents = self._documents(session)
            changed = await documents.transition(
                self._document_id,
                (WAITING, *RUNNING),
                {"indexing_status": KbIndexingStatus.ERROR, "error": payload, "updated_at": now},
            )
            document = await documents.get(self._document_id) if changed else None
            if document is not None:
                await refresh_document_totals(session, self._workspace_id, document, now)
        log.warning(
            "knowledge.document_failed",
            document_id=str(self._document_id),
            code=error.code,
            retryable=error.retryable,
            retry_after=payload.get("retry_after"),
            changed=changed,
        )

    async def finish(self) -> None:
        kb = self.kb
        try:
            if await self._has_fts(kb.id):
                await self._runtime.fts.refresh_stop_bigrams(kb.id)
                await self._runtime.index_db.refresh_unit_count(kb.id)
        except IndexUnavailable as exc:
            log.warning("knowledge.finish_skipped", error=str(exc))

    async def _has_fts(self, kb_id: uuid.UUID) -> bool:
        async with self._runtime.index_db.read() as conn:
            return await table_exists(conn, fts_table_name(kb_id))

    async def cleanup(self) -> None:
        if self._handle is None:
            return
        try:
            await self._executor.sandbox.release(self._handle, delete=True)
        except SandboxError as exc:
            log.warning("knowledge.work_dir_delete_failed", error=exc.message)


async def drop_document_index(
    db: Database,
    runtime: KnowledgeRuntime,
    workspace_id: uuid.UUID,
    kb: KnowledgeBase,
    document_id: uuid.UUID,
) -> int:
    """문서의 유닛 색인 행을 1,000개씩 지운다. 벡터는 embed_cache로 옮긴다."""
    if not runtime.available:
        return 0
    config = embedding_config_of(kb)
    dropped = 0
    for repository in (KbSegmentRepository, KbChildChunkRepository):
        after = 0
        while True:
            async with db.read() as session:
                pairs = await repository(session, workspace_id).unit_hashes(
                    document_id, after, DELETE_BATCH
                )
            if not pairs:
                break
            await drop_units(runtime, kb.id, dict(pairs), config)
            dropped += len(pairs)
            after = pairs[-1][0]
    return dropped


async def delete_document_rows(
    db: Database, workspace_id: uuid.UUID, document_id: uuid.UUID
) -> int:
    """자식 청크 → 세그먼트를 1,000행씩 지운다. 쓰기 잠금을 오래 잡지 않으려는 묶음이다(4.4)."""
    deleted = 0
    for repository in (KbChildChunkRepository, KbSegmentRepository):
        while True:
            async with db.write() as session:
                count = await repository(session, workspace_id).delete_some_for_document(
                    document_id, DELETE_BATCH
                )
            deleted += count
            if count < DELETE_BATCH:
                break
    return deleted


@dataclass
class KnowledgeIndexExecutor:
    """지식 레인의 실행 함수(Supervisor의 run_fn)."""

    db: Database
    settings: Settings
    runtime: KnowledgeRuntime
    storage: StorageBackend
    # DATA_DIR/kb-work의 세 번째 로컬 백엔드. 작업 폴더는 문서 id다(6.2).
    sandbox: SandboxBackend
    llm: ModelAdapter
    clock: Callable[[], datetime] = utcnow
    lane: KnowledgeLane | None = field(default=None)

    async def run(self, job_key: str, cancel: CancelSignal) -> None:
        kind, workspace_id, document_id = parse_job_key(job_key)
        if not self.runtime.available:
            # 색인 파일이 없으면 문서는 지금 상태로 남고 다음 기동의 복구가 다시 넣는다.
            log.warning("knowledge.job_skipped", job=job_key, reason="index_unavailable")
            return
        if kind == "delete":
            await self.delete_document(workspace_id, document_id, cancel)
            return
        port = _IngestPort(self, workspace_id, document_id)
        try:
            result = await run_ingest(port, cancel)
        finally:
            await port.cleanup()
        log.info(
            "knowledge.job_finished",
            document_id=str(document_id),
            result=result,
            cancel=cancel.reason,
        )

    async def delete_document(
        self, workspace_id: uuid.UUID, document_id: uuid.UUID, cancel: CancelSignal
    ) -> bool:
        """지운 문서의 색인 행 → 청크 행 → 원본 → 문서 행 순으로 지운다(4.4). 끝까지 했으면 True."""
        async with self.db.read() as session:
            document = await KbDocumentRepository(session, workspace_id).get(document_id)
            if document is None or document.deleted_at is None:
                return False
            kb = await KnowledgeBaseRepository(session, workspace_id).get(document.kb_id)
        if kb is None:
            return False
        await drop_document_index(self.db, self.runtime, workspace_id, kb, document_id)
        if cancel.is_set():
            return False
        await delete_document_rows(self.db, workspace_id, document_id)
        if cancel.is_set():
            return False
        try:
            await self.storage.delete(document.storage_key)
        except (OSError, StorageError) as exc:
            # 행 없이 남은 파일은 매시 정리가 7일 뒤 지운다.
            log.warning("knowledge.file_delete_failed", key=document.storage_key, error=str(exc))
        async with self.db.write() as session:
            await KbDocumentMetadataRepository(session, workspace_id).delete_for_document(
                document_id
            )
            await KbDocumentRepository(session, workspace_id).delete(document_id)
            await KnowledgeBaseRepository(session, workspace_id).refresh_counts(kb.id)
        if self.runtime.available:
            with contextlib.suppress(IndexUnavailable):
                await self.runtime.index_db.refresh_unit_count(kb.id)
        log.info("knowledge.document_purged", document_id=str(document_id))
        return True


@dataclass
class KnowledgeMaintenance:
    """기동 정합 검사와 매시 정리(5.4, 5.5, 4.5). 레인 작업과 같은 쓰기 순서를 지킨다."""

    db: Database
    runtime: KnowledgeRuntime
    settings: Settings
    storage: StorageBackend
    sandbox: SandboxBackend
    lane: KnowledgeLane | None = None
    clock: Callable[[], datetime] = utcnow

    async def _workspace_ids(self) -> list[uuid.UUID]:
        async with self.db.read() as session:
            return [row.id for row in await WorkspaceRepository(session).list_all()]

    async def raise_unit_counter(self) -> int:
        """kb_counter를 index.db 벡터·전문 색인 테이블의 최대 rowid 이상으로(M21 검토 1-4)."""
        if not self.runtime.available:
            return 0
        highest = 0
        async with self.runtime.index_db.read() as conn:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            for (name,) in rows:
                if _INDEX_TABLE.match(str(name)):
                    highest = max(highest, await max_rowid(conn, str(name)))
        async with self.db.write() as session:
            value = await KbCounterRepository(session).raise_to(highest)
        if value == highest and highest:
            log.info("knowledge.unit_counter_raised", value=value)
        return value

    async def reconcile(self, *, skip_busy: bool = False) -> dict[str, int]:
        """KB마다 app.db 설정과 index.db를 맞춘다(5.4 설정 비교, 5.5 재생성).

        벡터 설정(config_key·version·차원)이 다르거나 테이블이 없으면 다시 만들고 유닛을 pending,
        끝난 문서를 waiting으로 돌려 레인에 넣는다. 같은 차원에 모델만 다르면 vec0가 오류 없이
        엉뚱한 결과를 내므로 행 존재가 아니라 설정으로 가른다. skip_busy면(매시) 색인 중인 문서가
        있는 KB는 건너뛴다. 그 작업이 테이블을 직접 만들고 맞춘다.
        """
        result = {"kbs": 0, "documents": 0}
        if not self.runtime.available:
            return result
        for workspace_id in await self._workspace_ids():
            async with self.db.read() as session:
                kbs = await KnowledgeBaseRepository(session, workspace_id).list_active()
                busy = (
                    await KbDocumentRepository(session, workspace_id).in_progress_counts(
                        [kb.id for kb in kbs]
                    )
                    if skip_busy
                    else {}
                )
            for kb in kbs:
                if busy.get(kb.id):
                    continue
                documents = await self._reconcile_kb(workspace_id, kb)
                if documents is not None:
                    result["kbs"] += 1
                    result["documents"] += documents
        return result

    async def _reconcile_kb(self, workspace_id: uuid.UUID, kb: KnowledgeBase) -> int | None:
        async with self.db.read() as session:
            if not await KbSegmentRepository(session, workspace_id).has_units(kb.id):
                return None
        meta = await self.runtime.index_db.describe(kb.id)
        async with self.runtime.index_db.read() as conn:
            fts_missing = not await table_exists(conn, fts_table_name(kb.id))
            vec_present = await table_exists(conn, vec_table_name(kb.id))
        config = embedding_config_of(kb)
        vector_mismatch = config is not None and (
            meta is None
            or not vec_present
            or meta.vec_table != vec_table_name(kb.id)
            or meta.config_key != config.key()
            or meta.embedding_version != config.version
            or meta.dims != config.dimensions
        )
        if not fts_missing and not vector_mismatch:
            return None
        if config is not None and vector_mismatch:
            await self.runtime.vectors.ensure(kb.id, config, workspace_id=workspace_id)
        if fts_missing:
            await self.runtime.fts.ensure(kb.id, workspace_id=workspace_id)
        now = self.clock()
        async with self.db.write() as session:
            segments = KbSegmentRepository(session, workspace_id)
            children = KbChildChunkRepository(session, workspace_id)
            if fts_missing:
                await segments.requeue_fts(kb.id, now)
                await children.requeue_fts(kb.id)
            if vector_mismatch:
                await segments.requeue_vectors(kb.id, now)
                await children.requeue_vectors(kb.id)
            requeued = await KbDocumentRepository(session, workspace_id).ids_for_kb(
                kb.id,
                statuses=(KbIndexingStatus.COMPLETED, KbIndexingStatus.WAITING),
                limit=100_000,
            )
            documents = KbDocumentRepository(session, workspace_id)
            for document_id in requeued:
                await documents.transition(
                    document_id,
                    (KbIndexingStatus.COMPLETED, KbIndexingStatus.WAITING),
                    {
                        "indexing_status": KbIndexingStatus.WAITING,
                        "completed_at": None,
                        "updated_at": now,
                    },
                )
        event = "knowledge.index_rebuild" if meta is None else "knowledge.index_mismatch"
        log.warning(
            event,
            kb_id=str(kb.id),
            documents=len(requeued),
            fts_missing=fts_missing,
            vector_mismatch=vector_mismatch,
        )
        if self.lane is not None:
            for document_id in requeued:
                self.lane.index(workspace_id, document_id)
        return len(requeued)

    async def purge_orphans(self) -> dict[str, int]:
        """index.db에만 있는 행을 지우고 app.db에 ok인데 행이 없는 유닛은 pending으로(5.4)."""
        result = {"vec": 0, "fts": 0, "requeued": 0, "dropped_kbs": 0}
        if not self.runtime.available:
            return result
        for meta in await self.runtime.index_db.list_kbs():
            async with self.db.read() as session:
                kb = await KnowledgeBaseRepository(session, meta.workspace_id).get(meta.kb_id)
            if kb is None:
                # app.db가 이 KB를 모른다(옛 백업으로 되돌렸거나 다른 데이터 폴더의 index.db).
                await self.runtime.index_db.drop_kb(meta.kb_id)
                result["dropped_kbs"] += 1
                continue
            if kb.deleted_at is not None:
                continue
            vec, fts, requeued = await self._orphans_of(kb)
            result["vec"] += vec
            result["fts"] += fts
            result["requeued"] += requeued
            await self.runtime.index_db.refresh_unit_count(kb.id)
        if any(result.values()):
            log.warning("knowledge.orphans", **result)
        return result

    async def _orphans_of(self, kb: KnowledgeBase) -> tuple[int, int, int]:
        workspace_id = kb.workspace_id
        # index.db 행 목록을 먼저 읽는다. 작업은 app.db에 유닛 행을 넣은 뒤에 index.db에 쓰므로,
        # 이 목록에 있는 행의 유닛은 곧 읽을 app.db에 반드시 있다(막 생긴 행을 고아로 지우지
        # 않는다).
        vec_rows = {unit_no async for unit_no in self.runtime.vectors.list_unit_nos(kb.id)}
        fts_rows = {unit_no async for unit_no in self.runtime.fts.list_unit_nos(kb.id)}
        states: list[UnitState] = []
        repository = (
            KbChildChunkRepository if kb.doc_form == KbDocForm.HIERARCHICAL else KbSegmentRepository
        )
        async with self.db.read() as session:
            busy = set(
                await KbDocumentRepository(session, workspace_id).ids_for_kb(
                    kb.id, statuses=IN_PROGRESS_STATUSES, limit=100_000
                )
            )
        after = 0
        while True:
            async with self.db.read() as session:
                page = await repository(session, workspace_id).kb_unit_states(
                    kb.id, after, STATE_PAGE
                )
            states.extend(page)
            if len(page) < STATE_PAGE:
                break
            after = page[-1].unit_no
        # 켜진 유닛만 색인에 있어야 한다. 끈 청크의 행은 남아 있으면 지운다.
        wanted = {state.unit_no for state in states if state.enabled}
        await self.runtime.vectors.delete(kb.id, vec_rows - wanted)
        await self.runtime.fts.delete(kb.id, fts_rows - wanted)
        # 색인 중인 문서의 유닛은 작업이 쓰는 중이라 건너뛴다.
        lost = [
            state
            for state in states
            if state.enabled
            and state.document_id not in busy
            and (
                (state.index_state.get("fts") == "ok" and state.unit_no not in fts_rows)
                or (state.index_state.get("vector") == "ok" and state.unit_no not in vec_rows)
            )
        ]
        requeued = await self._requeue_lost(kb, lost, fts_rows, vec_rows) if lost else 0
        return len(vec_rows - wanted), len(fts_rows - wanted), requeued

    async def _requeue_lost(
        self,
        kb: KnowledgeBase,
        lost: Sequence[UnitState],
        fts_rows: Collection[int],
        vec_rows: Collection[int],
    ) -> int:
        workspace_id = kb.workspace_id
        now = self.clock()
        hierarchical = kb.doc_form == KbDocForm.HIERARCHICAL
        documents: set[uuid.UUID] = set()
        async with self.db.write() as session:
            repository = (
                KbChildChunkRepository(session, workspace_id)
                if hierarchical
                else KbSegmentRepository(session, workspace_id)
            )
            ids = [state.row_id for state in lost]
            rows: list[KbSegment] | list[KbChildChunk] = await repository.get_many(ids)
            parents: set[uuid.UUID] = set()
            for row in rows:
                state = dict(row.index_state or {})
                unit_no = row.unit_no
                if unit_no is None:
                    continue
                if state.get("fts") == "ok" and unit_no not in fts_rows:
                    state["fts"] = "pending"
                if state.get("vector") == "ok" and unit_no not in vec_rows:
                    state.update(vector="pending", embedding_version=None, embedded_at=None)
                row.index_state = state
                row.updated_at = now
                if isinstance(row, KbSegment):
                    row.status = KbSegmentStatus.WAITING
                else:
                    parents.add(row.segment_id)
                documents.add(row.document_id)
            if parents:
                await KbSegmentRepository(session, workspace_id).flush()
                for parent_id in parents:
                    await settle_parent(session, workspace_id, parent_id, now)
            for document_id in documents:
                # 색인 중인 문서는 작업이 직접 이어 간다. 끝났거나 멈춘 문서만 다시 넣는다.
                await KbDocumentRepository(session, workspace_id).transition(
                    document_id,
                    (KbIndexingStatus.COMPLETED,),
                    {
                        "indexing_status": KbIndexingStatus.WAITING,
                        "completed_at": None,
                        "updated_at": now,
                    },
                )
        if self.lane is not None:
            for document_id in documents:
                self.lane.index(workspace_id, document_id)
        return len(lost)

    async def purge_deleted(self) -> dict[str, int]:
        """지운 KB의 색인·청크·문서·원본을 지우고 작업이 죽어 남은 지운 문서를 다시 넣는다."""
        result = {"kbs": 0, "documents": 0, "requeued": 0}
        for workspace_id in await self._workspace_ids():
            async with self.db.read() as session:
                kbs = await KnowledgeBaseRepository(session, workspace_id).list_all()
                deleted_docs = await KbDocumentRepository(session, workspace_id).list_deleted()
            gone = {kb.id for kb in kbs if kb.deleted_at is not None}
            for kb in kbs:
                if kb.deleted_at is not None:
                    purged = await self._purge_kb(workspace_id, kb)
                    result["documents"] += purged
                    result["kbs"] += 1 if purged else 0
            for document in deleted_docs:
                if document.kb_id in gone or self.lane is None:
                    continue
                self.lane.delete(workspace_id, document.id)
                result["requeued"] += 1
        if any(result.values()):
            log.info("knowledge.deleted_purged", **result)
        return result

    async def _purge_kb(self, workspace_id: uuid.UUID, kb: KnowledgeBase) -> int:
        if self.runtime.available:
            with contextlib.suppress(IndexUnavailable):
                await self.runtime.index_db.drop_kb(kb.id)
        purged = 0
        while True:
            async with self.db.read() as session:
                document_ids = await KbDocumentRepository(session, workspace_id).ids_for_kb(
                    kb.id, limit=100
                )
            if not document_ids:
                break
            for document_id in document_ids:
                await delete_document_rows(self.db, workspace_id, document_id)
                async with self.db.write() as session:
                    documents = KbDocumentRepository(session, workspace_id)
                    keys = await documents.storage_keys([document_id])
                    await KbDocumentMetadataRepository(session, workspace_id).delete_for_document(
                        document_id
                    )
                    await documents.delete(document_id)
                for key in keys:
                    with contextlib.suppress(OSError, StorageError):
                        await self.storage.delete(key)
                purged += 1
        async with self.db.write() as session:
            await KbMetadataFieldRepository(session, workspace_id).delete_for_kb(kb.id)
            await KbEvalCaseRetentionRepository(session, workspace_id).delete_for_kb(kb.id)
            await KbEvalRetentionRepository(session, workspace_id).delete_for_kb(kb.id)
            await KbQueryLogRetentionRepository(session, workspace_id).delete_for_kb(kb.id)
            await KnowledgeBaseRepository(session, workspace_id).refresh_counts(kb.id)
        return purged

    async def purge_work_dirs(self) -> int:
        """kb-work/<문서> 중 돌고 있는 작업의 것이 아니고 1시간이 지난 폴더를 지운다."""
        root = self.settings.data_dir / KB_WORK
        cutoff = (self.clock() - WORK_DIR_KEEP).timestamp()
        folders = await asyncio.to_thread(_stale_dirs, root, cutoff)
        running: set[str] = set()
        if self.lane is not None:
            running = {
                key.rsplit(":", 1)[-1]
                for key in self.lane.supervisor.running_keys()
                if key.startswith("index:")
            }
        removed = 0
        resolved = await asyncio.to_thread(root.resolve) if folders else root
        for document_id in folders:
            if str(document_id) in running:
                continue
            handle = WorkspaceHandle(
                session_id=document_id, root=resolved / str(document_id), backend="local"
            )
            try:
                await self.sandbox.release(handle, delete=True)
            except SandboxError as exc:
                log.warning("knowledge.work_dir_delete_failed", error=exc.message)
                continue
            removed += 1
        return removed + await self.sandbox.purge_deleted()

    async def purge_cache(self) -> int:
        if not self.runtime.available:
            return 0
        return await self.runtime.cache.purge(
            max_age_days=self.settings.kb_embed_cache_days, max_rows=EMBED_CACHE_MAX_ROWS
        )

    async def purge_query_logs(self) -> int:
        cutoff = self.clock() - timedelta(days=self.settings.kb_query_log_days)
        deleted = 0
        for workspace_id in await self._workspace_ids():
            async with self.db.write() as session:
                deleted += await KbQueryLogRetentionRepository(session, workspace_id).delete_before(
                    cutoff
                )
        return deleted

    async def trim_eval_runs(self) -> int:
        trimmed = 0
        for workspace_id in await self._workspace_ids():
            async with self.db.write() as session:
                runs = KbEvalRetentionRepository(session, workspace_id)
                for kb_id in await runs.kb_ids():
                    trimmed += await runs.trim(kb_id, EVAL_RUNS_KEEP)
        return trimmed

    async def purge_loose_files(self) -> int:
        """kb-files/<워크스페이스>/ 아래 어느 문서 행도 가리키지 않고 7일이 지난 원본(4.5).

        원본을 바꾸면(update-by-text·file) 새 키에 쓰므로 파일 이름이 문서 id와 같다고 보지 않고
        행의 storage_key와 맞대어 본다.
        """
        try:
            root = self.storage.local_path("kb-files")
        except StorageError:
            return 0
        cutoff = (self.clock() - LOOSE_FILE_KEEP).timestamp()
        candidates = await asyncio.to_thread(_stale_files, root, cutoff)
        removed = 0
        for workspace_id, names in candidates.items():
            keys = [f"kb-files/{workspace_id}/{name}" for name in names]
            async with self.db.read() as session:
                used = await KbDocumentRepository(session, workspace_id).referenced_keys(keys)
            for key in sorted(set(keys) - used):
                with contextlib.suppress(OSError, StorageError):
                    if await self.storage.delete(key):
                        removed += 1
        return removed

    async def checkpoint(self) -> int:
        if not self.runtime.available:
            return 0
        _, frames, _ = await self.runtime.index_db.checkpoint("PASSIVE")
        return frames


def _stale_dirs(root: Path, cutoff: float) -> list[uuid.UUID]:
    found: list[uuid.UUID] = []
    if not root.is_dir():
        return found
    for path in root.iterdir():
        try:
            document_id = uuid.UUID(path.name)
        except ValueError:
            continue
        if str(document_id) == path.name and path.is_dir() and path.stat().st_mtime < cutoff:
            found.append(document_id)
    return found


def _stale_files(root: Path, cutoff: float) -> dict[uuid.UUID, list[str]]:
    found: dict[uuid.UUID, list[str]] = {}
    if not root.is_dir():
        return found
    for folder in root.iterdir():
        try:
            workspace_id = uuid.UUID(folder.name)
        except ValueError:
            continue
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            # 쓰다 만 임시 파일(.part)도 7일이 지났으면 지운다.
            if path.is_file() and path.stat().st_mtime < cutoff:
                found.setdefault(workspace_id, []).append(path.name)
    return found
