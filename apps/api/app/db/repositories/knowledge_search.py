"""검색·평가·질의 기록 리포지토리(3단계 설계 7장). 모두 workspace_id 범위 안이다.

검색은 index.db 후보를 이 모듈로 거른다. 끈 청크, 지웠거나 보관했거나 끈 문서의 유닛은 표에서
빠지고(5.4), 메타데이터 필터는 kb_document_metadata에 대한 EXISTS 조건 하나씩으로 바뀐다(7.5).
"""

import uuid
from collections.abc import Collection, Mapping
from typing import Any, cast

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, true

from app.db.models import (
    KbChildChunk,
    KbDocument,
    KbDocumentMetadata,
    KbEvalCase,
    KbEvalRun,
    KbQueryLog,
    KbSegment,
)
from app.db.models.enums import KbIndexingStatus
from app.db.repositories.base import WorkspaceScopedRepository
from app.knowledge.retrieval.filters import FilterClause, FilterPlan

# IN 목록 한 번의 크기. SQLite 바인드 변수 상한(32766)보다 한참 아래로 둔다.
_BATCH = 500


def _batches[T](items: Collection[T]) -> list[list[T]]:
    values = list(items)
    return [values[start : start + _BATCH] for start in range(0, len(values), _BATCH)]


def _value_column(clause: FilterClause) -> Any:
    if clause.field_type == "number":
        return KbDocumentMetadata.value_number
    if clause.field_type == "time":
        return KbDocumentMetadata.value_time
    return KbDocumentMetadata.value_string


def _match(clause: FilterClause) -> ColumnElement[bool]:
    # 글자 비교는 대소문자를 구분한다. SQLite LIKE는 ASCII 대소문자를 무시해 instr·substr를 쓴다.
    column = _value_column(clause)
    value = clause.value
    match clause.operator:
        case "is" | "=":
            return column == value
        case "is not" | "≠":
            return column != value
        case ">" | "after":
            return column > value
        case "<" | "before":
            return column < value
        case "≥":
            return column >= value
        case "≤":
            return column <= value
        case "contains":
            return func.instr(column, value) > 0
        case "not contains":
            return func.instr(column, value) == 0
        case "start with":
            text = str(value)
            return func.substr(column, 1, len(text)) == text
        case "end with":
            text = str(value)
            return func.substr(column, -len(text)) == text
        case "in":
            return column.in_(list(_items(value)))
        case "not in":
            return column.not_in(list(_items(value)))
        case _:
            raise ValueError(f"알 수 없는 연산자: {clause.operator}")


def _items(value: object) -> tuple[object, ...]:
    return cast(tuple[object, ...], value) if isinstance(value, tuple) else (value,)


def _present(clause: FilterClause) -> ColumnElement[bool]:
    column = _value_column(clause)
    if clause.field_type == "string":
        return and_(column.is_not(None), column != "")
    return column.is_not(None)


def filter_condition(
    plan: FilterPlan, field_ids: Mapping[str, uuid.UUID], workspace_id: uuid.UUID
) -> ColumnElement[bool]:
    """FilterPlan을 kb_document에 거는 조건으로. 조건마다 EXISTS 하나이고 값은 바인드 변수다.

    값이 없는 문서는 is not·not contains·not in에도 맞지 않는다(값이 있어야 비교한다). empty만 값이
    없거나 빈 글인 문서를 고른다.
    """
    parts: list[ColumnElement[bool]] = []
    for clause in plan.clauses:
        base = [
            KbDocumentMetadata.workspace_id == workspace_id,
            KbDocumentMetadata.document_id == KbDocument.id,
            KbDocumentMetadata.field_id == field_ids[clause.name],
        ]
        if clause.operator == "empty":
            parts.append(~exists().where(*base, _present(clause)))
        elif clause.operator == "not empty":
            parts.append(exists().where(*base, _present(clause)))
        else:
            parts.append(exists().where(*base, _match(clause)))
    if not parts:
        return true()
    return and_(*parts) if plan.logical_operator == "and" else or_(*parts)


class KbSearchDocumentRepository(WorkspaceScopedRepository[KbDocument]):
    model = KbDocument

    def _searchable(self, kb_id: uuid.UUID, *, completed_only: bool) -> list[ColumnElement[bool]]:
        # 보관·끈·지운 문서는 검색하지 않는다(4.4). search_while_indexing이 꺼져 있으면 색인이 끝난
        # 문서만이다(6.7).
        conditions = [
            KbDocument.kb_id == kb_id,
            KbDocument.deleted_at.is_(None),
            KbDocument.enabled.is_(True),
            KbDocument.archived.is_(False),
        ]
        if completed_only:
            conditions.append(KbDocument.indexing_status == KbIndexingStatus.COMPLETED)
        return conditions

    async def searchable(
        self,
        kb_id: uuid.UUID,
        *,
        completed_only: bool,
        plan: FilterPlan | None = None,
        field_ids: Mapping[str, uuid.UUID] | None = None,
    ) -> dict[uuid.UUID, str]:
        """검색할 문서 id → indexing_status. plan이 있으면 메타데이터 조건에 맞는 것만."""
        statement = self._select(KbDocument.id, KbDocument.indexing_status).where(
            *self._searchable(kb_id, completed_only=completed_only)
        )
        if plan is not None and plan.clauses:
            statement = statement.where(filter_condition(plan, field_ids or {}, self.workspace_id))
        rows = await self._execute(statement)
        return {doc_id: status for doc_id, status in rows.all()}

    async def in_batch(self, kb_id: uuid.UUID, batch: str) -> list[KbDocument]:
        # datasets indexing-status. 같은 요청으로 올린 문서 묶음(4.1 batch).
        return await self._scalars(
            self._select()
            .where(
                KbDocument.kb_id == kb_id,
                KbDocument.batch == batch,
                KbDocument.deleted_at.is_(None),
            )
            .order_by(KbDocument.position)
        )

    async def word_count(self, kb_id: uuid.UUID) -> int:
        statement = self._select(func.coalesce(func.sum(KbDocument.word_count), 0)).where(
            KbDocument.kb_id == kb_id, KbDocument.deleted_at.is_(None)
        )
        return int(await self._scalar(statement) or 0)

    async def count_rows(self, kb_id: uuid.UUID) -> int:
        # 지우는 중인 문서도 센다. index.db에 아직 그 유닛이 남아 있을 수 있다.
        statement = self._select(func.count(KbDocument.id)).where(KbDocument.kb_id == kb_id)
        return int(await self._scalar(statement) or 0)

    async def get_many(self, document_ids: Collection[uuid.UUID]) -> list[KbDocument]:
        found: list[KbDocument] = []
        for batch in _batches(document_ids):
            found.extend(await self._scalars(self._select().where(KbDocument.id.in_(batch))))
        return found

    async def bump_hits(self, document_ids: Collection[uuid.UUID]) -> None:
        # updated_at은 그대로 둔다. 기본 메타데이터 last_update_date가 이 값이다.
        for batch in _batches(document_ids):
            await self._execute(
                self._update()
                .where(KbDocument.id.in_(batch))
                .values(hit_count=KbDocument.hit_count + 1, updated_at=KbDocument.updated_at)
            )


class KbSearchSegmentRepository(WorkspaceScopedRepository[KbSegment]):
    model = KbSegment

    async def units(
        self, kb_id: uuid.UUID, unit_nos: Collection[int]
    ) -> list[tuple[int, uuid.UUID, uuid.UUID]]:
        """General·Q&A 유닛 → (unit_no, segment_id, document_id). 켜진 청크만."""
        rows: list[tuple[int, uuid.UUID, uuid.UUID]] = []
        for batch in _batches(unit_nos):
            result = await self._execute(
                self._select(KbSegment.unit_no, KbSegment.id, KbSegment.document_id).where(
                    KbSegment.kb_id == kb_id,
                    KbSegment.unit_no.in_(batch),
                    KbSegment.enabled.is_(True),
                )
            )
            rows.extend((int(unit), seg, doc) for unit, seg, doc in result.all())
        return rows

    async def get_many(self, segment_ids: Collection[uuid.UUID]) -> list[KbSegment]:
        found: list[KbSegment] = []
        for batch in _batches(segment_ids):
            found.extend(await self._scalars(self._select().where(KbSegment.id.in_(batch))))
        return found

    async def bump_hits(self, segment_ids: Collection[uuid.UUID]) -> None:
        for batch in _batches(segment_ids):
            await self._execute(
                self._update()
                .where(KbSegment.id.in_(batch))
                .values(hit_count=KbSegment.hit_count + 1, updated_at=KbSegment.updated_at)
            )


class KbSearchChildRepository(WorkspaceScopedRepository[KbChildChunk]):
    model = KbChildChunk

    async def units(
        self, kb_id: uuid.UUID, unit_nos: Collection[int]
    ) -> list[tuple[int, uuid.UUID, uuid.UUID, uuid.UUID]]:
        """Parent-child 유닛 → (unit_no, child_id, 부모 segment_id, document_id).

        부모 청크가 켜진 것만이다.
        """
        rows: list[tuple[int, uuid.UUID, uuid.UUID, uuid.UUID]] = []
        for batch in _batches(unit_nos):
            result = await self._execute(
                self._select(
                    KbChildChunk.unit_no,
                    KbChildChunk.id,
                    KbChildChunk.segment_id,
                    KbChildChunk.document_id,
                )
                .join(KbSegment, KbSegment.id == KbChildChunk.segment_id)
                .where(
                    KbChildChunk.kb_id == kb_id,
                    KbChildChunk.unit_no.in_(batch),
                    KbSegment.workspace_id == self.workspace_id,
                    KbSegment.enabled.is_(True),
                )
            )
            rows.extend((int(unit), child, seg, doc) for unit, child, seg, doc in result.all())
        return rows

    async def get_many(self, child_ids: Collection[uuid.UUID]) -> list[KbChildChunk]:
        found: list[KbChildChunk] = []
        for batch in _batches(child_ids):
            found.extend(await self._scalars(self._select().where(KbChildChunk.id.in_(batch))))
        return found


class KbQueryLogRepository(WorkspaceScopedRepository[KbQueryLog]):
    model = KbQueryLog

    async def list_page(self, kb_id: uuid.UUID, *, limit: int, offset: int) -> list[KbQueryLog]:
        return await self._scalars(
            self._select()
            .where(KbQueryLog.kb_id == kb_id)
            .order_by(KbQueryLog.created_at.desc(), KbQueryLog.id.desc())
            .limit(limit)
            .offset(offset)
        )

    async def count(self, kb_id: uuid.UUID) -> int:
        statement = self._select(func.count(KbQueryLog.id)).where(KbQueryLog.kb_id == kb_id)
        return int(await self._scalar(statement) or 0)

    def add(self, row: KbQueryLog) -> KbQueryLog:
        return self._add(row)

    async def flush(self) -> None:
        await self._flush()


class KbEvalCaseRepository(WorkspaceScopedRepository[KbEvalCase]):
    model = KbEvalCase

    async def list_for_kb(self, kb_id: uuid.UUID) -> list[KbEvalCase]:
        return await self._scalars(
            self._select()
            .where(KbEvalCase.kb_id == kb_id)
            .order_by(KbEvalCase.created_at, KbEvalCase.id)
        )

    async def count(self, kb_id: uuid.UUID) -> int:
        statement = self._select(func.count(KbEvalCase.id)).where(KbEvalCase.kb_id == kb_id)
        return int(await self._scalar(statement) or 0)

    async def get_in_kb(self, kb_id: uuid.UUID, case_id: uuid.UUID) -> KbEvalCase | None:
        return await self._scalar(
            self._select().where(KbEvalCase.id == case_id, KbEvalCase.kb_id == kb_id)
        )

    async def delete(self, case_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbEvalCase.id == case_id))

    async def delete_all(self, kb_id: uuid.UUID) -> None:
        await self._execute(self._delete().where(KbEvalCase.kb_id == kb_id))

    def add(self, row: KbEvalCase) -> KbEvalCase:
        return self._add(row)

    async def flush(self) -> None:
        await self._flush()


class KbEvalRunRepository(WorkspaceScopedRepository[KbEvalRun]):
    model = KbEvalRun

    async def list_for_kb(self, kb_id: uuid.UUID, *, limit: int) -> list[KbEvalRun]:
        return await self._scalars(
            self._select()
            .where(KbEvalRun.kb_id == kb_id)
            .order_by(KbEvalRun.created_at.desc(), KbEvalRun.id.desc())
            .limit(limit)
        )

    async def get_in_kb(self, kb_id: uuid.UUID, run_id: uuid.UUID) -> KbEvalRun | None:
        return await self._scalar(
            self._select().where(KbEvalRun.id == run_id, KbEvalRun.kb_id == kb_id)
        )

    async def prune(self, kb_id: uuid.UUID, *, keep: int) -> None:
        """KB마다 최근 keep개만 남긴다(4.5)."""
        kept = (
            select(KbEvalRun.id)
            .where(KbEvalRun.workspace_id == self.workspace_id, KbEvalRun.kb_id == kb_id)
            .order_by(KbEvalRun.created_at.desc(), KbEvalRun.id.desc())
            .limit(keep)
        )
        await self._execute(
            self._delete().where(KbEvalRun.kb_id == kb_id, KbEvalRun.id.not_in(kept))
        )

    def add(self, row: KbEvalRun) -> KbEvalRun:
        return self._add(row)

    async def flush(self) -> None:
        await self._flush()
