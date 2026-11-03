"""KB 단위 서비스 API(datasets, 3단계 설계 8.5, 확장)가 기존 지식 서비스 위에 더 필요로 하는 것.

KB 정보(Dify 필드명), 업로드 묶음(batch)의 색인 상태, 문서를 만들 때 이름으로 주는 메타데이터
값이다. 문서·청크·메타데이터 조작 자체는 콘솔과 같은 서비스를 그대로 부른다.
"""

import uuid
from collections.abc import Mapping

from app.db.engine import Database
from app.db.repositories.knowledge import KbMetadataFieldRepository
from app.db.repositories.knowledge_search import KbSearchDocumentRepository
from app.schemas.knowledge import DocumentMetadataUpdate, MetadataValueIn
from app.schemas.service_api import (
    DatasetIndexingStatus,
    DatasetIndexingStatusItem,
    DatasetOut,
)
from app.services.errors import Problems, ServiceError
from app.services.knowledge import active_kb, parse_embedding, parse_retrieval
from app.services.knowledge_metadata import KnowledgeMetadataService, _parse_value

MetadataValues = Mapping[str, int | float | str | None]


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
