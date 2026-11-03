"""평가 지표와 평가셋 CSV(3단계 설계 7.7)."""

import uuid
from pathlib import Path

from app.knowledge.eval import first_hit, metrics, parse_csv

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "knowledge"
DOC_A, DOC_B = uuid.UUID(int=1), uuid.UUID(int=2)
SEG = [uuid.UUID(int=100 + n) for n in range(12)]


def test_a_document_answer_matches_any_of_its_segments() -> None:
    results = [(SEG[0], DOC_B), (SEG[1], DOC_A), (SEG[2], DOC_A)]

    assert first_hit(results, document_ids={DOC_A}, segment_ids=set()) == 2
    assert first_hit(results, document_ids=set(), segment_ids={SEG[2]}) == 3
    assert first_hit(results, document_ids={uuid.UUID(int=9)}, segment_ids=set()) is None


def test_hits_below_the_tenth_place_do_not_count() -> None:
    results = [(SEG[n], DOC_B) for n in range(11)] + [(SEG[11], DOC_A)]

    assert first_hit(results, document_ids={DOC_A}, segment_ids=set()) is None


def test_recall_and_mrr() -> None:
    computed = metrics([1, 2, 4, None])

    assert computed.cases == 4
    assert (computed.r1, computed.r3, computed.r5) == (0.25, 0.5, 0.75)
    assert computed.mrr10 == round((1 + 1 / 2 + 1 / 4) / 4, 4)
    assert metrics([]).cases == 0


def test_the_synthetic_set_parses_without_problems() -> None:
    cases, problems = parse_csv((FIXTURES / "ko_eval.csv").read_text(encoding="utf-8"))

    assert problems == []
    assert len(cases) == 53
    assert cases[0].question == "입사 첫해에 연차가 며칠이야?"
    assert (cases[0].document_name, cases[0].note, cases[0].line) == ("A1", "sem", 2)


def test_csv_rows_with_problems_are_reported_by_line() -> None:
    text = (
        "﻿question,expected_document_name,expected_segment_id,note\n"
        "연차는?,규정,,\n"
        ",규정,,\n"
        "병가는?,,not-a-uuid,\n"
        "출장비는?,,,\n"
        ",,,\n"
        f"보안은?,,{SEG[0]},메모\n"
    )

    cases, problems = parse_csv(text)

    assert [(case.line, case.question) for case in cases] == [(2, "연차는?"), (7, "보안은?")]
    assert cases[1].segment_id == SEG[0] and cases[1].note == "메모"
    assert [problem.line for problem in problems] == [3, 4, 5]


def test_a_csv_without_the_needed_columns_is_refused() -> None:
    cases, problems = parse_csv("q,answer\n연차는?,A1\n")

    assert cases == []
    assert problems[0].line == 1
