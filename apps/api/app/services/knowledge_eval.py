"""검색 평가(3단계 설계 7.7): 평가셋 관리, CSV 가져오기, 평가 실행과 저장(확장).

평가 실행은 질문마다 검색(source=eval, 질의 임베딩은 interactive)을 한 번씩 하고 R@1·R@3·R@5·
MRR@10을 낸다. top_k가 작아도 상위 10개까지 봐야 R@5·MRR@10을 잴 수 있어 검색은 최소 10개로
돌린다(순위는 top_k와 상관없이 같다). 저장한 실행은 KB마다 최근 30개만 남는다(4.5).
"""

import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import structlog
from pydantic import ValidationError

from app.config import Settings
from app.db.engine import Database
from app.db.models import KbEvalCase, KbEvalRun
from app.db.repositories.knowledge import KbDocumentRepository, KbSegmentRepository
from app.db.repositories.knowledge_search import KbEvalCaseRepository, KbEvalRunRepository
from app.db.types import new_id, utcnow
from app.knowledge.eval import (
    EVAL_DEPTH,
    MAX_CSV_BYTES,
    MAX_EVAL_CASES,
    CsvCase,
    first_hit,
    metrics,
    parse_csv,
)
from app.schemas.knowledge import RetrievalSettings
from app.schemas.retrieval import (
    EvalCaseCreate,
    EvalCaseList,
    EvalCaseOut,
    EvalCaseResult,
    EvalExpected,
    EvalHit,
    EvalImportProblem,
    EvalImportResult,
    EvalMetrics,
    EvalRunList,
    EvalRunOut,
    EvalRunRequest,
    EvalRunSummary,
    RetrievalWarning,
)
from app.services.errors import ServiceError, not_found
from app.services.knowledge import active_kb, parse_retrieval
from app.services.knowledge_search import KnowledgeSearchService

log = structlog.get_logger("app.services.knowledge_eval")

KEEP_RUNS = 30
TOP_HITS = 5


def _expected(raw: dict[str, Any] | None) -> EvalExpected:
    try:
        return EvalExpected.model_validate(raw or {})
    except ValidationError:
        return EvalExpected()


def _case_out(row: KbEvalCase) -> EvalCaseOut:
    return EvalCaseOut(
        id=row.id,
        question=row.question,
        expected=_expected(row.expected),
        note=row.note,
        created_at=row.created_at,
    )


def _settings(raw: dict[str, Any] | None) -> RetrievalSettings:
    try:
        return RetrievalSettings.model_validate(raw or {})
    except ValidationError:
        return RetrievalSettings()


def _run_out(row: KbEvalRun) -> EvalRunOut:
    return EvalRunOut(
        id=row.id,
        settings=_settings(row.settings),
        metrics=EvalMetrics.model_validate(row.metrics),
        cases=[EvalCaseResult.model_validate(item) for item in row.cases or []],
        duration_ms=row.duration_ms,
        created_at=row.created_at,
    )


class KnowledgeEvalService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        search: KnowledgeSearchService,
        *,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._db = db
        self._settings = settings
        self._search = search
        self._clock = clock
        self._monotonic = monotonic

    async def list_cases(self, workspace_id: uuid.UUID, kb_id: uuid.UUID) -> EvalCaseList:
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            rows = await KbEvalCaseRepository(session, workspace_id).list_for_kb(kb.id)
        return EvalCaseList(items=[_case_out(row) for row in rows], total=len(rows))

    async def create_case(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, body: EvalCaseCreate
    ) -> EvalCaseOut:
        expected = body.expected
        if not expected.document_ids and not expected.segment_ids:
            raise ServiceError(422, "eval_expected_required", "정답 문서나 청크를 하나 이상 고른다")
        async with self._db.write() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            await self._check_targets(session, workspace_id, kb.id, expected)
            cases = KbEvalCaseRepository(session, workspace_id)
            if await cases.count(kb.id) >= MAX_EVAL_CASES:
                raise _cases_full()
            row = cases.add(
                KbEvalCase(
                    id=new_id(),
                    kb_id=kb.id,
                    question=body.question,
                    expected=expected.model_dump(mode="json"),
                    note=body.note,
                    created_at=self._clock(),
                )
            )
            await cases.flush()
            return _case_out(row)

    async def _check_targets(
        self, session: Any, workspace_id: uuid.UUID, kb_id: uuid.UUID, expected: EvalExpected
    ) -> None:
        documents = await KbDocumentRepository(session, workspace_id).get_many_live(
            kb_id, expected.document_ids
        )
        if len(documents) != len(set(expected.document_ids)):
            raise not_found("문서")
        segments = KbSegmentRepository(session, workspace_id)
        for segment_id in set(expected.segment_ids):
            segment = await segments.get(segment_id)
            if segment is None or segment.kb_id != kb_id:
                raise not_found("청크")

    async def delete_case(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, case_id: uuid.UUID
    ) -> None:
        async with self._db.write() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            cases = KbEvalCaseRepository(session, workspace_id)
            if await cases.get_in_kb(kb.id, case_id) is None:
                raise not_found("평가 질문")
            await cases.delete(case_id)

    async def import_cases(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, text: str, *, replace: bool = False
    ) -> EvalImportResult:
        """CSV(question, expected_document_name | expected_segment_id, note)를 넣는다.

        문서 이름은 이 KB의 살아 있는 문서와 맞춘다. 같은 이름이 여럿이면 모두 정답이다. 고칠 수
        없는 줄은 넣지 않고 problems에 줄 번호와 함께 남긴다. replace인데 넣을 질문이 하나도 없으면
        지금 평가셋을 지우지 않고 422 eval_import_empty다.
        """
        if len(text.encode("utf-8")) > MAX_CSV_BYTES:
            raise ServiceError(
                413,
                "csv_too_large",
                f"평가셋 CSV는 {MAX_CSV_BYTES // 2**20}MB 이하여야 한다",
                details={"max_bytes": MAX_CSV_BYTES},
            )
        parsed, csv_problems = parse_csv(text)
        problems = [
            EvalImportProblem(line=item.line, message=item.message) for item in csv_problems
        ]
        async with self._db.write() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            documents = await KbDocumentRepository(session, workspace_id).all_live(kb.id)
            by_name: dict[str, list[uuid.UUID]] = {}
            for document in documents:
                by_name.setdefault(document.name, []).append(document.id)
            segments = KbSegmentRepository(session, workspace_id)
            valid: list[tuple[CsvCase, EvalExpected]] = []
            for item in parsed:
                expected = EvalExpected()
                if item.document_name is not None:
                    found = by_name.get(item.document_name)
                    if not found:
                        problems.append(
                            EvalImportProblem(
                                line=item.line, message=f"문서가 없다: {item.document_name[:80]}"
                            )
                        )
                        continue
                    expected.document_ids = found
                if item.segment_id is not None:
                    segment = await segments.get(item.segment_id)
                    if segment is None or segment.kb_id != kb.id:
                        problems.append(
                            EvalImportProblem(line=item.line, message="이 KB의 청크가 아니다")
                        )
                        continue
                    expected.segment_ids = [segment.id]
                valid.append((item, expected))
            problems.sort(key=lambda problem: problem.line)
            if replace and not valid:
                raise ServiceError(
                    422,
                    "eval_import_empty",
                    "넣을 수 있는 질문이 없어 지금 평가셋을 그대로 두었다",
                    details={"problems": [problem.model_dump() for problem in problems]},
                )
            cases = KbEvalCaseRepository(session, workspace_id)
            if replace:
                await cases.delete_all(kb.id)
            room = MAX_EVAL_CASES - await cases.count(kb.id)
            now = self._clock()
            for item, expected in valid[:room]:
                cases.add(
                    KbEvalCase(
                        id=new_id(),
                        kb_id=kb.id,
                        question=item.question,
                        expected=expected.model_dump(mode="json"),
                        note=item.note,
                        created_at=now,
                    )
                )
            for item, _ in valid[room:]:
                problems.append(
                    EvalImportProblem(line=item.line, message=f"평가셋은 {MAX_EVAL_CASES}개까지다")
                )
            await cases.flush()
            total = await cases.count(kb.id)
        created = min(len(valid), max(room, 0))
        log.info(
            "knowledge.eval_imported",
            kb_id=str(kb_id),
            created=created,
            problems=len(problems),
            replace=replace,
        )
        return EvalImportResult(created=created, problems=problems, total=total)

    async def run(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, body: EvalRunRequest
    ) -> EvalRunOut:
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            rows = await KbEvalCaseRepository(session, workspace_id).list_for_kb(kb.id)
            stored = parse_retrieval(kb.retrieval_model)
        if not rows:
            raise ServiceError(422, "eval_cases_empty", "평가셋이 비었다. 질문을 먼저 넣는다")
        settings = body.retrieval_model or stored.model_copy(
            update={"top_k": min(stored.top_k, self._settings.kb_top_k_max)}
        )
        depth = min(max(settings.top_k, EVAL_DEPTH), self._settings.kb_top_k_max)
        searched = settings.model_copy(update={"top_k": depth})
        started = self._monotonic()
        ranks: list[int | None] = []
        results: list[EvalCaseResult] = []
        warnings: dict[str, RetrievalWarning] = {}
        for row in rows:
            expected = _expected(row.expected)
            found = await self._search.retrieve(
                workspace_id, [kb.id], row.question, searched, source="eval"
            )
            for warning in found.warnings:
                warnings.setdefault(warning.code, warning)
            pairs = [(record.segment.id, record.document.id) for record in found.records]
            rank = first_hit(
                pairs,
                document_ids=set(expected.document_ids),
                segment_ids=set(expected.segment_ids),
            )
            ranks.append(rank)
            results.append(
                EvalCaseResult(
                    case_id=row.id,
                    question=row.question,
                    rank=rank,
                    top=[
                        EvalHit(
                            segment_id=record.segment.id,
                            document_id=record.document.id,
                            score=record.score,
                        )
                        for record in found.records[:TOP_HITS]
                    ],
                )
            )
        computed = metrics(ranks)
        out = EvalRunOut(
            id=None,
            settings=settings,
            metrics=EvalMetrics(
                r1=computed.r1,
                r3=computed.r3,
                r5=computed.r5,
                mrr10=computed.mrr10,
                cases=computed.cases,
            ),
            cases=results,
            duration_ms=int((self._monotonic() - started) * 1000),
            warnings=list(warnings.values()),
            created_at=self._clock(),
        )
        if body.save:
            out = await self._save(workspace_id, kb.id, out)
        log.info(
            "knowledge.eval_run",
            kb_id=str(kb_id),
            cases=computed.cases,
            r1=computed.r1,
            mrr10=computed.mrr10,
            saved=body.save,
        )
        return out

    async def _save(self, workspace_id: uuid.UUID, kb_id: uuid.UUID, out: EvalRunOut) -> EvalRunOut:
        async with self._db.write() as session:
            await active_kb(session, workspace_id, kb_id)
            runs = KbEvalRunRepository(session, workspace_id)
            row = runs.add(
                KbEvalRun(
                    id=new_id(),
                    kb_id=kb_id,
                    settings=out.settings.model_dump(mode="json"),
                    metrics=out.metrics.model_dump(mode="json"),
                    cases=[item.model_dump(mode="json") for item in out.cases],
                    duration_ms=out.duration_ms,
                    created_at=out.created_at,
                )
            )
            await runs.flush()
            await runs.prune(kb_id, keep=KEEP_RUNS)
        return out.model_copy(update={"id": row.id})

    async def list_runs(self, workspace_id: uuid.UUID, kb_id: uuid.UUID) -> EvalRunList:
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            rows = await KbEvalRunRepository(session, workspace_id).list_for_kb(
                kb.id, limit=KEEP_RUNS
            )
        return EvalRunList(
            items=[
                EvalRunSummary(
                    id=row.id,
                    settings=_settings(row.settings),
                    metrics=EvalMetrics.model_validate(row.metrics),
                    duration_ms=row.duration_ms,
                    created_at=row.created_at,
                )
                for row in rows
            ]
        )

    async def get_run(
        self, workspace_id: uuid.UUID, kb_id: uuid.UUID, run_id: uuid.UUID
    ) -> EvalRunOut:
        async with self._db.read() as session:
            kb = await active_kb(session, workspace_id, kb_id)
            row = await KbEvalRunRepository(session, workspace_id).get_in_kb(kb.id, run_id)
        if row is None:
            raise not_found("평가 실행")
        return _run_out(row)


def _cases_full() -> ServiceError:
    return ServiceError(
        422,
        "eval_cases_full",
        f"평가셋은 {MAX_EVAL_CASES}개까지다",
        details={"max": MAX_EVAL_CASES},
    )
