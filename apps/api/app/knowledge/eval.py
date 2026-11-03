"""검색 평가(3단계 설계 7.7): 평가셋 CSV 읽기와 R@k·MRR@10.

정답이 문서 단위면 그 문서의 어느 청크든 맞은 것으로 본다. 순위는 1부터이고 상위 10개 안에 없으면
None이다.
"""

import csv
import io
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from uuid import UUID

from app.schemas.retrieval import MAX_EVAL_QUESTION_CHARS

EVAL_DEPTH = 10
# 평가 실행 하나가 요청 안에서 끝나야 해서(질문마다 질의 임베딩 한 번) 평가셋 크기를 묶는다.
MAX_EVAL_CASES = 300
MAX_CSV_BYTES = 1024 * 1024

QUESTION = "question"
DOCUMENT_NAME = "expected_document_name"
SEGMENT_ID = "expected_segment_id"
NOTE = "note"


@dataclass(frozen=True, slots=True)
class Metrics:
    r1: float
    r3: float
    r5: float
    mrr10: float
    cases: int


@dataclass(frozen=True, slots=True)
class CsvCase:
    # 머리글이 1번 줄이다.
    line: int
    question: str
    document_name: str | None
    segment_id: UUID | None
    note: str | None


@dataclass(frozen=True, slots=True)
class CsvProblem:
    line: int
    message: str


def first_hit(
    results: Sequence[tuple[UUID, UUID]],
    *,
    document_ids: Collection[UUID],
    segment_ids: Collection[UUID],
    depth: int = EVAL_DEPTH,
) -> int | None:
    """results는 순위대로의 (segment_id, document_id). 정답이 처음 나온 순위."""
    for rank, (segment_id, document_id) in enumerate(results[:depth], start=1):
        if segment_id in segment_ids or document_id in document_ids:
            return rank
    return None


def _recall(ranks: Sequence[int | None], k: int) -> float:
    return sum(1 for rank in ranks if rank is not None and rank <= k) / len(ranks)


def metrics(ranks: Sequence[int | None]) -> Metrics:
    if not ranks:
        return Metrics(r1=0.0, r3=0.0, r5=0.0, mrr10=0.0, cases=0)
    mrr = sum(1 / rank for rank in ranks if rank is not None and rank <= EVAL_DEPTH) / len(ranks)
    return Metrics(
        r1=round(_recall(ranks, 1), 4),
        r3=round(_recall(ranks, 3), 4),
        r5=round(_recall(ranks, 5), 4),
        mrr10=round(mrr, 4),
        cases=len(ranks),
    )


def parse_csv(text: str) -> tuple[list[CsvCase], list[CsvProblem]]:
    """question 칸과 expected_document_name·expected_segment_id 중 하나 이상이 있는 CSV."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿"), newline=""))
    header = [name.strip() for name in (reader.fieldnames or [])]
    reader.fieldnames = header
    if QUESTION not in header or not ({DOCUMENT_NAME, SEGMENT_ID} & set(header)):
        return [], [
            CsvProblem(
                1,
                f"머리글에 {QUESTION}와 {DOCUMENT_NAME} 또는 {SEGMENT_ID} 칸이 있어야 한다",
            )
        ]
    cases: list[CsvCase] = []
    problems: list[CsvProblem] = []
    for row in reader:
        line = reader.line_num
        question = (row.get(QUESTION) or "").strip()
        name = (row.get(DOCUMENT_NAME) or "").strip() or None
        raw_segment = (row.get(SEGMENT_ID) or "").strip() or None
        note = (row.get(NOTE) or "").strip() or None
        if not question and name is None and raw_segment is None:
            continue
        if not question:
            problems.append(CsvProblem(line, "질문이 비었다"))
            continue
        if len(question) > MAX_EVAL_QUESTION_CHARS:
            problems.append(CsvProblem(line, f"질문은 {MAX_EVAL_QUESTION_CHARS}자 이하여야 한다"))
            continue
        segment_id: UUID | None = None
        if raw_segment is not None:
            try:
                segment_id = UUID(raw_segment)
            except ValueError:
                problems.append(CsvProblem(line, f"청크 id가 아니다: {raw_segment[:80]}"))
                continue
        if name is None and segment_id is None:
            problems.append(CsvProblem(line, "정답 문서 이름이나 청크 id가 없다"))
            continue
        cases.append(CsvCase(line, question, name, segment_id, note))
    return cases, problems
