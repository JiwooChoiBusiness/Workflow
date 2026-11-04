"""워크플로우 지식 노드와 에이전트 search_knowledge가 쓰는 지식 베이스 포트(3단계 설계 8.1,
8.2, 8.4).

엔진과 루프는 DB를 모르므로 실행기가 런마다 여기의 포트를 만들어 넣는다. 검색은 검색 서비스
(KnowledgeSearchService.retrieve)를 source=node·tool로 부르고, 문서 만들기는 문서 서비스의 생성
경로를 그대로 부른다. 그래프 검증이 보는 살아 있는 지식 베이스 목록(KnowledgeBaseRef)과 node-types의
kb_options도 여기서 만든다.
"""

import uuid
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from typing import Any, get_args

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.base import KnowledgeResult, KnowledgeSearchError, ModelUsage
from app.agent.tools.search_knowledge import MAX_QUERY_CHARS, KnowledgeBaseChoice
from app.config import Settings
from app.db.engine import Database
from app.db.models.enums import KbDataSourceType
from app.db.repositories.knowledge import (
    KbDocumentRepository,
    KbMetadataFieldRepository,
    KnowledgeBaseRepository,
)
from app.knowledge.retrieval.filters import OPERATORS
from app.llm.base import ModelAdapter
from app.llm.key_pool import KeyTier
from app.schemas.citations import RetrieverResourceData
from app.schemas.knowledge import (
    KbDocumentPatch,
    KbDocumentTextCreate,
    KbDocumentUploadOptions,
    MetadataType,
    ProcessRule,
    RerankingMode,
    RetrievalSettings,
    SearchMethod,
)
from app.schemas.retrieval import (
    MAX_CONDITIONS,
    ComparisonOperator,
    FilterMode,
    MetadataFilter,
)
from app.schemas.workflow import Graph, KnowledgeNodeOptions
from app.services.agent_tree import agents_tier
from app.services.errors import ServiceError
from app.services.files import sanitize_filename
from app.services.knowledge import parse_retrieval
from app.services.knowledge_datasets import KnowledgeDatasetService
from app.services.knowledge_documents import KnowledgeDocumentService, UploadSource
from app.services.knowledge_index_executor import KnowledgeLane
from app.services.knowledge_runtime import KnowledgeRuntime, key_tier_of
from app.services.knowledge_search import KnowledgeSearchService, RetrievalFailed
from app.services.run_tier import paid_key_configured, sql_tier
from app.services.workflow_files import stored_file
from app.storage.base import StorageBackend
from app.workflow.graph import agent_references, knowledge_references, sql_references
from app.workflow.nodes.base import KnowledgeBaseRef
from app.workflow.nodes.knowledge_index import DEFAULT_WAIT_SEC, INDEX_STATUSES, MAX_WAIT_SEC
from app.workflow.nodes.knowledge_retrieval import MAX_KBS, RETRIEVAL_TIMEOUT_SEC
from app.workflow.ports import (
    DuplicatePolicy,
    IndexedDocuments,
    KnowledgeHits,
    KnowledgePortError,
)

log = structlog.get_logger("app.services.workflow_knowledge")

ERR_KB_NOT_FOUND = "knowledge_base_not_found"
ERR_DOCUMENT_NOT_FOUND = "document_not_found"


def resource_data(raw: Any) -> RetrieverResourceData:
    """검색 서비스의 RetrieverResource를 인용 모델로(필드명이 같다)."""
    return RetrieverResourceData.model_validate(raw.model_dump(mode="json"))


async def knowledge_refs(
    session: AsyncSession, workspace_id: uuid.UUID, kb_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, KnowledgeBaseRef]:
    """살아 있는 지식 베이스의 검증용 정보(이름, 유료 전용인지, 메타데이터 필드)."""
    if not kb_ids:
        return {}
    rows = await KnowledgeBaseRepository(session, workspace_id).get_many_active(kb_ids)
    fields = KbMetadataFieldRepository(session, workspace_id)
    refs: dict[uuid.UUID, KnowledgeBaseRef] = {}
    for row in rows:
        found: dict[str, MetadataType] = {
            field.name: field.type  # pyright: ignore[reportAssignmentType]
            for field in await fields.list_for_kb(row.id)
        }
        refs[row.id] = KnowledgeBaseRef(
            id=row.id, name=row.name, paid=key_tier_of(row) == "paid", fields=found
        )
    return refs


async def graph_knowledge(
    session: AsyncSession, workspace_id: uuid.UUID, graph: Graph | None
) -> dict[uuid.UUID, KnowledgeBaseRef]:
    """그래프의 지식 노드가 가리키는 살아 있는 지식 베이스(live_agent_ids와 같은 자리)."""
    if graph is None:
        return {}
    return await knowledge_refs(session, workspace_id, knowledge_references(graph))


def graph_tier(refs: Mapping[uuid.UUID, KnowledgeBaseRef]) -> KeyTier:
    return "paid" if any(ref.paid for ref in refs.values()) else "free"


async def workflow_tier(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    graph: Graph | None,
    refs: Mapping[uuid.UUID, KnowledgeBaseRef],
) -> KeyTier:
    """워크플로우 런의 등급. 지식 노드의 KB, SQL 노드의 데이터 소스(3단계 10.6), agent 노드가 부르는
    에이전트 트리의 KB·데이터 소스를 함께 본다."""
    if graph_tier(refs) == "paid" or graph is None:
        return graph_tier(refs)
    if await sql_tier(session, workspace_id, sql_references(graph)) == "paid":
        return "paid"
    return await agents_tier(session, workspace_id, agent_references(graph))


async def knowledge_choices(
    session: AsyncSession, workspace_id: uuid.UUID, kb_ids: Sequence[uuid.UUID]
) -> list[KnowledgeBaseChoice]:
    """에이전트 런이 검색할 지식 베이스(버전에 고른 순서). 지운 것은 뺀다."""
    if not kb_ids:
        return []
    rows = await KnowledgeBaseRepository(session, workspace_id).get_many_active(kb_ids)
    by_id = {row.id: row for row in rows}
    return [
        KnowledgeBaseChoice(kb_id=row.id, slug=row.slug, name=row.name, description=row.description)
        for kb_id in kb_ids
        if (row := by_id.get(kb_id)) is not None
    ]


def node_options(settings: Settings) -> KnowledgeNodeOptions:
    operators: dict[str, list[str]] = {
        kind: [name for name in get_args(ComparisonOperator) if name in allowed]
        for kind, allowed in OPERATORS.items()
    }
    return KnowledgeNodeOptions(
        max_kbs=MAX_KBS,
        top_k_max=settings.kb_top_k_max,
        default_retrieval=RetrievalSettings(),
        search_methods=list(get_args(SearchMethod)),
        reranking_modes=list(get_args(RerankingMode)),
        rerank_candidates_max=20,
        filter_modes=list(get_args(FilterMode)),
        max_conditions=MAX_CONDITIONS,
        operators=operators,
        retrieval_timeout_sec=RETRIEVAL_TIMEOUT_SEC,
        duplicate_policies=list(get_args(DuplicatePolicy)),
        index_statuses=list(INDEX_STATUSES),
        index_wait_default_sec=DEFAULT_WAIT_SEC,
        index_wait_max_sec=MAX_WAIT_SEC,
        tool_query_max_chars=MAX_QUERY_CHARS,
        paid_key_configured=paid_key_configured(settings),
    )


def _port_error(exc: ServiceError, *, missing: str = ERR_KB_NOT_FOUND) -> KnowledgePortError:
    """서비스 오류를 포트 오류로. 문서 서비스의 404(not_found)는 missing 코드가 된다.

    KB를 찾는 호출(검색, 문서를 만들기 전)은 검색 서비스와 같은 knowledge_base_not_found, 만든
    문서를 다루는 호출은 document_not_found로 준다.
    """
    if isinstance(exc, RetrievalFailed):
        return KnowledgePortError(
            exc.code, exc.message, retryable=exc.retryable, retry_after=exc.retry_after
        )
    code = missing if exc.code == "not_found" and exc.status == 404 else exc.code
    return KnowledgePortError(code, exc.message, retryable=exc.status >= 500)


class WorkflowKnowledgePort:
    """엔진의 KnowledgePort. 런 하나의 워크스페이스로 묶여 있다."""

    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        workspace_id: uuid.UUID,
        runtime: KnowledgeRuntime | None,
        llm: ModelAdapter | None,
        storage: StorageBackend | None,
        lane: KnowledgeLane | None,
    ) -> None:
        self._db = db
        self._settings = settings
        self._workspace_id = workspace_id
        self._runtime = runtime
        self._llm = llm
        self._storage = storage
        self._lane = lane

    async def retrieve(
        self,
        kb_ids: Sequence[uuid.UUID],
        query: str,
        settings: RetrievalSettings | None,
        metadata_filter: MetadataFilter | None,
        *,
        tier: KeyTier,
    ) -> KnowledgeHits:
        service = KnowledgeSearchService(self._db, self._settings, self._runtime, llm=self._llm)
        try:
            result = await service.retrieve(
                self._workspace_id,
                kb_ids,
                query,
                settings,
                metadata_filter=metadata_filter,
                source="node",
                tier=tier,
            )
        except ServiceError as exc:
            raise _port_error(exc) from None
        return KnowledgeHits(
            query=result.query,
            resources=[resource_data(item) for item in result.resources()],
            warnings=list(result.warnings),
            calls=list(result.model_calls),
            settings=result.settings,
            latency_ms=result.latency_ms,
            auto_filter=result.auto_filter,
        )

    def _documents(self) -> KnowledgeDocumentService:
        if self._storage is None:
            raise KnowledgePortError("knowledge_unavailable", "문서를 저장할 저장소가 없다")
        return KnowledgeDocumentService(self._db, self._settings, self._storage, lane=self._lane)

    async def _same_name(
        self, kb_id: uuid.UUID, names: Collection[str]
    ) -> dict[str, list[uuid.UUID]]:
        """이름이 같은 살아 있는 문서. KB가 없으면 knowledge_base_not_found다."""
        async with self._db.read() as session:
            if await KnowledgeBaseRepository(session, self._workspace_id).get_active(kb_id) is None:
                raise KnowledgePortError(ERR_KB_NOT_FOUND, "지식 베이스를 찾을 수 없다")
            documents = KbDocumentRepository(session, self._workspace_id)
            return await documents.live_ids_by_name(kb_id, set(names))

    async def _metadata(self, kb_id: uuid.UUID, metadata: Mapping[str, str]) -> list[Any]:
        """렌더한 글을 필드 타입에 맞춰 바꾸고 문서를 만들기 전에 검사한다. 빈 값은 두지 않는다."""
        wanted = {name: value for name, value in metadata.items() if value}
        if not wanted:
            return []
        async with self._db.read() as session:
            fields = await KbMetadataFieldRepository(session, self._workspace_id).list_for_kb(kb_id)
        types = {field.name: field.type for field in fields}
        values: dict[str, int | float | str | None] = {}
        for name, text in wanted.items():
            if types.get(name) == "number":
                try:
                    values[name] = float(text)
                except ValueError:
                    raise KnowledgePortError(
                        "metadata_value_invalid", f"메타데이터 {name}의 값이 수가 아니다: {text}"
                    ) from None
            else:
                values[name] = text
        try:
            return await KnowledgeDatasetService(self._db).metadata_fields(
                self._workspace_id, kb_id, values
            )
        except ServiceError as exc:
            raise _port_error(exc) from None

    async def _finish(
        self,
        documents: KnowledgeDocumentService,
        kb_id: uuid.UUID,
        created: Sequence[uuid.UUID],
        *,
        replaced: Sequence[uuid.UUID],
        values: list[Any],
        rename: str | None = None,
    ) -> None:
        """문서를 만든 뒤의 일(이름 바꾸기, 메타데이터, 같은 이름의 옛 문서 지우기).

        여기서 실패하면 노드를 다시 돌려도 문서가 또 생기므로 재시도하지 않게 하고, 이미 만든 문서
        id를 오류 글에 남긴다. 새 문서를 먼저 넣고 옛 문서를 지우므로 중간에 실패해도 있던 문서는
        남는다. 이미 지워진 옛 문서(404)는 넘어간다.
        """
        datasets = KnowledgeDatasetService(self._db)
        try:
            if rename is not None and len(created) == 1:
                await documents.update_document(
                    self._workspace_id, kb_id, created[0], KbDocumentPatch(name=rename)
                )
            for document_id in created:
                await datasets.apply_metadata(self._workspace_id, kb_id, document_id, values)
            for document_id in replaced:
                try:
                    await documents.delete_document(self._workspace_id, kb_id, document_id)
                except ServiceError as exc:
                    if exc.status != 404:
                        raise
        except ServiceError as exc:
            error = _port_error(exc, missing=ERR_DOCUMENT_NOT_FOUND)
            made = ", ".join(str(document_id) for document_id in created)
            raise KnowledgePortError(
                error.code, f"{error.message} (이미 만든 문서: {made})", retryable=False
            ) from None

    async def index_text(
        self,
        kb_id: uuid.UUID,
        *,
        name: str,
        text: str,
        process_rule: ProcessRule | None,
        metadata: Mapping[str, str],
        on_duplicate: DuplicatePolicy,
    ) -> IndexedDocuments:
        documents = self._documents()
        existing = (await self._same_name(kb_id, [name])).get(name, [])
        if existing and on_duplicate == "skip":
            return IndexedDocuments(document_ids=existing, batch="", skipped=True)
        values = await self._metadata(kb_id, metadata)
        try:
            created = await documents.create_from_text(
                self._workspace_id,
                kb_id,
                KbDocumentTextCreate(
                    name=name, text=text, process_rule=process_rule, allow_duplicate=True
                ),
                data_source_type=KbDataSourceType.WORKFLOW,
            )
        except ServiceError as exc:
            raise _port_error(exc) from None
        ids = [document.id for document in created.documents]
        await self._finish(
            documents,
            kb_id,
            ids,
            replaced=existing if on_duplicate == "replace" else [],
            values=values,
        )
        log.info("knowledge.node_indexed", kb_id=str(kb_id), documents=len(ids), source="text")
        return IndexedDocuments(document_ids=ids, batch=created.batch)

    async def index_files(
        self,
        kb_id: uuid.UUID,
        files: Sequence[Mapping[str, Any]],
        *,
        name: str | None,
        process_rule: ProcessRule | None,
        metadata: Mapping[str, str],
        on_duplicate: DuplicatePolicy,
    ) -> IndexedDocuments:
        documents = self._documents()
        storage = self._storage
        if storage is None:
            raise KnowledgePortError("knowledge_unavailable", "문서를 저장할 저장소가 없다")
        located: list[tuple[str, str, str]] = []
        for raw in files:
            try:
                found = await stored_file(self._db, self._workspace_id, raw)
            except ValueError:
                raise KnowledgePortError("file_invalid", "파일 참조가 아니다") from None
            if found is None:
                filename = str(raw.get("filename") or "")
                raise KnowledgePortError("file_not_found", f"파일을 찾지 못했다: {filename}")
            located.append((found.ref.filename, found.ref.mime, found.storage_key))
        # 문서 이름은 문서 서비스가 정리한 파일 이름이다. 하나뿐이면 노드가 준 이름으로 바꾼다.
        names = [name] if name is not None else [sanitize_filename(item[0]) for item in located]
        existing = await self._same_name(kb_id, names)
        skipped: list[uuid.UUID] = []
        if on_duplicate == "skip":
            keep: list[tuple[str, str, str]] = []
            for item, doc_name in zip(located, names, strict=False):
                if doc_name in existing:
                    skipped.extend(existing[doc_name])
                else:
                    keep.append(item)
            located = keep
            if not located:
                return IndexedDocuments(document_ids=skipped, batch="", skipped=True)
        values = await self._metadata(kb_id, metadata)

        def chunks(key: str) -> AsyncIterator[bytes]:
            return storage.read_stream(key)

        try:
            created = await documents.create_from_uploads(
                self._workspace_id,
                kb_id,
                [UploadSource(filename, mime, chunks(key)) for filename, mime, key in located],
                KbDocumentUploadOptions(process_rule=process_rule, allow_duplicate=True),
                data_source_type=KbDataSourceType.WORKFLOW,
            )
        except ServiceError as exc:
            raise _port_error(exc) from None
        ids = [document.id for document in created.documents]
        replaced = (
            [doc_id for doc_ids in existing.values() for doc_id in doc_ids]
            if on_duplicate == "replace"
            else []
        )
        await self._finish(documents, kb_id, ids, replaced=replaced, values=values, rename=name)
        log.info("knowledge.node_indexed", kb_id=str(kb_id), documents=len(ids), source="files")
        return IndexedDocuments(document_ids=[*ids, *skipped], batch=created.batch)

    async def statuses(
        self, kb_id: uuid.UUID, document_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        async with self._db.read() as session:
            rows = await KbDocumentRepository(session, self._workspace_id).get_many_live(
                kb_id, list(document_ids)
            )
        return {row.id: row.indexing_status for row in rows}


class AgentKnowledgePort:
    """search_knowledge의 KnowledgePort. 런의 워크스페이스·키 등급과 버전의 검색 설정으로 묶여
    있다."""

    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        workspace_id: uuid.UUID,
        runtime: KnowledgeRuntime | None,
        llm: ModelAdapter | None,
        retrieval_model: RetrievalSettings | None,
        tier: KeyTier,
    ) -> None:
        self._db = db
        self._settings = settings
        self._workspace_id = workspace_id
        self._runtime = runtime
        self._llm = llm
        self._retrieval = retrieval_model
        self._tier: KeyTier = tier

    async def _settings_for(
        self, kb_ids: Sequence[uuid.UUID], top_k: int | None
    ) -> RetrievalSettings | None:
        settings = self._retrieval
        if settings is None and len(kb_ids) > 1:
            # 저장 검증이 막지만 3단계 전 형식이나 손으로 고친 버전을 위해 기본값으로 섞는다.
            settings = RetrievalSettings()
        if settings is None and top_k is not None:
            async with self._db.read() as session:
                kb = await KnowledgeBaseRepository(session, self._workspace_id).get_active(
                    kb_ids[0]
                )
            settings = parse_retrieval(kb.retrieval_model) if kb is not None else None
        if settings is not None and top_k is not None:
            settings = settings.model_copy(
                update={"top_k": min(top_k, self._settings.kb_top_k_max)}
            )
        return settings

    async def search(
        self, query: str, kb_ids: Sequence[uuid.UUID], *, top_k: int | None
    ) -> KnowledgeResult:
        service = KnowledgeSearchService(self._db, self._settings, self._runtime, llm=self._llm)
        try:
            settings = await self._settings_for(kb_ids, top_k)
            result = await service.retrieve(
                self._workspace_id, kb_ids, query, settings, source="tool", tier=self._tier
            )
        except ServiceError as exc:
            error = _port_error(exc)
            raise KnowledgeSearchError(error.code, error.message) from None
        return KnowledgeResult(
            resources=[resource_data(item) for item in result.resources()],
            warnings=[f"{item.code}: {item.message}" for item in result.warnings],
            calls=[
                ModelUsage(model=call.model, key_index=call.key_index, usage=call.usage)
                for call in result.model_calls
            ],
        )
