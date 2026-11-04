"""지식 베이스 테이블(3단계 설계 4.1)의 리포지토리. kb_counter 말고는 workspace_id 범위 안이다."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, case, func, or_, select, update

from app.db.models import (
    KbChildChunk,
    KbCounter,
    KbDocument,
    KbDocumentMetadata,
    KbEvalCase,
    KbEvalRun,
    KbMetadataField,
    KbQueryLog,
    KbSegment,
    KnowledgeBase,
)
from app.db.models.enums import KbIndexingStatus, KbSegmentStatus
from app.db.repositories.base import GlobalRepository, WorkspaceScopedRepository

UNIT_NO = "unit_no"
# 대기·파싱·정리·분할·임베딩 중. 목록의 "색인 중 n"과 청크 편집 거절에 쓴다.
IN_PROGRESS_STATUSES = (
    KbIndexingStatus.WAITING,
    KbIndexingStatus.PARSING,
    KbIndexingStatus.CLEANING,
    KbIndexingStatus.SPLITTING,
    KbIndexingStatus.INDEXING,
)
_RUNNING_STATUSES = IN_PROGRESS_STATUSES[1:]


@dataclass(frozen=True, slots=True)
class UnitState:
    # 유닛 하나의 색인 상태(본문 없이). 자식 청크면 enabled는 부모의 값이다.
    row_id: uuid.UUID
    unit_no: int
    index_state: dict[str, Any]
    enabled: bool
    document_id: uuid.UUID


def _pending_vector(column: Any) -> Any:
    # 유닛의 벡터만 다시 만들게 표시한다. JSON 열은 조건에 쓰지 않고 값만 바꾼다.
    return func.json_set(
        column, "$.vector", "pending", "$.embedding_version", None, "$.embedded_at", None
    )


def display_condition(status: str) -> ColumnElement[bool]:
    """목록 필터 값(display_status, 4.4)을 SQL 조건으로."""
    completed = KbDocument.indexing_status == KbIndexingStatus.COMPLETED
    match status:
        case "queuing":
            return KbDocument.indexing_status == KbIndexingStatus.WAITING
        case "indexing":
            return KbDocument.indexing_status.in_(list(_RUNNING_STATUSES))
        case "paused":
            return KbDocument.indexing_status == KbIndexingStatus.PAUSED
        case "error":
            return KbDocument.indexing_status == KbIndexingStatus.ERROR
        case "available":
            return and_(completed, KbDocument.enabled.is_(True), KbDocument.archived.is_(False))
        case "disabled":
            return and_(completed, KbDocument.enabled.is_(False), KbDocument.archived.is_(False))
        case "archived":
            return and_(completed, KbDocument.archived.is_(True))
        case _:
            raise ValueError(f"알 수 없는 문서 상태 필터: {status}")


class KnowledgeBaseRepository(WorkspaceScopedRepository[KnowledgeBase]):
    # 지운(deleted_at) KB의 행은 매시 정리가 문서까지 지운 뒤 없앤다. 조회는 살아 있는 행만 본다.
    model = KnowledgeBase

    async def get_active(self, kb_id: uuid.UUID) -> KnowledgeBase | None:
        return await self._scalar(
            self._select().where(KnowledgeBase.id == kb_id, KnowledgeBase.deleted_at.is_(None))
        )

    async def get_active_by_slug(self, slug: str) -> KnowledgeBase | None:
        return await self._scalar(
            self._select().where(KnowledgeBase.slug == slug, KnowledgeBase.deleted_at.is_(None))
        )

    async def get_many_active(self, kb_ids: Collection[uuid.UUID]) -> list[KnowledgeBase]:
        if not kb_ids:
            return []
        return await self._scalars(
            self._select().where(
                KnowledgeBase.id.in_(list(kb_ids)), KnowledgeBase.deleted_at.is_(None)
            )
        )

    async def list_active(self) -> list[KnowledgeBase]:
        return await self._scalars(
            self._select()
            .where(KnowledgeBase.deleted_at.is_(None))
            .order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id.desc())
        )

    async def slug_taken(self, slug: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        statement = self._select(KnowledgeBase.id).where(
            KnowledgeBase.slug == slug, KnowledgeBase.deleted_at.is_(None)
        )
        if exclude_id is not None:
            statement = statement.where(KnowledgeBase.id != exclude_id)
        return await self._scalar(statement.limit(1)) is not None

    async def list_all(self) -> list[KnowledgeBase]:
        # 지운 KB까지. 매시 정리와 기동 정합 검사가 쓴다.
        return await self._scalars(self._select().order_by(KnowledgeBase.created_at))

    async def add_embedded_tokens(self, kb_id: uuid.UUID, tokens: int) -> None:
        if tokens > 0:
            await self._execute(
                self._update()
                .where(KnowledgeBase.id == kb_id)
                .values(embedded_tokens_estimated=KnowledgeBase.embedded_tokens_estimated + tokens)
            )

    async def refresh_counts(self, kb_id: uuid.UUID) -> None:
        """지우지 않은 문서 수와 그 청크 수를 다시 센다. 문서·청크를 더하거나 지운 뒤 부른다."""
        documents = (
            select(func.count())
            .select_from(KbDocument)
            .where(
                KbDocument.workspace_id == self.workspace_id,
                KbDocument.kb_id == kb_id,
                KbDocument.deleted_at.is_(None),
            )
            .scalar_subquery()
        )
        segments = (
            select(func.count())
            .select_from(KbSegment)
            .join(KbDocument, KbDocument.id == KbSegment.document_id)
            .where(
                KbSegment.workspace_id == self.workspace_id,
                KbSegment.kb_id == kb_id,
                KbDocument.deleted_at.is_(None),
            )
            .scalar_subquery()
        )
        await self._execute(
            self._update()
            .where(KnowledgeBase.id == kb_id)
            .values(document_count=documents, segment_count=segments)
        )

    def add(self, kb: KnowledgeBase) -> KnowledgeBase:
        return self._add(kb)

    async def flush(self) -> None:
        await self._flush()


class KbDocumentRepository(WorkspaceScopedRepository[KbDocument]):
    # 지운 문서(deleted_at)는 지식 레인의 delete 작업이 행을 지울 때까지 남는다. 조회는 살아 있는
    # 문서만 본다.
    model = KbDocument

    async def get_live(self, kb_id: uuid.UUID, document_id: uuid.UUID) -> KbDocument | None:
        return await self._scalar(
            self._select().where(
                KbDocument.id == document_id,
                KbDocument.kb_id == kb_id,
                KbDocument.deleted_at.is_(None),
            )
        )

    async def get_many_live(
        self, kb_id: uuid.UUID, document_ids: Collection[uuid.UUID]
    ) -> list[KbDocument]:
        if not document_ids:
            return []
        return await self._scalars(
            self._select().where(
                KbDocument.id.in_(list(document_ids)),
                KbDocument.kb_id == kb_id,
                KbDocument.deleted_at.is_(None),
            )
        )

    async def live_names(
        self, kb_id: uuid.UUID, document_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """살아 있는 문서의 id → 이름. 평가셋 정답 문서를 보일 때 쓴다(JSON 열은 읽지 않는다)."""
        if not document_ids:
            return {}
        rows = await self._execute(
            self._select(KbDocument.id, KbDocument.name).where(
                KbDocument.id.in_(list(document_ids)),
                KbDocument.kb_id == kb_id,
                KbDocument.deleted_at.is_(None),
            )
        )
        return {document_id: str(name) for document_id, name in rows.all()}

    async def live_ids_by_name(
        self, kb_id: uuid.UUID, names: Collection[str]
    ) -> dict[str, list[uuid.UUID]]:
        """이름이 정확히 같은 살아 있는 문서의 id(이름 → 목록 순서대로). knowledge_index 노드의 같은
        이름 처리(skip·replace)가 쓴다."""
        if not names:
            return {}
        rows = await self._execute(
            self._select(KbDocument.id, KbDocument.name)
            .where(
                KbDocument.kb_id == kb_id,
                KbDocument.name.in_(list(names)),
                KbDocument.deleted_at.is_(None),
            )
            .order_by(KbDocument.position)
        )
        found: dict[str, list[uuid.UUID]] = {}
        for document_id, name in rows.all():
            found.setdefault(str(name), []).append(document_id)
        return found

    async def status_counts(self, kb_id: uuid.UUID) -> list[tuple[str, bool, bool, int]]:
        """살아 있는 문서를 (indexing_status, enabled, archived)로 묶어 센다. KB 통계용."""
        rows = await self._execute(
            self._select(
                KbDocument.indexing_status,
                KbDocument.enabled,
                KbDocument.archived,
                func.count(),
            )
            .where(KbDocument.kb_id == kb_id, KbDocument.deleted_at.is_(None))
            .group_by(KbDocument.indexing_status, KbDocument.enabled, KbDocument.archived)
        )
        return [
            (str(status), bool(enabled), bool(archived), int(count))
            for status, enabled, archived, count in rows.all()
        ]

    async def error_payloads(
        self, kb_id: uuid.UUID, *, limit: int
    ) -> list[tuple[uuid.UUID, dict[str, Any] | None]]:
        """오류로 끝난 살아 있는 문서의 (id, error) 앞 limit개. error JSON은 조건에 쓰지 않아 읽는
        쪽이 code를 본다."""
        rows = await self._execute(
            self._select(KbDocument.id, KbDocument.error)
            .where(
                KbDocument.kb_id == kb_id,
                KbDocument.deleted_at.is_(None),
                KbDocument.indexing_status == KbIndexingStatus.ERROR,
            )
            .order_by(KbDocument.position)
            .limit(limit)
        )
        return [(document_id, error) for document_id, error in rows.all()]

    def _conditions(
        self, kb_id: uuid.UUID, status: str | None, keyword: str | None
    ) -> list[ColumnElement[bool]]:
        conditions = [KbDocument.kb_id == kb_id, KbDocument.deleted_at.is_(None)]
        if status is not None:
            conditions.append(display_condition(status))
        if keyword:
            conditions.append(KbDocument.name.contains(keyword, autoescape=True))
        return conditions

    async def list_live(
        self,
        kb_id: uuid.UUID,
        *,
        status: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[KbDocument]:
        return await self._scalars(
            self._select()
            .where(*self._conditions(kb_id, status, keyword))
            .order_by(KbDocument.position.desc(), KbDocument.id.desc())
            .limit(limit)
            .offset(offset)
        )

    async def count_live(
        self, kb_id: uuid.UUID, *, status: str | None = None, keyword: str | None = None
    ) -> int:
        statement = self._select(func.count(KbDocument.id)).where(
            *self._conditions(kb_id, status, keyword)
        )
        return int(await self._scalar(statement) or 0)

    async def all_live(self, kb_id: uuid.UUID) -> list[KbDocument]:
        return await self._scalars(
            self._select()
            .where(KbDocument.kb_id == kb_id, KbDocument.deleted_at.is_(None))
            .order_by(KbDocument.position)
        )

    async def live_with_sha256(self, kb_id: uuid.UUID, hashes: Collection[str]) -> list[KbDocument]:
        if not hashes:
            return []
        return await self._scalars(
            self._select().where(
                KbDocument.kb_id == kb_id,
                KbDocument.deleted_at.is_(None),
                KbDocument.sha256.in_(list(hashes)),
            )
        )

    async def next_position(self, kb_id: uuid.UUID) -> int:
        # 지운 문서의 번호도 다시 쓰지 않는다.
        current = await self._scalar(
            self._select(func.max(KbDocument.position)).where(KbDocument.kb_id == kb_id)
        )
        return (current or 0) + 1

    async def in_progress_counts(self, kb_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not kb_ids:
            return {}
        rows = await self._execute(
            self._select(KbDocument.kb_id, func.count())
            .where(
                KbDocument.kb_id.in_(list(kb_ids)),
                KbDocument.deleted_at.is_(None),
                KbDocument.indexing_status.in_(list(IN_PROGRESS_STATUSES)),
            )
            .group_by(KbDocument.kb_id)
        )
        return {kb_id: int(count) for kb_id, count in rows.all()}

    async def requeue_for_embedding(self, kb_id: uuid.UUID, now: datetime) -> None:
        """색인 설정이 바뀐 KB의 문서를 모두 waiting으로(4.4). 분할은 그대로 두고 벡터만 새로."""
        await self._execute(
            self._update()
            .where(KbDocument.kb_id == kb_id, KbDocument.deleted_at.is_(None))
            .values(
                indexing_status=KbIndexingStatus.WAITING,
                completed_segments=0,
                completed_at=None,
                error=None,
                updated_at=now,
            )
        )

    async def transition(
        self, document_id: uuid.UUID, expected: Collection[str], values: dict[str, Any]
    ) -> bool:
        """지우지 않았고 상태가 expected 중 하나일 때만 바꾼다. 바꿨으면 True.

        지식 레인의 작업과 사용자 요청(멈춤·삭제)이 같은 문서를 건드리므로 상태 전이는 모두 이 꼴로
        쓴다. 그사이 사용자가 멈춘 문서를 작업이 다시 indexing으로 되돌리지 않는다.
        """
        result = await self._execute(
            self._update()
            .where(
                KbDocument.id == document_id,
                KbDocument.deleted_at.is_(None),
                KbDocument.indexing_status.in_(list(expected)),
            )
            .values(**values)
        )
        return bool(getattr(result, "rowcount", 0))

    async def list_to_resume(self, kb_ids: Collection[uuid.UUID]) -> list[KbDocument]:
        """살아 있는 KB의 대기·진행 중 문서(만든 순). 기동 복구가 다시 넣는다(6.5)."""
        if not kb_ids:
            return []
        return await self._scalars(
            self._select()
            .where(
                KbDocument.kb_id.in_(list(kb_ids)),
                KbDocument.deleted_at.is_(None),
                KbDocument.indexing_status.in_(list(IN_PROGRESS_STATUSES)),
            )
            .order_by(KbDocument.created_at, KbDocument.id)
        )

    async def list_deleted(self, *, limit: int = 1000) -> list[KbDocument]:
        return await self._scalars(
            self._select()
            .where(KbDocument.deleted_at.is_not(None))
            .order_by(KbDocument.deleted_at, KbDocument.id)
            .limit(limit)
        )

    async def ids_for_kb(
        self, kb_id: uuid.UUID, *, statuses: Collection[str] | None = None, limit: int = 1000
    ) -> list[uuid.UUID]:
        statement = self._select(KbDocument.id).where(KbDocument.kb_id == kb_id)
        if statuses is not None:
            statement = statement.where(
                KbDocument.indexing_status.in_(list(statuses)), KbDocument.deleted_at.is_(None)
            )
        return await self._scalars(statement.order_by(KbDocument.id).limit(limit))

    async def storage_keys(self, document_ids: Collection[uuid.UUID]) -> set[str]:
        if not document_ids:
            return set()
        return set(
            await self._scalars(
                self._select(KbDocument.storage_key).where(KbDocument.id.in_(list(document_ids)))
            )
        )

    async def referenced_keys(self, keys: Collection[str]) -> set[str]:
        """문서 행(지운 문서 포함)이 가리키는 원본 키. 행 없는 원본 정리가 쓴다."""
        if not keys:
            return set()
        return set(
            await self._scalars(
                self._select(KbDocument.storage_key).where(KbDocument.storage_key.in_(list(keys)))
            )
        )

    async def existing_ids(self, document_ids: Collection[uuid.UUID]) -> set[uuid.UUID]:
        if not document_ids:
            return set()
        return set(
            await self._scalars(
                self._select(KbDocument.id).where(KbDocument.id.in_(list(document_ids)))
            )
        )

    async def delete(self, document_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbDocument.id == document_id))

    def add(self, document: KbDocument) -> KbDocument:
        return self._add(document)

    async def flush(self) -> None:
        await self._flush()


class KbSegmentRepository(WorkspaceScopedRepository[KbSegment]):
    model = KbSegment

    async def get_in_document(
        self, document_id: uuid.UUID, segment_id: uuid.UUID
    ) -> KbSegment | None:
        return await self._scalar(
            self._select().where(KbSegment.id == segment_id, KbSegment.document_id == document_id)
        )

    def _conditions(
        self, document_id: uuid.UUID, keyword: str | None, enabled: bool | None
    ) -> list[ColumnElement[bool]]:
        conditions = [KbSegment.document_id == document_id]
        if keyword:
            conditions.append(
                or_(
                    KbSegment.content.contains(keyword, autoescape=True),
                    KbSegment.answer.contains(keyword, autoescape=True),
                )
            )
        if enabled is not None:
            conditions.append(KbSegment.enabled.is_(enabled))
        return conditions

    async def list_page(
        self,
        document_id: uuid.UUID,
        *,
        keyword: str | None = None,
        enabled: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[KbSegment]:
        return await self._scalars(
            self._select()
            .where(*self._conditions(document_id, keyword, enabled))
            .order_by(KbSegment.position)
            .limit(limit)
            .offset(offset)
        )

    async def count(
        self, document_id: uuid.UUID, *, keyword: str | None = None, enabled: bool | None = None
    ) -> int:
        statement = self._select(func.count(KbSegment.id)).where(
            *self._conditions(document_id, keyword, enabled)
        )
        return int(await self._scalar(statement) or 0)

    async def next_position(self, document_id: uuid.UUID) -> int:
        current = await self._scalar(
            self._select(func.max(KbSegment.position)).where(KbSegment.document_id == document_id)
        )
        return (current or 0) + 1

    async def document_totals(self, document_id: uuid.UUID) -> tuple[int, int, int, int]:
        """(청크 수, 색인이 끝난 청크 수, 글자 수 합, 토큰 합)."""
        completed = func.sum(case((KbSegment.status == KbSegmentStatus.COMPLETED, 1), else_=0))
        row = (
            await self._execute(
                self._select(
                    func.count(),
                    func.coalesce(completed, 0),
                    func.coalesce(func.sum(KbSegment.word_count), 0),
                    func.coalesce(func.sum(KbSegment.tokens), 0),
                ).where(KbSegment.document_id == document_id)
            )
        ).one()
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])

    async def delete(self, segment_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbSegment.id == segment_id))

    async def get_many(self, segment_ids: Collection[uuid.UUID]) -> list[KbSegment]:
        if not segment_ids:
            return []
        return await self._scalars(self._select().where(KbSegment.id.in_(list(segment_ids))))

    async def units_after(self, document_id: uuid.UUID, after: int, limit: int) -> list[KbSegment]:
        """유닛인 세그먼트(General·Q&A)를 unit_no 순으로 after 다음부터."""
        return await self._scalars(
            self._select()
            .where(
                KbSegment.document_id == document_id,
                KbSegment.unit_no.is_not(None),
                KbSegment.unit_no > after,
            )
            .order_by(KbSegment.unit_no)
            .limit(limit)
        )

    async def kb_unit_states(self, kb_id: uuid.UUID, after: int, limit: int) -> list[UnitState]:
        # 고아 정리용. 본문 열은 읽지 않는다(10만 유닛이면 수백 MB다).
        rows = await self._execute(
            self._select(
                KbSegment.id,
                KbSegment.unit_no,
                KbSegment.index_state,
                KbSegment.enabled,
                KbSegment.document_id,
            )
            .where(
                KbSegment.kb_id == kb_id,
                KbSegment.unit_no.is_not(None),
                KbSegment.unit_no > after,
            )
            .order_by(KbSegment.unit_no)
            .limit(limit)
        )
        return [
            UnitState(row_id, int(unit_no), dict(state or {}), bool(enabled), document_id)
            for row_id, unit_no, state, enabled, document_id in rows.all()
        ]

    async def has_units(self, kb_id: uuid.UUID) -> bool:
        found = await self._scalar(
            self._select(KbSegment.id).where(KbSegment.kb_id == kb_id).limit(1)
        )
        return found is not None

    async def requeue_fts(self, kb_id: uuid.UUID, now: datetime) -> None:
        """유닛의 전문 색인을 pending으로(index.db를 잃었을 때, 5.5)."""
        await self._execute(
            self._update()
            .where(KbSegment.kb_id == kb_id, KbSegment.unit_no.is_not(None))
            .values(
                index_state=func.json_set(KbSegment.index_state, "$.fts", "pending"),
                status=KbSegmentStatus.WAITING,
                updated_at=now,
            )
        )

    async def unit_hashes(
        self, document_id: uuid.UUID, after: int, limit: int
    ) -> list[tuple[int, str]]:
        rows = await self._execute(
            self._select(KbSegment.unit_no, KbSegment.content_hash)
            .where(
                KbSegment.document_id == document_id,
                KbSegment.unit_no.is_not(None),
                KbSegment.unit_no > after,
            )
            .order_by(KbSegment.unit_no)
            .limit(limit)
        )
        return [(int(unit_no), str(digest)) for unit_no, digest in rows.all()]

    def _indexed(self) -> list[ColumnElement[bool]]:
        # 색인이 끝난 켜진 유닛. 색인 설정을 바꾸면 같은 트랜잭션에서 waiting으로 돌아가므로
        # completed면 지금 설정의 벡터다(4.4). 끈 청크는 색인 행이 없다. 지운 문서의 유닛도
        # 쓰지 않는다(KbDocument를 조인해서 부른다).
        return [
            KbSegment.unit_no.is_not(None),
            KbSegment.enabled.is_(True),
            KbSegment.status == KbSegmentStatus.COMPLETED,
            KbDocument.workspace_id == self.workspace_id,
            KbDocument.deleted_at.is_(None),
        ]

    async def indexed_units(self, hashes: Collection[str]) -> list[tuple[str, uuid.UUID, int]]:
        """이 글(content_hash)로 색인이 끝난 유닛. (해시, KB)마다 하나만(임베딩 캐시, 6.6)."""
        if not hashes:
            return []
        rows = await self._execute(
            self._select(KbSegment.content_hash, KbSegment.kb_id, func.max(KbSegment.unit_no))
            .join(KbDocument, KbDocument.id == KbSegment.document_id)
            .where(KbSegment.content_hash.in_(list(hashes)), *self._indexed())
            .group_by(KbSegment.content_hash, KbSegment.kb_id)
        )
        return [(str(digest), kb_id, int(unit_no)) for digest, kb_id, unit_no in rows.all()]

    async def indexed_hashes(self, kb_id: uuid.UUID, unit_nos: Collection[int]) -> dict[int, str]:
        """그 유닛들 중 아직 색인이 끝난 채인 것의 글(content_hash)."""
        if not unit_nos:
            return {}
        rows = await self._execute(
            self._select(KbSegment.unit_no, KbSegment.content_hash)
            .join(KbDocument, KbDocument.id == KbSegment.document_id)
            .where(
                KbSegment.kb_id == kb_id, KbSegment.unit_no.in_(list(unit_nos)), *self._indexed()
            )
        )
        return {int(unit_no): str(digest) for unit_no, digest in rows.all()}

    async def delete_some_for_document(self, document_id: uuid.UUID, limit: int) -> int:
        ids = await self._scalars(
            self._select(KbSegment.id).where(KbSegment.document_id == document_id).limit(limit)
        )
        if ids:
            await self._execute(self._delete().where(KbSegment.id.in_(ids)))
        return len(ids)

    async def has_waiting(self, document_id: uuid.UUID) -> bool:
        """색인을 기다리는 켜진 청크가 남았는지. 유닛이 pending이면 그 청크(부모)는 waiting이다."""
        found = await self._scalar(
            self._select(KbSegment.id)
            .where(
                KbSegment.document_id == document_id,
                KbSegment.status == KbSegmentStatus.WAITING,
                KbSegment.enabled.is_(True),
            )
            .limit(1)
        )
        return found is not None

    async def statuses_of(self, document_id: uuid.UUID) -> dict[uuid.UUID, str]:
        rows = await self._execute(
            self._select(KbSegment.id, KbSegment.status).where(KbSegment.document_id == document_id)
        )
        return {segment_id: str(status) for segment_id, status in rows.all()}

    async def requeue_vectors(self, kb_id: uuid.UUID, now: datetime) -> None:
        """모든 청크를 waiting, 유닛인 청크의 벡터를 pending으로(색인 설정 변경, 4.4)."""
        await self._execute(
            self._update()
            .where(KbSegment.kb_id == kb_id)
            .values(status=KbSegmentStatus.WAITING, updated_at=now)
        )
        await self._execute(
            self._update()
            .where(KbSegment.kb_id == kb_id, KbSegment.unit_no.is_not(None))
            .values(index_state=_pending_vector(KbSegment.index_state))
        )

    def add(self, segment: KbSegment) -> KbSegment:
        return self._add(segment)

    async def flush(self) -> None:
        await self._flush()


class KbChildChunkRepository(WorkspaceScopedRepository[KbChildChunk]):
    model = KbChildChunk

    async def list_for_segments(self, segment_ids: Collection[uuid.UUID]) -> list[KbChildChunk]:
        if not segment_ids:
            return []
        return await self._scalars(
            self._select()
            .where(KbChildChunk.segment_id.in_(list(segment_ids)))
            .order_by(KbChildChunk.segment_id, KbChildChunk.position)
        )

    async def get_in_segment(
        self, segment_id: uuid.UUID, child_id: uuid.UUID
    ) -> KbChildChunk | None:
        return await self._scalar(
            self._select().where(KbChildChunk.id == child_id, KbChildChunk.segment_id == segment_id)
        )

    async def next_position(self, segment_id: uuid.UUID) -> int:
        current = await self._scalar(
            self._select(func.max(KbChildChunk.position)).where(
                KbChildChunk.segment_id == segment_id
            )
        )
        return (current or 0) + 1

    async def delete(self, child_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbChildChunk.id == child_id))

    async def delete_for_segment(self, segment_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbChildChunk.segment_id == segment_id))

    async def get_many(self, child_ids: Collection[uuid.UUID]) -> list[KbChildChunk]:
        if not child_ids:
            return []
        return await self._scalars(self._select().where(KbChildChunk.id.in_(list(child_ids))))

    async def units_after(
        self, document_id: uuid.UUID, after: int, limit: int
    ) -> list[tuple[KbChildChunk, bool]]:
        """(자식 청크, 부모가 켜져 있는지)를 unit_no 순으로 after 다음부터."""
        rows = await self._execute(
            self._select(KbChildChunk, KbSegment.enabled)
            .join(KbSegment, KbSegment.id == KbChildChunk.segment_id)
            .where(KbChildChunk.document_id == document_id, KbChildChunk.unit_no > after)
            .order_by(KbChildChunk.unit_no)
            .limit(limit)
        )
        return [(child, bool(enabled)) for child, enabled in rows.all()]

    async def kb_unit_states(self, kb_id: uuid.UUID, after: int, limit: int) -> list[UnitState]:
        rows = await self._execute(
            self._select(
                KbChildChunk.id,
                KbChildChunk.unit_no,
                KbChildChunk.index_state,
                KbSegment.enabled,
                KbChildChunk.document_id,
            )
            .join(KbSegment, KbSegment.id == KbChildChunk.segment_id)
            .where(KbChildChunk.kb_id == kb_id, KbChildChunk.unit_no > after)
            .order_by(KbChildChunk.unit_no)
            .limit(limit)
        )
        return [
            UnitState(row_id, int(unit_no), dict(state or {}), bool(enabled), document_id)
            for row_id, unit_no, state, enabled, document_id in rows.all()
        ]

    async def requeue_fts(self, kb_id: uuid.UUID) -> None:
        await self._execute(
            self._update()
            .where(KbChildChunk.kb_id == kb_id)
            .values(index_state=func.json_set(KbChildChunk.index_state, "$.fts", "pending"))
        )

    async def unit_hashes(
        self, document_id: uuid.UUID, after: int, limit: int
    ) -> list[tuple[int, str]]:
        rows = await self._execute(
            self._select(KbChildChunk.unit_no, KbChildChunk.content_hash)
            .where(KbChildChunk.document_id == document_id, KbChildChunk.unit_no > after)
            .order_by(KbChildChunk.unit_no)
            .limit(limit)
        )
        return [(int(unit_no), str(digest)) for unit_no, digest in rows.all()]

    def _indexed(self) -> list[ColumnElement[bool]]:
        # 자식 청크에는 status가 없다. 부모가 completed면 자식이 모두 끝났다. 부모 청크와 문서를
        # 조인해서 부른다.
        return [
            KbSegment.workspace_id == self.workspace_id,
            KbSegment.enabled.is_(True),
            KbSegment.status == KbSegmentStatus.COMPLETED,
            KbDocument.workspace_id == self.workspace_id,
            KbDocument.deleted_at.is_(None),
        ]

    async def indexed_units(self, hashes: Collection[str]) -> list[tuple[str, uuid.UUID, int]]:
        if not hashes:
            return []
        rows = await self._execute(
            self._select(
                KbChildChunk.content_hash, KbChildChunk.kb_id, func.max(KbChildChunk.unit_no)
            )
            .join(KbSegment, KbSegment.id == KbChildChunk.segment_id)
            .join(KbDocument, KbDocument.id == KbChildChunk.document_id)
            .where(KbChildChunk.content_hash.in_(list(hashes)), *self._indexed())
            .group_by(KbChildChunk.content_hash, KbChildChunk.kb_id)
        )
        return [(str(digest), kb_id, int(unit_no)) for digest, kb_id, unit_no in rows.all()]

    async def indexed_hashes(self, kb_id: uuid.UUID, unit_nos: Collection[int]) -> dict[int, str]:
        if not unit_nos:
            return {}
        rows = await self._execute(
            self._select(KbChildChunk.unit_no, KbChildChunk.content_hash)
            .join(KbSegment, KbSegment.id == KbChildChunk.segment_id)
            .join(KbDocument, KbDocument.id == KbChildChunk.document_id)
            .where(
                KbChildChunk.kb_id == kb_id,
                KbChildChunk.unit_no.in_(list(unit_nos)),
                *self._indexed(),
            )
        )
        return {int(unit_no): str(digest) for unit_no, digest in rows.all()}

    async def delete_some_for_document(self, document_id: uuid.UUID, limit: int) -> int:
        ids = await self._scalars(
            self._select(KbChildChunk.id)
            .where(KbChildChunk.document_id == document_id)
            .limit(limit)
        )
        if ids:
            await self._execute(self._delete().where(KbChildChunk.id.in_(ids)))
        return len(ids)

    async def requeue_vectors(self, kb_id: uuid.UUID) -> None:
        await self._execute(
            self._update()
            .where(KbChildChunk.kb_id == kb_id)
            .values(index_state=_pending_vector(KbChildChunk.index_state))
        )

    def add(self, child: KbChildChunk) -> KbChildChunk:
        return self._add(child)

    async def flush(self) -> None:
        await self._flush()


class KbMetadataFieldRepository(WorkspaceScopedRepository[KbMetadataField]):
    model = KbMetadataField

    async def list_for_kb(self, kb_id: uuid.UUID) -> list[KbMetadataField]:
        return await self._scalars(
            self._select()
            .where(KbMetadataField.kb_id == kb_id)
            .order_by(KbMetadataField.position, KbMetadataField.id)
        )

    async def get_in_kb(self, kb_id: uuid.UUID, field_id: uuid.UUID) -> KbMetadataField | None:
        return await self._scalar(
            self._select().where(KbMetadataField.id == field_id, KbMetadataField.kb_id == kb_id)
        )

    async def name_taken(
        self, kb_id: uuid.UUID, name: str, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        statement = self._select(KbMetadataField.id).where(
            KbMetadataField.kb_id == kb_id, KbMetadataField.name == name
        )
        if exclude_id is not None:
            statement = statement.where(KbMetadataField.id != exclude_id)
        return await self._scalar(statement.limit(1)) is not None

    async def next_position(self, kb_id: uuid.UUID) -> int:
        current = await self._scalar(
            self._select(func.max(KbMetadataField.position)).where(KbMetadataField.kb_id == kb_id)
        )
        return (current or 0) + 1

    async def delete_many(self, field_ids: Collection[uuid.UUID]) -> None:
        if field_ids:
            await self._execute(self._delete().where(KbMetadataField.id.in_(list(field_ids))))

    async def delete_for_kb(self, kb_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbMetadataField.kb_id == kb_id))

    def add(self, field: KbMetadataField) -> KbMetadataField:
        return self._add(field)

    async def flush(self) -> None:
        await self._flush()


class KbDocumentMetadataRepository(WorkspaceScopedRepository[KbDocumentMetadata]):
    model = KbDocumentMetadata

    async def list_for_documents(
        self, document_ids: Collection[uuid.UUID]
    ) -> list[KbDocumentMetadata]:
        if not document_ids:
            return []
        return await self._scalars(
            self._select().where(KbDocumentMetadata.document_id.in_(list(document_ids)))
        )

    async def list_for_field(
        self, field_id: uuid.UUID, document_ids: Collection[uuid.UUID]
    ) -> list[KbDocumentMetadata]:
        if not document_ids:
            return []
        return await self._scalars(
            self._select().where(
                KbDocumentMetadata.field_id == field_id,
                KbDocumentMetadata.document_id.in_(list(document_ids)),
            )
        )

    async def use_counts(self, kb_id: uuid.UUID) -> dict[uuid.UUID, int]:
        rows = await self._execute(
            self._select(KbDocumentMetadata.field_id, func.count())
            .join(KbDocument, KbDocument.id == KbDocumentMetadata.document_id)
            .where(KbDocumentMetadata.kb_id == kb_id, KbDocument.deleted_at.is_(None))
            .group_by(KbDocumentMetadata.field_id)
        )
        return {field_id: int(count) for field_id, count in rows.all()}

    async def delete_for_fields(self, field_ids: Collection[uuid.UUID]) -> None:
        if field_ids:
            await self._execute(
                self._delete().where(KbDocumentMetadata.field_id.in_(list(field_ids)))
            )

    async def delete_values(self, field_id: uuid.UUID, document_ids: Collection[uuid.UUID]) -> None:
        if document_ids:
            await self._execute(
                self._delete().where(
                    KbDocumentMetadata.field_id == field_id,
                    KbDocumentMetadata.document_id.in_(list(document_ids)),
                )
            )

    async def delete_for_document(self, document_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbDocumentMetadata.document_id == document_id))

    async def delete_for_kb(self, kb_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbDocumentMetadata.kb_id == kb_id))

    def add(self, value: KbDocumentMetadata) -> KbDocumentMetadata:
        return self._add(value)

    async def flush(self) -> None:
        await self._flush()


class KbQueryLogRetentionRepository(WorkspaceScopedRepository[KbQueryLog]):
    # 보존 정리 전용(4.5). 기록을 남기고 읽는 쪽은 검색(M23)이다.
    model = KbQueryLog

    async def delete_before(self, cutoff: datetime) -> int:
        result = await self._execute(self._delete().where(KbQueryLog.created_at < cutoff))
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_for_kb(self, kb_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbQueryLog.kb_id == kb_id))


class KbEvalRetentionRepository(WorkspaceScopedRepository[KbEvalRun]):
    model = KbEvalRun

    async def trim(self, kb_id: uuid.UUID, keep: int) -> int:
        """KB마다 최근 keep개만 남긴다(4.5)."""
        stale = await self._scalars(
            self._select(KbEvalRun.id)
            .where(KbEvalRun.kb_id == kb_id)
            .order_by(KbEvalRun.created_at.desc(), KbEvalRun.id.desc())
            .offset(keep)
        )
        if stale:
            await self._execute(self._delete().where(KbEvalRun.id.in_(stale)))
        return len(stale)

    async def kb_ids(self) -> list[uuid.UUID]:
        return await self._scalars(self._select(KbEvalRun.kb_id).distinct())

    async def delete_for_kb(self, kb_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbEvalRun.kb_id == kb_id))


class KbEvalCaseRetentionRepository(WorkspaceScopedRepository[KbEvalCase]):
    model = KbEvalCase

    async def delete_for_kb(self, kb_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbEvalCase.kb_id == kb_id))


class KbCounterRepository(GlobalRepository[KbCounter]):
    model = KbCounter

    async def raise_to(self, minimum: int, name: str = UNIT_NO) -> int:
        """번호가 minimum보다 작으면 올린다. 올린 뒤의 값을 돌려준다.

        app.db만 옛 백업으로 되돌리면 번호가 index.db에 이미 있는 rowid로 거슬러 간다. 그러면 새
        유닛이 남의 벡터 행을 물려받으므로 기동 때 index.db의 최대 rowid 이상으로 맞춘다.
        """
        current = await self.session.scalar(select(KbCounter.value).where(KbCounter.name == name))
        if current is None:
            self.session.add(KbCounter(name=name, value=max(minimum, 0)))
            await self.session.flush()
            return max(minimum, 0)
        if current >= minimum:
            return int(current)
        await self.session.execute(
            update(KbCounter).where(KbCounter.name == name).values(value=minimum)
        )
        return minimum

    async def allocate(self, count: int, name: str = UNIT_NO) -> range:
        """번호 count개를 받는다. 쓰기 트랜잭션 안에서 불러야 겹치지 않는다(쓰기 락이 하나다)."""
        if count <= 0:
            return range(0)
        statement = (
            update(KbCounter)
            .where(KbCounter.name == name)
            .values(value=KbCounter.value + count)
            .returning(KbCounter.value)
        )
        last = (await self.session.execute(statement)).scalar_one_or_none()
        if last is None:
            # 0007이 unit_no 행을 넣는다. 다른 이름의 수열은 처음 쓸 때 만든다.
            self.session.add(KbCounter(name=name, value=count))
            await self.session.flush()
            last = count
        return range(last - count + 1, last + 1)
