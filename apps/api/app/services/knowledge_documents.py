"""지식 베이스 문서 올리기·목록·삭제·상태 조작·재색인·미리보기(3단계 설계 4.4, 6.2~6.5, 12.1).

올린 파일과 붙여 넣은 글은 저장소 kb-files/<workspace_id>/<document_id>에 원본으로 두고 문서는
waiting으로 만들어 지식 레인에 넣는다. 파싱·분할·색인은 레인의 작업이 한다. 삭제는 deleted_at만 찍고
바로 돌아온다. 행·색인·원본은 레인의 delete 작업과 매시 정리가 지운다(4.4).

일시 정지는 요청이 paused를 적고 레인에서 뺀다(실행 중이면 취소). 작업은 다음 묶음 경계에서 상태를
그대로 두고 나가므로 재개 요청과 엇갈려도 paused를 덮어쓰지 않는다.
"""

import asyncio
import math
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from app.config import Settings
from app.db.engine import Database
from app.db.models import KbDocument, KnowledgeBase
from app.db.models.enums import KbDataSourceType, KbDocForm, KbIndexingStatus, KbIndexingTechnique
from app.db.repositories.knowledge import (
    IN_PROGRESS_STATUSES,
    KbDocumentRepository,
    KnowledgeBaseRepository,
)
from app.db.types import new_id, utcnow
from app.knowledge.ingest.job import prepare_chunks
from app.knowledge.ingest.parse import (
    SANDBOX_KINDS,
    SUFFIXES,
    WARN_NO_TEXT_LAYER,
    WARN_TEXT_TRUNCATED,
    SourceText,
    extract_in_sandbox,
    file_kind,
    read_local_text,
)
from app.sandbox.base import SandboxBackend, SandboxError
from app.schemas.knowledge import (
    DocumentMetadataValue,
    DocumentPreview,
    DocumentStatusAction,
    DocumentStatusResult,
    DocumentStatusSkip,
    ExtractMode,
    KbDocumentList,
    KbDocumentOut,
    KbDocumentPatch,
    KbDocumentsCreated,
    KbDocumentTextCreate,
    KbDocumentUploadOptions,
    PreviewChildChunk,
    PreviewChunk,
    ProcessRule,
)
from app.services.errors import ServiceError
from app.services.files import guess_mime, sanitize_filename
from app.services.knowledge import (
    MAX_UPLOAD_FILES,
    active_kb,
    display_of,
    live_document,
    parse_process_rule,
    retry_after_of,
    split_rules,
)
from app.services.knowledge_metadata import document_metadata, fill_built_in
from app.services.knowledge_runtime import embedding_config_of, key_tier_of
from app.storage.base import StorageBackend, StorageError, StorageLimitExceeded, kb_file_key
from app.workflow.extract_runner import ExtractError

if TYPE_CHECKING:
    from app.llm.embeddings import EmbedLimiter
    from app.services.knowledge_index_executor import KnowledgeLane

log = structlog.get_logger("app.services.knowledge_documents")

ACCEPTED_FORMATS = "txt, md, csv, html, pdf, docx"
_MB = 1024 * 1024
# 미리보기가 돌려주는 청크 수(6.4). Parent-child는 부모 10개와 그 자식이다.
PREVIEW_CHUNKS = 50
PREVIEW_PARENTS = 10
_PAUSABLE = frozenset(IN_PROGRESS_STATUSES)


@dataclass(frozen=True)
class UploadSource:
    filename: str | None
    content_type: str | None
    chunks: AsyncIterable[bytes]


@dataclass(frozen=True)
class OriginalFile:
    storage_key: str
    filename: str
    mime: str


@dataclass(frozen=True)
class _Stored:
    document_id: uuid.UUID
    storage_key: str
    name: str
    filename: str
    mime: str
    size_bytes: int
    sha256: str


def display_status(document: KbDocument) -> str:
    return display_of(document.indexing_status, document.enabled, document.archived)


def document_out(
    document: KbDocument, doc_metadata: list[DocumentMetadataValue] | None = None
) -> KbDocumentOut:
    return KbDocumentOut(
        id=document.id,
        kb_id=document.kb_id,
        position=document.position,
        name=document.name,
        data_source_type=document.data_source_type,  # pyright: ignore[reportArgumentType]
        filename=document.filename,
        mime=document.mime,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        indexing_status=document.indexing_status,  # pyright: ignore[reportArgumentType]
        display_status=display_status(document),  # pyright: ignore[reportArgumentType]
        enabled=document.enabled,
        archived=document.archived,
        error=document.error,
        process_rule=parse_process_rule(document.process_rule),
        extract=document.extract,
        total_segments=document.total_segments,
        completed_segments=document.completed_segments,
        word_count=document.word_count,
        tokens=document.tokens,
        hit_count=document.hit_count,
        batch=document.batch,
        processing_started_at=document.processing_started_at,
        parsing_completed_at=document.parsing_completed_at,
        cleaning_completed_at=document.cleaning_completed_at,
        splitting_completed_at=document.splitting_completed_at,
        completed_at=document.completed_at,
        paused_at=document.paused_at,
        stopped_at=document.stopped_at,
        doc_metadata=doc_metadata or [],
        created_at=document.created_at,
        updated_at=document.updated_at,
    )


def check_writable(document: KbDocument) -> None:
    # 보관 문서는 읽기 전용이다(4.4).
    if document.archived:
        raise ServiceError(
            400, "document_archived", "보관한 문서는 읽기 전용이다. 보관을 풀고 고친다"
        )


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _text_filename(name: str) -> str:
    return sanitize_filename(name if name.lower().endswith(".txt") else f"{name}.txt")


def _extract_mode(document: KbDocument) -> ExtractMode:
    return "model" if (document.extract or {}).get("mode") == "model" else "text"


class KnowledgeDocumentService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        storage: StorageBackend,
        *,
        lane: "KnowledgeLane | None" = None,
        sandbox: SandboxBackend | None = None,
        limiter: "EmbedLimiter | None" = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._storage = storage
        # 지식 레인. 없으면(시험) 문서가 waiting으로 남고 다음 기동의 복구가 넣는다.
        self._lane = lane
        # DATA_DIR/kb-work의 백엔드. pdf·docx 미리보기가 쓴다.
        self._sandbox = sandbox
        self._limiter = limiter
        self._clock = clock

    @property
    def max_upload_bytes(self) -> int:
        return self._settings.kb_upload_max_mb * _MB

    @property
    def max_text_bytes(self) -> int:
        return self._settings.kb_doc_max_text_mb * _MB

    async def list_documents(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        *,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> KbDocumentList:
        offset = (page - 1) * limit
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            documents = KbDocumentRepository(session, workspace_id)
            total = await documents.count_live(kb.id, status=status, keyword=keyword)
            rows = await documents.list_live(
                kb.id, status=status, keyword=keyword, limit=limit, offset=offset
            )
            found = await document_metadata(session, workspace_id, kb.id, [row.id for row in rows])
        return KbDocumentList(
            items=[document_out(row, found[row.id]) for row in rows],
            total=total,
            page=page,
            limit=limit,
            has_more=offset + len(rows) < total,
        )

    async def get_document(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, document_id: uuid.UUID
    ) -> KbDocumentOut:
        async with self._db.read() as session:
            kb, document = await live_document(session, workspace_id, kb_id, document_id)
            found = await document_metadata(session, workspace_id, kb.id, [document.id])
        return document_out(document, found[document.id])

    async def original(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, document_id: uuid.UUID
    ) -> OriginalFile:
        """문서 원본. 원본을 바꾼 문서는 지금 원본이고, 글로 만든 문서는 저장해 둔 .txt다."""
        async with self._db.read() as session:
            _, document = await live_document(session, workspace_id, kb_id, document_id)
        return OriginalFile(document.storage_key, document.filename, document.mime)

    async def create_from_text(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        body: KbDocumentTextCreate,
        *,
        uploader: str | None = None,
        data_source_type: KbDataSourceType = KbDataSourceType.PASTE_TEXT,
    ) -> KbDocumentsCreated:
        """붙여 넣은 글 하나를 .txt 원본으로 저장하고 문서를 만든다."""
        data = body.text.encode("utf-8")
        if len(data) > self.max_text_bytes:
            raise ServiceError(
                413,
                "text_too_large",
                f"글은 {self._settings.kb_doc_max_text_mb}MB 이하여야 한다",
                details={"max_bytes": self.max_text_bytes},
            )
        async with self._db.read() as session:
            await active_kb(session, workspace_id, kb_id)
        document_id = new_id()
        key = kb_file_key(workspace_id, document_id)
        stored = await self._storage.write_stream(key, _one_chunk(data))
        item = _Stored(
            document_id=document_id,
            storage_key=key,
            name=body.name,
            filename=_text_filename(body.name),
            mime="text/plain",
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
        )
        return await self._insert(
            workspace_id,
            kb_id,
            [item],
            process_rule=body.process_rule,
            allow_duplicate=body.allow_duplicate,
            data_source_type=data_source_type,
            uploader=uploader,
        )

    async def create_from_uploads(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        files: Sequence[UploadSource],
        options: KbDocumentUploadOptions,
        *,
        uploader: str | None = None,
        data_source_type: KbDataSourceType = KbDataSourceType.UPLOAD_FILE,
    ) -> KbDocumentsCreated:
        """파일 여러 개(최대 20)를 원본으로 저장하고 문서를 만든다. 하나라도 실패하면 모두 뺀다.

        data_source_type은 문서가 어디서 왔는지다. knowledge_index 노드가 넣는 파일은 workflow다.
        """
        if not files:
            raise ServiceError(
                422,
                "validation_error",
                "요청 값이 올바르지 않다",
                details=[{"loc": ["body", "files"], "msg": "파일이 없다", "type": "missing"}],
            )
        if len(files) > MAX_UPLOAD_FILES:
            raise ServiceError(
                422,
                "too_many_files",
                f"한 번에 {MAX_UPLOAD_FILES}개까지 올린다",
                details={"max_files": MAX_UPLOAD_FILES},
            )
        names = [sanitize_filename(source.filename) for source in files]
        mimes = [
            guess_mime(name, source.content_type) for name, source in zip(names, files, strict=True)
        ]
        unsupported = [
            name
            for name, mime in zip(names, mimes, strict=True)
            if file_kind({"filename": name, "mime": mime}) is None
        ]
        if unsupported:
            raise ServiceError(
                415,
                "unsupported_file_type",
                f"지식 베이스에는 {ACCEPTED_FORMATS} 파일만 넣는다",
                details={"filenames": unsupported},
            )
        async with self._db.read() as session:
            await active_kb(session, workspace_id, kb_id)
        stored: list[_Stored] = []
        try:
            for source, name, mime in zip(files, names, mimes, strict=True):
                document_id = new_id()
                key = kb_file_key(workspace_id, document_id)
                try:
                    obj = await self._storage.write_stream(
                        key, source.chunks, max_bytes=self.max_upload_bytes
                    )
                except StorageLimitExceeded:
                    raise ServiceError(
                        413,
                        "file_too_large",
                        f"파일은 {self._settings.kb_upload_max_mb}MB 이하여야 한다",
                        details={"filename": name, "max_bytes": self.max_upload_bytes},
                    ) from None
                stored.append(
                    _Stored(
                        document_id=document_id,
                        storage_key=key,
                        name=name,
                        filename=name,
                        mime=mime,
                        size_bytes=obj.size_bytes,
                        sha256=obj.sha256,
                    )
                )
        except BaseException:
            await self._discard(stored)
            raise
        return await self._insert(
            workspace_id,
            kb_id,
            stored,
            process_rule=options.process_rule,
            allow_duplicate=options.allow_duplicate,
            data_source_type=data_source_type,
            uploader=uploader,
            extract_mode=options.extract_mode,
        )

    async def _discard(self, stored: Sequence[_Stored]) -> None:
        for item in stored:
            try:
                await self._storage.delete(item.storage_key)
            except (OSError, StorageError) as exc:
                log.warning("knowledge.file_delete_failed", key=item.storage_key, error=str(exc))

    async def _insert(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        stored: Sequence[_Stored],
        *,
        process_rule: ProcessRule | None,
        allow_duplicate: bool,
        data_source_type: KbDataSourceType,
        uploader: str | None,
        extract_mode: ExtractMode = "text",
    ) -> KbDocumentsCreated:
        batch = new_id().hex
        try:
            async with self._db.write() as session:
                kb = await active_kb(session, workspace_id, kb_id)
                documents = KbDocumentRepository(session, workspace_id)
                if not allow_duplicate:
                    await _check_duplicates(documents, kb.id, stored)
                rule = (process_rule or parse_process_rule(kb.process_rule)).model_dump(mode="json")
                position = await documents.next_position(kb.id)
                now = self._clock()
                rows = [
                    documents.add(
                        KbDocument(
                            id=item.document_id,
                            kb_id=kb.id,
                            position=position + offset,
                            name=item.name,
                            data_source_type=data_source_type,
                            filename=item.filename,
                            mime=item.mime,
                            size_bytes=item.size_bytes,
                            sha256=item.sha256,
                            storage_key=item.storage_key,
                            process_rule=rule,
                            # 모델로 읽기는 PDF에만 건다. 나머지는 파싱 때 채운다.
                            extract=(
                                {"mode": "model"}
                                if extract_mode == "model"
                                and file_kind({"filename": item.filename, "mime": item.mime})
                                == "pdf"
                                else None
                            ),
                            indexing_status=KbIndexingStatus.WAITING,
                            enabled=True,
                            archived=False,
                            total_segments=0,
                            completed_segments=0,
                            word_count=0,
                            tokens=0,
                            hit_count=0,
                            batch=batch,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    for offset, item in enumerate(stored)
                ]
                await documents.flush()
                await fill_built_in(session, workspace_id, kb, rows, uploader=uploader)
                await KnowledgeBaseRepository(session, workspace_id).refresh_counts(kb.id)
                found = await document_metadata(
                    session, workspace_id, kb.id, [row.id for row in rows]
                )
                created = KbDocumentsCreated(
                    batch=batch, documents=[document_out(row, found[row.id]) for row in rows]
                )
        except BaseException:
            await self._discard(stored)
            raise
        if self._lane is not None:
            for item in stored:
                self._lane.index(workspace_id, item.document_id)
        log.info(
            "knowledge.documents_created",
            kb_id=str(kb_id),
            batch=batch,
            count=len(stored),
            source=str(data_source_type),
        )
        return created

    async def update_document(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        patch: KbDocumentPatch,
        *,
        uploader: str | None = None,
    ) -> KbDocumentOut:
        async with self._db.write() as session:
            kb, document = await live_document(session, workspace_id, kb_id, document_id)
            check_writable(document)
            if patch.name is not None and patch.name != document.name:
                document.name = patch.name
                document.updated_at = self._clock()
                await fill_built_in(
                    session,
                    workspace_id,
                    kb,
                    [document],
                    uploader=uploader,
                    only=("document_name", "last_update_date"),
                )
            found = await document_metadata(session, workspace_id, kb.id, [document.id])
            return document_out(document, found[document.id])

    async def delete_document(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, document_id: uuid.UUID
    ) -> None:
        """deleted_at만 찍는다. 색인 중이어도 된다(지식 레인이 작업을 멈추고 지운다, 6.5)."""
        async with self._db.write() as session:
            kb, document = await live_document(session, workspace_id, kb_id, document_id)
            now = self._clock()
            document.deleted_at = now
            document.updated_at = now
            await KbDocumentRepository(session, workspace_id).flush()
            await KnowledgeBaseRepository(session, workspace_id).refresh_counts(kb.id)
        if self._lane is not None:
            self._lane.delete(workspace_id, document_id)
        log.info("knowledge.document_deleted", kb_id=str(kb_id), document_id=str(document_id))

    async def set_status(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        action: DocumentStatusAction,
        document_ids: Sequence[uuid.UUID],
    ) -> DocumentStatusResult:
        """문서 상태 조작(4.4, 6.5). 맞지 않는 상태의 문서는 skipped에 사유와 함께 남긴다.

        켜기·끄기·보관은 색인 중인 문서가 하나라도 있으면 400 document_indexing이다(Dify와 같다).
        """
        wanted = list(dict.fromkeys(document_ids))
        skipped: list[DocumentStatusSkip] = []
        changed: list[KbDocument] = []
        to_stop: list[uuid.UUID] = []
        to_queue: list[uuid.UUID] = []
        now = self._clock()
        async with self._db.write() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            rows = {
                row.id: row
                for row in await KbDocumentRepository(session, workspace_id).get_many_live(
                    kb.id, wanted
                )
            }
            for document_id in wanted:
                if document_id not in rows:
                    skipped.append(
                        DocumentStatusSkip(
                            document_id=document_id, code="not_found", message="문서가 없다"
                        )
                    )
            documents = [rows[document_id] for document_id in wanted if document_id in rows]
            if action in ("enable", "disable", "archive", "unarchive"):
                busy = [row.id for row in documents if row.indexing_status in _PAUSABLE]
                if busy:
                    raise ServiceError(
                        400,
                        "document_indexing",
                        "색인 중인 문서는 켜고 끄거나 보관할 수 없다. 끝나거나 멈춘 뒤 한다",
                        details={"document_ids": [str(item) for item in busy]},
                    )
            for document in documents:
                skip = self._apply(action, document, now)
                if skip is not None:
                    skipped.append(skip)
                    continue
                document.updated_at = now
                changed.append(document)
                if action == "pause":
                    to_stop.append(document.id)
                elif action in ("resume", "retry"):
                    to_queue.append(document.id)
            await KbDocumentRepository(session, workspace_id).flush()
            found = await document_metadata(
                session, workspace_id, kb.id, [row.id for row in changed]
            )
            items = [document_out(row, found[row.id]) for row in changed]
        if self._lane is not None:
            for document_id in to_stop:
                self._lane.stop(workspace_id, document_id, "pause")
            for document_id in to_queue:
                self._lane.index(workspace_id, document_id)
        log.info(
            "knowledge.documents_status",
            kb_id=str(kb_id),
            action=action,
            changed=len(changed),
            skipped=len(skipped),
        )
        return DocumentStatusResult(items=items, skipped=skipped)

    def _apply(
        self, action: DocumentStatusAction, document: KbDocument, now: datetime
    ) -> DocumentStatusSkip | None:
        status = document.indexing_status

        def skip(
            code: str, message: str, retry_after: datetime | None = None
        ) -> DocumentStatusSkip:
            return DocumentStatusSkip(
                document_id=document.id, code=code, message=message, retry_after=retry_after
            )

        match action:
            case "enable" | "disable":
                document.enabled = action == "enable"
            case "archive" | "unarchive":
                document.archived = action == "archive"
            case "pause":
                if status not in _PAUSABLE:
                    return skip("not_indexing", "대기·색인 중인 문서만 멈춘다")
                document.indexing_status = KbIndexingStatus.PAUSED
                document.paused_at = now
            case "resume":
                if status != KbIndexingStatus.PAUSED:
                    return skip("not_paused", "멈춘 문서만 다시 시작한다")
                document.indexing_status = KbIndexingStatus.WAITING
                document.paused_at = None
            case "retry":
                if status == KbIndexingStatus.WAITING:
                    # 레인에 없는 대기 문서(일일 한도로 큐를 비운 뒤 등)를 다시 넣는다.
                    if self._lane is not None and self._lane.busy(
                        document.workspace_id, document.id
                    ):
                        return skip("already_queued", "이미 대기열에 있다")
                    return None
                if status != KbIndexingStatus.ERROR:
                    return skip("not_failed", "오류로 끝난 문서만 다시 시도한다")
                retry_after = retry_after_of(document)
                if retry_after is not None and retry_after > now:
                    return skip(
                        "retry_after",
                        "오늘의 임베딩 일일 한도를 다 썼다."
                        " 태평양 자정(한국 16~17시) 뒤에 다시 시도한다",
                        retry_after,
                    )
                document.indexing_status = KbIndexingStatus.WAITING
                document.error = None
        return None

    async def reindex(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        extract_mode: ExtractMode | None,
    ) -> KbDocumentOut:
        """원본에서 처음부터 다시 나눈다. 지운 유닛의 벡터는 캐시로 가서 같은 청크는 다시 부르지
        않는다. extract_mode=model은 스캔 PDF를 채팅 모델로 읽는다(6.2)."""
        async with self._db.write() as session:
            kb, document = await live_document(session, workspace_id, kb_id, document_id)
            _check_reindexable(document)
            mode = extract_mode or _extract_mode(document)
            if (
                mode == "model"
                and file_kind({"filename": document.filename, "mime": document.mime}) != "pdf"
            ):
                raise _model_read_pdf_only()
            _restart(document, mode, self._clock())
            await KbDocumentRepository(session, workspace_id).flush()
            found = await document_metadata(session, workspace_id, kb.id, [document.id])
            out = document_out(document, found[document.id])
        if self._lane is not None:
            self._lane.index(workspace_id, document_id)
        log.info("knowledge.document_reindex", document_id=str(document_id), extract_mode=mode)
        return out

    async def replace_text(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        text: str,
        *,
        name: str | None = None,
        process_rule: ProcessRule | None = None,
        allow_duplicate: bool = False,
        uploader: str | None = None,
    ) -> KbDocumentOut:
        """원본을 새 글로 바꾸고 다시 색인한다(Dify update-by-text). 문서 id·메타데이터는 그대로.

        색인 중이면 400 document_indexing, 보관이면 400 document_archived(재색인과 같은 규칙),
        같은 KB의 다른 문서와 내용이 같으면 409 duplicate_document다.
        """
        data = text.encode("utf-8")
        if not data or len(data) > self.max_text_bytes:
            raise ServiceError(
                413 if data else 422,
                "text_too_large" if data else "content_blank",
                f"글은 비어 있지 않고 {self._settings.kb_doc_max_text_mb}MB 이하여야 한다",
                details={"max_bytes": self.max_text_bytes},
            )
        document = await self._replaceable(workspace_id, kb_id, document_id)
        label = name or document.name
        key = kb_file_key(workspace_id, new_id())
        stored = await self._storage.write_stream(key, _one_chunk(data))
        return await self._replace(
            workspace_id,
            kb_id,
            _Stored(
                document_id=document_id,
                storage_key=key,
                name=label,
                filename=_text_filename(label),
                mime="text/plain",
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
            ),
            name=name,
            process_rule=process_rule,
            allow_duplicate=allow_duplicate,
            extract_mode="text",
            uploader=uploader,
        )

    async def replace_file(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        upload: UploadSource,
        *,
        name: str | None = None,
        process_rule: ProcessRule | None = None,
        allow_duplicate: bool = False,
        extract_mode: ExtractMode | None = None,
        uploader: str | None = None,
    ) -> KbDocumentOut:
        """원본을 새 파일로 바꾸고 다시 색인한다(Dify update-by-file). 문서 id·메타데이터는 그대로.

        형식이 아니면 415 unsupported_file_type, KB_UPLOAD_MAX_MB를 넘으면 413 file_too_large,
        PDF가 아닌데 model이면 422 model_read_pdf_only다. 나머지는 replace_text와 같다.
        """
        filename = sanitize_filename(upload.filename)
        mime = guess_mime(filename, upload.content_type)
        kind = file_kind({"filename": filename, "mime": mime})
        if kind is None:
            raise ServiceError(
                415,
                "unsupported_file_type",
                f"지식 베이스에는 {ACCEPTED_FORMATS} 파일만 넣는다",
                details={"filenames": [filename]},
            )
        if extract_mode == "model" and kind != "pdf":
            raise _model_read_pdf_only()
        document = await self._replaceable(workspace_id, kb_id, document_id)
        key = kb_file_key(workspace_id, new_id())
        try:
            stored = await self._storage.write_stream(
                key, upload.chunks, max_bytes=self.max_upload_bytes
            )
        except StorageLimitExceeded:
            raise ServiceError(
                413,
                "file_too_large",
                f"파일은 {self._settings.kb_upload_max_mb}MB 이하여야 한다",
                details={"filename": filename, "max_bytes": self.max_upload_bytes},
            ) from None
        if kind != "pdf":
            mode: ExtractMode = "text"
        else:
            mode = extract_mode or _extract_mode(document)
        return await self._replace(
            workspace_id,
            kb_id,
            _Stored(
                document_id=document_id,
                storage_key=key,
                name=name or document.name,
                filename=filename,
                mime=mime,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
            ),
            name=name,
            process_rule=process_rule,
            allow_duplicate=allow_duplicate,
            extract_mode=mode,
            uploader=uploader,
        )

    async def _replaceable(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, document_id: uuid.UUID
    ) -> KbDocument:
        # 새 원본을 저장하기 전에 먼저 거른다. 쓰기 트랜잭션에서 한 번 더 본다.
        async with self._db.read() as session:
            _, document = await live_document(session, workspace_id, kb_id, document_id)
        _check_reindexable(document)
        return document

    async def _replace(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        item: _Stored,
        *,
        name: str | None,
        process_rule: ProcessRule | None,
        allow_duplicate: bool,
        extract_mode: ExtractMode,
        uploader: str | None,
    ) -> KbDocumentOut:
        """저장해 둔 새 원본으로 문서 행을 바꾸고 옛 원본을 지운 뒤 레인에 넣는다.

        원본은 새 키에 먼저 쓰고 행을 바꾼 뒤에 옛 키를 지운다. 중간에 실패하면 새 파일만 지워
        문서는 옛 원본을 그대로 가리킨다.
        """
        try:
            async with self._db.write() as session:
                kb, document = await live_document(session, workspace_id, kb_id, item.document_id)
                _check_reindexable(document)
                documents = KbDocumentRepository(session, workspace_id)
                if not allow_duplicate:
                    await _check_duplicates(documents, kb.id, [item], exclude=document.id)
                old_key = document.storage_key
                renamed = name is not None and name != document.name
                document.storage_key = item.storage_key
                document.filename = item.filename
                document.mime = item.mime
                document.size_bytes = item.size_bytes
                document.sha256 = item.sha256
                if name is not None:
                    document.name = name
                if process_rule is not None:
                    document.process_rule = process_rule.model_dump(mode="json")
                _restart(document, extract_mode, self._clock())
                await documents.flush()
                await fill_built_in(
                    session,
                    workspace_id,
                    kb,
                    [document],
                    uploader=uploader,
                    only=("document_name", "last_update_date")
                    if renamed
                    else ("last_update_date",),
                )
                found = await document_metadata(session, workspace_id, kb.id, [document.id])
                out = document_out(document, found[document.id])
        except BaseException:
            await self._discard([item])
            raise
        if old_key != item.storage_key:
            try:
                await self._storage.delete(old_key)
            except (OSError, StorageError) as exc:
                # 행 없이 남은 원본은 매시 정리가 7일 뒤 지운다.
                log.warning("knowledge.file_delete_failed", key=old_key, error=str(exc))
        if self._lane is not None:
            self._lane.index(workspace_id, item.document_id)
        log.info(
            "knowledge.document_replaced",
            kb_id=str(kb_id),
            document_id=str(item.document_id),
            extract_mode=extract_mode,
        )
        return out

    async def preview(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        *,
        process_rule: ProcessRule | None,
        text: str | None = None,
        upload: UploadSource | None = None,
    ) -> DocumentPreview:
        """파싱·정리·분할을 요청 안에서 돌린다. 아무것도 저장하지 않는다(6.4)."""
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
        config = embedding_config_of(kb)
        rule = process_rule or parse_process_rule(kb.process_rule)
        rules = split_rules(
            rule, kb.doc_form, input_limit=config.input_token_limit if config else None
        )
        if upload is not None:
            source = await self._preview_source(upload)
        else:
            source = SourceText(text=text or "", kind="text")
        warnings = [WARN_TEXT_TRUNCATED] if source.truncated else []
        if source.no_text_layer:
            warnings.append(WARN_NO_TEXT_LAYER)
        result, split_warnings = await asyncio.to_thread(prepare_chunks, source.text, rules)
        warnings.extend(item for item in split_warnings if item not in warnings)
        hierarchical = kb.doc_form == KbDocForm.HIERARCHICAL
        shown = result.chunks[: PREVIEW_PARENTS if hierarchical else PREVIEW_CHUNKS]
        children = sum(len(chunk.children) for chunk in result.chunks)
        tokens = (
            sum(child.tokens for chunk in result.chunks for child in chunk.children)
            if hierarchical
            else sum(chunk.tokens for chunk in result.chunks)
        )
        units = children if hierarchical else len(result.chunks)
        return DocumentPreview(
            chunks=[
                PreviewChunk(
                    content=chunk.content,
                    tokens=chunk.tokens,
                    answer=chunk.answer,
                    children=[
                        PreviewChildChunk(content=child.content, tokens=child.tokens)
                        for child in chunk.children
                    ],
                )
                for chunk in shown
            ],
            total_chunks=len(result.chunks),
            total_child_chunks=children,
            tokens=tokens,
            estimated_minutes=self._estimated_minutes(kb, units),
            extract=source.extract_info(warnings),
            warnings=warnings,
        )

    def _estimated_minutes(self, kb: KnowledgeBase, units: int) -> int:
        if kb.indexing_technique == KbIndexingTechnique.ECONOMY or units == 0:
            return 0
        tier = key_tier_of(kb)
        if self._limiter is not None:
            limits = self._limiter.limits(tier)
            rpm, reserve = limits.rpm, limits.reserve
        elif tier == "paid":
            rpm, reserve = self._settings.kb_embed_rpm_paid, self._settings.kb_embed_reserve
        else:
            rpm, reserve = self._settings.kb_embed_rpm, self._settings.kb_embed_reserve
        return math.ceil(units / max(rpm - reserve, 1))

    async def _preview_source(self, upload: UploadSource) -> SourceText:
        name = sanitize_filename(upload.filename)
        mime = guess_mime(name, upload.content_type)
        kind = file_kind({"filename": name, "mime": mime})
        if kind is None:
            raise ServiceError(
                415, "unsupported_file_type", f"지식 베이스에는 {ACCEPTED_FORMATS} 파일만 넣는다"
            )
        if self._sandbox is None:
            raise ServiceError(503, "extractor_unavailable", "미리보기 작업 폴더가 없다")
        budget = self.max_text_bytes
        handle = await self._sandbox.acquire(new_id())
        try:
            relative = f"source{SUFFIXES[kind]}"
            size = 0
            target = await asyncio.to_thread((handle.root / relative).open, "wb")
            try:
                async for chunk in upload.chunks:
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise ServiceError(
                            413,
                            "file_too_large",
                            f"파일은 {self._settings.kb_upload_max_mb}MB 이하여야 한다",
                            details={"max_bytes": self.max_upload_bytes},
                        )
                    await asyncio.to_thread(target.write, chunk)
            finally:
                await asyncio.to_thread(target.close)
            if kind not in SANDBOX_KINDS:
                return await read_local_text(handle.root / relative, kind, budget)
            cache = self._settings.data_dir / "cache" / "wf-extract"
            return await extract_in_sandbox(
                self._sandbox,
                handle,
                relative=relative,
                kind=kind,
                budget=budget,
                cache_dir=cache,
                exec_prefix=f"kb-preview-{handle.session_id.hex[-8:]}",
            )
        except ExtractError as exc:
            raise ServiceError(422, exc.code, exc.message) from None
        except SandboxError as exc:
            raise ServiceError(422, exc.code, exc.message) from None
        finally:
            try:
                await self._sandbox.release(handle, delete=True)
            except SandboxError as exc:
                log.warning("knowledge.preview_cleanup_failed", error=exc.message)


def _model_read_pdf_only() -> ServiceError:
    return ServiceError(422, "model_read_pdf_only", "모델로 읽기는 PDF 문서에만 쓸 수 있다")


def _check_reindexable(document: KbDocument) -> None:
    # 재색인·원본 바꾸기 공통. 보관 문서는 읽기 전용이고, 색인 중인 문서는 작업이 끝나거나 멈춘
    # 뒤에 한다(멈춘 문서는 된다).
    check_writable(document)
    if document.indexing_status in _PAUSABLE:
        raise ServiceError(
            400,
            "document_indexing",
            "색인 중인 문서는 다시 색인할 수 없다. 끝나거나 멈춘 뒤 한다",
            details={"indexing_status": document.indexing_status},
        )


def _restart(document: KbDocument, mode: ExtractMode, now: datetime) -> None:
    # 분할 완료 시각을 지워 작업이 남은 유닛을 지우고(벡터는 캐시로) 원본에서 처음부터 나누게 한다.
    document.indexing_status = KbIndexingStatus.WAITING
    document.splitting_completed_at = None
    document.completed_at = None
    document.paused_at = None
    document.error = None
    document.extract = {"mode": mode}
    document.updated_at = now


async def _check_duplicates(
    documents: KbDocumentRepository,
    kb_id: uuid.UUID,
    stored: Sequence[_Stored],
    *,
    exclude: uuid.UUID | None = None,
) -> None:
    # 같은 KB 안의 같은 내용(sha256)은 막는다. 같은 요청 안에서 겹친 파일도 같다(4.1). 원본을 바꾸는
    # 문서 자신(exclude)은 세지 않는다.
    existing = {
        row.sha256: row
        for row in await documents.live_with_sha256(kb_id, {s.sha256 for s in stored})
        if row.id != exclude
    }
    seen: dict[str, str] = {}
    duplicates: list[dict[str, Any]] = []
    for item in stored:
        row = existing.get(item.sha256)
        if row is not None:
            duplicates.append(
                {"filename": item.name, "document_id": str(row.id), "document_name": row.name}
            )
        elif item.sha256 in seen:
            duplicates.append({"filename": item.name, "duplicate_of": seen[item.sha256]})
        else:
            seen[item.sha256] = item.name
    if duplicates:
        raise ServiceError(
            409,
            "duplicate_document",
            "같은 내용의 문서가 이미 있다. 그래도 넣으려면 allow_duplicate를 참으로 보낸다",
            details={"duplicates": duplicates},
        )
