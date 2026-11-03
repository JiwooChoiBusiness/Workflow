"""지식 베이스 메타데이터 필드와 문서 값(3단계 설계 4.1, 7.5, 12.1).

필드는 KB 단위이고 값은 문서마다 kb_document_metadata 행 하나다. 검색 필터가 이 행을 SQL 조건으로
읽는다(M23). 기본 필드 다섯(document_name, uploader, upload_date, last_update_date, source)은
built_in_fields_enabled를 켜면 시스템이 만들고 채우며 사용자는 고치지 못한다.
"""

import uuid
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import Database
from app.db.models import KbDocument, KbDocumentMetadata, KbMetadataField, KnowledgeBase
from app.db.models.enums import KbMetadataType
from app.db.repositories.knowledge import (
    KbDocumentMetadataRepository,
    KbDocumentRepository,
    KbMetadataFieldRepository,
)
from app.db.types import new_id, utcnow
from app.schemas.knowledge import (
    DocumentMetadataList,
    DocumentMetadataOut,
    DocumentMetadataUpdate,
    DocumentMetadataValue,
    MetadataFieldCreate,
    MetadataFieldList,
    MetadataFieldOut,
    MetadataFieldPatch,
)
from app.services.errors import Problems, ServiceError, not_found
from app.services.knowledge import active_kb

log = structlog.get_logger("app.services.knowledge_metadata")

BUILT_IN_FIELDS: tuple[tuple[str, KbMetadataType], ...] = (
    ("document_name", KbMetadataType.STRING),
    ("uploader", KbMetadataType.STRING),
    ("upload_date", KbMetadataType.TIME),
    ("last_update_date", KbMetadataType.TIME),
    ("source", KbMetadataType.STRING),
)
BUILT_IN_NAMES = frozenset(name for name, _ in BUILT_IN_FIELDS)
MAX_STRING_VALUE = 1000

MetadataValue = str | float | datetime | None


def _value_of(row: KbDocumentMetadata, field_type: str) -> MetadataValue:
    if field_type == KbMetadataType.NUMBER:
        return row.value_number
    if field_type == KbMetadataType.TIME:
        return row.value_time
    return row.value_string


def _assign(row: KbDocumentMetadata, field_type: str, value: MetadataValue) -> None:
    row.value_string = (
        value if field_type == KbMetadataType.STRING and isinstance(value, str) else None
    )
    row.value_number = (
        float(value)
        if field_type == KbMetadataType.NUMBER and isinstance(value, int | float)
        else None
    )
    row.value_time = (
        value if field_type == KbMetadataType.TIME and isinstance(value, datetime) else None
    )


def _built_in_value(name: str, document: KbDocument, uploader: str | None) -> MetadataValue:
    match name:
        case "document_name":
            return document.name
        case "uploader":
            return uploader
        case "upload_date":
            return document.created_at
        case "last_update_date":
            return document.updated_at
        case _:
            return document.data_source_type


async def _set_values(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    kb_id: uuid.UUID,
    field: KbMetadataField,
    values: dict[uuid.UUID, MetadataValue],
) -> None:
    """문서마다 이 필드의 값을 넣거나 바꾼다. None이면 그 문서의 값을 지운다."""
    repo = KbDocumentMetadataRepository(session, workspace_id)
    existing = {row.document_id: row for row in await repo.list_for_field(field.id, list(values))}
    await repo.delete_values(
        field.id, [doc_id for doc_id, value in values.items() if value is None]
    )
    for document_id, value in values.items():
        if value is None:
            continue
        row = existing.get(document_id)
        if row is None:
            row = repo.add(
                KbDocumentMetadata(
                    id=new_id(), kb_id=kb_id, document_id=document_id, field_id=field.id
                )
            )
        _assign(row, field.type, value)
    await repo.flush()


async def fill_built_in(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    kb: KnowledgeBase,
    documents: Sequence[KbDocument],
    *,
    uploader: str | None,
    only: Collection[str] | None = None,
) -> None:
    """기본 필드 값을 문서에서 다시 채운다. only를 주면 그 이름의 필드만."""
    if not kb.built_in_fields_enabled or not documents:
        return
    fields = await KbMetadataFieldRepository(session, workspace_id).list_for_kb(kb.id)
    for field in fields:
        if not field.built_in or (only is not None and field.name not in only):
            continue
        values = {doc.id: _built_in_value(field.name, doc, uploader) for doc in documents}
        await _set_values(session, workspace_id, kb.id, field, values)


async def document_metadata(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    kb_id: uuid.UUID,
    document_ids: Collection[uuid.UUID],
) -> dict[uuid.UUID, list[DocumentMetadataValue]]:
    """문서마다 값이 있는 필드 목록(필드 순서대로)."""
    found: dict[uuid.UUID, list[DocumentMetadataValue]] = {doc_id: [] for doc_id in document_ids}
    if not document_ids:
        return found
    fields = await KbMetadataFieldRepository(session, workspace_id).list_for_kb(kb_id)
    rows = await KbDocumentMetadataRepository(session, workspace_id).list_for_documents(
        document_ids
    )
    by_key = {(row.document_id, row.field_id): row for row in rows}
    for document_id in document_ids:
        for field in fields:
            row = by_key.get((document_id, field.id))
            if row is None:
                continue
            found[document_id].append(
                DocumentMetadataValue(
                    field_id=field.id,
                    name=field.name,
                    type=field.type,  # pyright: ignore[reportArgumentType]
                    built_in=field.built_in,
                    value=_value_of(row, field.type),
                )
            )
    return found


def _field_out(field: KbMetadataField, use_count: int) -> MetadataFieldOut:
    return MetadataFieldOut(
        id=field.id,
        name=field.name,
        type=field.type,  # pyright: ignore[reportArgumentType]
        built_in=field.built_in,
        position=field.position,
        use_count=use_count,
        created_at=field.created_at,
    )


def _parse_value(
    field: KbMetadataField, raw: Any, loc: tuple[str | int, ...], problems: Problems
) -> MetadataValue:
    if raw is None:
        return None
    if field.type == KbMetadataType.STRING:
        if isinstance(raw, str) and len(raw) <= MAX_STRING_VALUE:
            return raw
        message = f"string 필드 값은 {MAX_STRING_VALUE}자 이하의 글이어야 한다"
    elif field.type == KbMetadataType.NUMBER:
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            return float(raw)
        message = "number 필드 값은 수여야 한다"
    else:
        if isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        message = (
            "time 필드 값은 시간대가 있는 ISO 8601 글이어야 한다(예: 2026-11-02T09:00:00+09:00)"
        )
    problems.add(loc, "metadata_value_invalid", message, field=field.name)
    return None


class KnowledgeMetadataService:
    def __init__(self, db: Database, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._clock = clock

    async def _list(
        self, session: AsyncSession, workspace_id: uuid.UUID, kb: KnowledgeBase
    ) -> MetadataFieldList:
        fields = await KbMetadataFieldRepository(session, workspace_id).list_for_kb(kb.id)
        counts = await KbDocumentMetadataRepository(session, workspace_id).use_counts(kb.id)
        return MetadataFieldList(
            items=[_field_out(field, counts.get(field.id, 0)) for field in fields],
            built_in_fields_enabled=kb.built_in_fields_enabled,
        )

    async def list_fields(self, workspace_id: uuid.UUID, kb_id: uuid.UUID) -> MetadataFieldList:
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            return await self._list(session, workspace_id, kb)

    async def create_field(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, body: MetadataFieldCreate
    ) -> MetadataFieldOut:
        _check_name(body.name)
        async with self._db.write() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            fields = KbMetadataFieldRepository(session, workspace_id)
            if await fields.name_taken(kb.id, body.name):
                raise _name_taken(body.name)
            field = fields.add(
                KbMetadataField(
                    id=new_id(),
                    kb_id=kb.id,
                    name=body.name,
                    type=body.type,
                    built_in=False,
                    position=await fields.next_position(kb.id),
                    created_at=self._clock(),
                )
            )
            await fields.flush()
            return _field_out(field, 0)

    async def rename_field(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        field_id: uuid.UUID,
        patch: MetadataFieldPatch,
    ) -> MetadataFieldOut:
        _check_name(patch.name)
        async with self._db.write() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            fields = KbMetadataFieldRepository(session, workspace_id)
            field = await _user_field(fields, kb.id, field_id)
            if patch.name != field.name:
                if await fields.name_taken(kb.id, patch.name, exclude_id=field.id):
                    raise _name_taken(patch.name)
                field.name = patch.name
                await fields.flush()
            counts = await KbDocumentMetadataRepository(session, workspace_id).use_counts(kb.id)
            return _field_out(field, counts.get(field.id, 0))

    async def delete_field(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, field_id: uuid.UUID
    ) -> None:
        """필드와 그 값 행을 함께 지운다(4.1)."""
        async with self._db.write() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            fields = KbMetadataFieldRepository(session, workspace_id)
            field = await _user_field(fields, kb.id, field_id)
            await KbDocumentMetadataRepository(session, workspace_id).delete_for_fields([field.id])
            await fields.delete_many([field.id])
        log.info("knowledge.metadata_field_deleted", kb_id=str(kb_id), field_id=str(field_id))

    async def set_built_in(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, enabled: bool, *, uploader: str | None
    ) -> MetadataFieldList:
        async with self._db.write() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            fields = KbMetadataFieldRepository(session, workspace_id)
            current = [field for field in await fields.list_for_kb(kb.id) if field.built_in]
            if enabled and not kb.built_in_fields_enabled:
                position = await fields.next_position(kb.id)
                for offset, (name, field_type) in enumerate(BUILT_IN_FIELDS):
                    fields.add(
                        KbMetadataField(
                            id=new_id(),
                            kb_id=kb.id,
                            name=name,
                            type=field_type,
                            built_in=True,
                            position=position + offset,
                            created_at=self._clock(),
                        )
                    )
                await fields.flush()
                kb.built_in_fields_enabled = True
                documents = await KbDocumentRepository(session, workspace_id).all_live(kb.id)
                await fill_built_in(session, workspace_id, kb, documents, uploader=uploader)
            elif not enabled and kb.built_in_fields_enabled:
                ids = [field.id for field in current]
                await KbDocumentMetadataRepository(session, workspace_id).delete_for_fields(ids)
                await fields.delete_many(ids)
                kb.built_in_fields_enabled = False
            return await self._list(session, workspace_id, kb)

    async def update_values(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, body: DocumentMetadataUpdate
    ) -> DocumentMetadataList:
        document_ids = list(dict.fromkeys(body.document_ids))
        async with self._db.write() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            documents = await KbDocumentRepository(session, workspace_id).get_many_live(
                kb.id, document_ids
            )
            if len(documents) != len(document_ids):
                raise not_found("문서")
            if any(document.archived for document in documents):
                raise ServiceError(
                    400, "document_archived", "보관한 문서는 읽기 전용이다. 보관을 풀고 고친다"
                )
            fields = KbMetadataFieldRepository(session, workspace_id)
            problems = Problems()
            updates: list[tuple[KbMetadataField, MetadataValue]] = []
            for index, item in enumerate(body.values):
                field = await fields.get_in_kb(kb.id, item.field_id)
                if field is None:
                    raise not_found("메타데이터 필드")
                if field.built_in:
                    raise _built_in_readonly()
                value = _parse_value(field, item.value, ("values", index, "value"), problems)
                updates.append((field, value))
            problems.raise_if_any()
            values = KbDocumentMetadataRepository(session, workspace_id)
            for field, value in updates:
                targets = document_ids
                if not body.apply_to_all:
                    # "모든 문서에 적용"을 끄면 그 필드가 이미 있는 문서만 바꾼다(7.5).
                    present = await values.list_for_field(field.id, document_ids)
                    targets = [row.document_id for row in present]
                await _set_values(
                    session, workspace_id, kb.id, field, dict.fromkeys(targets, value)
                )
            found = await document_metadata(session, workspace_id, kb.id, document_ids)
        return DocumentMetadataList(
            items=[
                DocumentMetadataOut(document_id=doc_id, doc_metadata=found[doc_id])
                for doc_id in document_ids
            ]
        )


def _check_name(name: str) -> None:
    if name in BUILT_IN_NAMES:
        raise ServiceError(
            422,
            "metadata_name_reserved",
            f"{name}은(는) 기본 필드 이름이라 쓸 수 없다",
            details={"name": name},
        )


def _name_taken(name: str) -> ServiceError:
    return ServiceError(
        409, "metadata_field_exists", f"이미 있는 필드 이름이다: {name}", details={"name": name}
    )


def _built_in_readonly() -> ServiceError:
    return ServiceError(
        400, "metadata_built_in", "기본 필드는 시스템이 채운다. 고치거나 지울 수 없다"
    )


async def _user_field(
    fields: KbMetadataFieldRepository, kb_id: uuid.UUID, field_id: uuid.UUID
) -> KbMetadataField:
    field = await fields.get_in_kb(kb_id, field_id)
    if field is None:
        raise not_found("메타데이터 필드")
    if field.built_in:
        raise _built_in_readonly()
    return field
