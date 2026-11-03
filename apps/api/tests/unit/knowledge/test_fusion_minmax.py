"""후보 안 min-max 점수 가중합과 코사인 원값 순위(3단계 설계 7.2).

fusion_cases.json은 실측 벡터(emb2p, 768)와 bigram bm25 값이고, 기대 순위는 생성 스크립트가 검사
대상과 따로 계산했다.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.knowledge.retrieval.fusion import (
    keyword_only,
    keyword_scores,
    minmax,
    vector_only,
    weighted,
)
from app.knowledge.units import FtsHit, Hit

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "knowledge" / "fusion_cases.json"
DATA: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))


def hits(case: dict[str, Any]) -> tuple[list[Hit], list[FtsHit]]:
    vector = [Hit(int(unit), float(cosine)) for unit, cosine in case["vector"]]
    keyword = [FtsHit(int(unit), float(score)) for unit, score in case["keyword"]]
    return vector, keyword


def case_id(case: dict[str, Any]) -> str:
    return str(case["question"])


@pytest.mark.parametrize("weight", [0.7, 0.8])
@pytest.mark.parametrize("case", DATA["cases"], ids=case_id)
def test_weighted_matches_the_hand_computed_order(case: dict[str, Any], weight: float) -> None:
    vector, keyword = hits(case)

    ranked = [item.unit_no for item in weighted(vector, keyword, weight)]

    assert ranked[:5] == case["expected_top"][f"minmax_{weight}"]


@pytest.mark.parametrize("case", DATA["cases"], ids=case_id)
def test_vector_only_ranks_by_raw_cosine(case: dict[str, Any]) -> None:
    vector, _ = hits(case)

    ranked = vector_only(vector)

    assert [item.unit_no for item in ranked[:5]] == case["expected_top"]["vector"]
    assert ranked[0].score == ranked[0].cosine == case["vector"][0][1]


@pytest.mark.parametrize("case", DATA["cases"], ids=case_id)
def test_keyword_only_ranks_by_bm25(case: dict[str, Any]) -> None:
    _, keyword = hits(case)

    ranked = keyword_only(keyword)

    assert [item.unit_no for item in ranked[:5]] == case["expected_top"]["keyword"]
    assert ranked[0].score == 1.0


def test_the_default_puts_the_answer_first_for_every_question() -> None:
    # 실측 벡터 재계산의 K3 수치: 벡터만 0.981(ERR-4417을 놓친다), min-max a=0.7은 1.000.
    first = {"weighted": 0, "vector": 0, "keyword": 0}
    for row in DATA["all"]:
        vector, keyword = hits(row)
        first["weighted"] += weighted(vector, keyword, 0.7)[0].unit_no == row["expected"]
        first["vector"] += vector_only(vector)[0].unit_no == row["expected"]
        top = keyword_only(keyword)
        first["keyword"] += bool(top) and top[0].unit_no == row["expected"]

    total = len(DATA["all"])
    assert total == 53
    assert round(first["weighted"] / total, 4) == DATA["r1"]["minmax_0.7"] == 1.0
    assert round(first["vector"] / total, 4) == DATA["r1"]["vector"]
    assert round(first["keyword"] / total, 4) == DATA["r1"]["keyword"]


def test_a_keyword_hit_lifts_the_near_tie_in_err_4417() -> None:
    case = next(item for item in DATA["cases"] if item["question"] == "ERR-4417")
    vector, keyword = hits(case)

    fused = weighted(vector, keyword, 0.7)

    assert vector_only(vector)[0].unit_no == 42
    assert fused[0].unit_no == case["expected_unit"] == 39
    assert fused[0].keyword_score == 1.0
    assert fused[0].vector_rank == 2 and fused[0].keyword_rank == 1


def test_minmax_maps_the_candidates_onto_zero_to_one() -> None:
    scores = minmax([Hit(1, 0.9), Hit(2, 0.7), Hit(3, 0.8)])

    assert scores[1] == 1.0 and scores[2] == 0.0
    assert scores[3] == pytest.approx(0.5)
    assert minmax([Hit(4, 0.42)]) == {4: 1.0}
    assert minmax([Hit(5, 0.5), Hit(6, 0.5)]) == {5: 1.0, 6: 1.0}
    assert minmax([]) == {}


def test_keyword_scores_divide_by_the_best_bm25() -> None:
    assert keyword_scores([FtsHit(1, 8.0), FtsHit(2, 2.0)]) == {1: 1.0, 2: 0.25}


def test_a_candidate_on_one_side_only_gets_that_side() -> None:
    fused = {
        item.unit_no: item
        for item in weighted(
            [Hit(1, 0.9), Hit(2, 0.5)], [FtsHit(2, 4.0), FtsHit(3, 2.0)], vector_weight=0.7
        )
    }

    assert fused[1].score == pytest.approx(0.7)
    assert fused[2].score == pytest.approx(0.3)
    assert fused[3].score == pytest.approx(0.15)
    assert fused[3].cosine is None and fused[3].vector_score is None
    assert fused[1].keyword_score is None


def test_ties_go_to_the_higher_raw_cosine_then_the_smaller_unit() -> None:
    # 셋 다 결합 점수가 0.5다. 코사인이 높은 7, 코사인이 없는 둘 중 unit_no가 작은 3, 그다음 9.
    vector = [Hit(7, 0.8), Hit(8, 0.4)]
    keyword = [FtsHit(9, 1.0), FtsHit(3, 1.0)]

    fused = weighted(vector, keyword, vector_weight=0.5)

    assert [item.unit_no for item in fused] == [7, 3, 9, 8]
    assert fused[0].score == fused[1].score == fused[2].score == 0.5
