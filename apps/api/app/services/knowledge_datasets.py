"""KB 단위 서비스 API(datasets, 3단계 설계 8.5, 확장)가 기존 지식 서비스 위에 더 필요로 하는 것.

KB 정보(Dify 필드명), 업로드 묶음(batch)의 색인 상태, 문서를 만들 때 이름으로 주는 메타데이터
값, Dify 모양(operation_data)의 메타데이터 일괄 변경이다. 문서·청크·메타데이터 조작 자체는 콘솔과
같은 서비스를 그대로 부른다.
"""

import uuid
from collections.abc import Mapping, Sequence

from app.db.engine import Database
from app.db.repositories.knowledge import KbDocumentRepository, KbMetadataFieldRepository
from app.db.repositories.knowledge_search import KbSearchDocumentRepository
from app.schemas.knowledge import (
    DocumentMetadataList,
    DocumentMetadataOut,
    DocumentMetadataUpdate,
    MetadataValueIn,
)
from app.schemas.service_api import (
    DatasetIndexingStatus,
    DatasetIndexingStatusItem,
    DatasetOut,
    DifyDocumentMetadataUpdate,
)
from app.services.errors import Problems, ServiceError, not_found
from app.services.knowledge import active_kb, parse_embedding, parse_retrieval
from app.services.knowledge_metadata import (
    KnowledgeMetadataService,
    _parse_value,
    document_metadata,
)

MetadataValues = Mapping[str, int | float | str | None]
# DocumentMetadataUpdate.values 한 번의 상한.
VALUES_PER_UPDATE = 50


class KnowledgeDatasetService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def dataset(self, workspace_id: uuid.UUID, kb_id: uuid.UUID) -> DatasetOut:
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            words = await KbSearchDocumentRepository(session, workspace_id).word_count(kb.id)
        embedding = parse_embedding(kb.embedding)
        return DatasetOut(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            indexing_technique=kb.indexing_technique,  # pyright: ignore[reportArgumentType]
            doc_form=kb.doc_form,  # pyright: ignore[reportArgumentType]
            embedding_model=embedding.model if embedding is not None else None,
            embedding_model_provider="gemini" if embedding is not None else None,
            retrieval_model=parse_retrieval(kb.retrieval_model),
            document_count=kb.document_count,
            word_count=words,
            created_at=kb.created_at,
            updated_at=kb.updated_at,
        )

    async def doc_form(self, workspace_id: uuid.UUID, kb_id: uuid.UUID) -> str:
        # 세그먼트 응답에 붙는 값이다. KB 요약(문서 글자 수 합)은 다시 세지 않는다.
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
        return kb.doc_form

    async def indexing_status(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, batch: str
    ) -> DatasetIndexingStatus:
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            rows = await KbSearchDocumentRepository(session, workspace_id).in_batch(kb.id, batch)
        return DatasetIndexingStatus(
            data=[
                DatasetIndexingStatusItem(
                    id=row.id,
                    indexing_status=row.indexing_status,  # pyright: ignore[reportArgumentType]
                    processing_started_at=row.processing_started_at,
                    parsing_completed_at=row.parsing_completed_at,
                    cleaning_completed_at=row.cleaning_completed_at,
                    splitting_completed_at=row.splitting_completed_at,
                    completed_at=row.completed_at,
                    paused_at=row.paused_at,
                    stopped_at=row.stopped_at,
                    error=row.error,
                    completed_segments=row.completed_segments,
                    total_segments=row.total_segments,
                )
                for row in rows
            ]
        )

    async def metadata_fields(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, values: MetadataValues
    ) -> list[MetadataValueIn]:
        """이름 → 값을 필드 id → 값으로. 문서를 만들기 전에 불러 없는 필드와 틀린 값을 먼저
        거절한다."""
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            fields = await KbMetadataFieldRepository(session, workspace_id).list_for_kb(kb.id)
        by_name = {field.name: field for field in fields}
        unknown = sorted(name for name in values if name not in by_name)
        if unknown:
            raise ServiceError(
                422,
                "metadata_field_not_found",
                f"지식 베이스에 없는 메타데이터 필드다: {', '.join(unknown)}",
                details={"names": unknown},
            )
        built_in = sorted(name for name in values if by_name[name].built_in)
        if built_in:
            raise ServiceError(
                400, "metadata_built_in", "기본 필드는 시스템이 채운다. 고치거나 지울 수 없다"
            )
        # 값의 타입도 문서를 만들기 전에 본다. 콘솔의 값 저장과 같은 검사다.
        problems = Problems()
        for name, value in values.items():
            _parse_value(by_name[name], value, ("doc_metadata", name), problems)
        problems.raise_if_any()
        return [
            MetadataValueIn(field_id=by_name[name].id, value=value)
            for name, value in values.items()
        ]

    async def apply_operations(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, body: DifyDocumentMetadataUpdate
    ) -> DocumentMetadataList:
        """Dify 모양의 문서 메타데이터 일괄 변경(M23 검토에서 미룬 것).

        metadata_list의 필드는 id로, 없으면 name으로 찾는다. 기본 필드는 시스템이 채우므로 건너뛴다
        (Dify 클라이언트가 조회한 목록을 그대로 돌려보내도 되게). partial_update가 거짓이면 Dify처럼
        목록에 없는 사용자 필드 값을 지운다. 문서마다 따로 쓰므로 중간에 실패하면 앞의 문서는 바뀐
        채로 남는다.
        """
        document_ids = list(dict.fromkeys(item.document_id for item in body.operation_data))
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            fields = await KbMetadataFieldRepository(session, workspace_id).list_for_kb(kb.id)
            found = await KbDocumentRepository(session, workspace_id).get_many_live(
                kb.id, document_ids
            )
        if len(found) != len(document_ids):
            raise not_found("문서")
        by_id = {field.id: field for field in fields}
        by_name = {field.name: field for field in fields}
        problems = Problems()
        plans: list[tuple[uuid.UUID, dict[uuid.UUID, int | float | str | None]]] = []
        for index, operation in enumerate(body.operation_data):
            values: dict[uuid.UUID, int | float | str | None] = {}
            for position, item in enumerate(operation.metadata_list):
                field = by_id.get(item.id) if item.id is not None else by_name.get(item.name or "")
                if field is None:
                    problems.add(
                        ("operation_data", index, "metadata_list", position),
                        "metadata_field_not_found",
                        f"지식 베이스에 없는 메타데이터 필드다: {item.id or item.name}",
                    )
                    continue
                if not field.built_in:
                    values[field.id] = item.value
            if not operation.partial_update:
                for field in fields:
                    if not field.built_in:
                        values.setdefault(field.id, None)
            plans.append((operation.document_id, values))
        problems.raise_if_any()
        service = KnowledgeMetadataService(self._db)
        for document_id, values in plans:
            await self._write(service, workspace_id, kb.id, document_id, list(values.items()))
        async with self._db.read() as session:
            current = await document_metadata(session, workspace_id, kb.id, document_ids)
        return DocumentMetadataList(
            items=[
                DocumentMetadataOut(document_id=doc_id, doc_metadata=current[doc_id])
                for doc_id in document_ids
            ]
        )

    async def _write(
        self,
        service: KnowledgeMetadataService,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        values: Sequence[tuple[uuid.UUID, int | float | str | None]],
    ) -> None:
        for start in range(0, len(values), VALUES_PER_UPDATE):
            chunk = values[start : start + VALUES_PER_UPDATE]
            await service.update_values(
                workspace_id,
                kb_id,
                DocumentMetadataUpdate(
                    document_ids=[document_id],
                    values=[
                        MetadataValueIn(field_id=field_id, value=value) for field_id, value in chunk
                    ],
                    apply_to_all=True,
                ),
            )

    async def apply_metadata(
        self,
        workspace_id: uuid.UUID,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        values: list[MetadataValueIn],
    ) -> None:
        if not values:
            return
        await KnowledgeMetadataService(self._db).update_values(
            workspace_id,
            kb_id,
            DocumentMetadataUpdate(document_ids=[document_id], values=values, apply_to_all=True),
        )
