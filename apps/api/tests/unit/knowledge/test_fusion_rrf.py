"""가중 RRF(k=60, 3단계 설계 7.2). 기대 순위는 fusion_cases.json의 실측 값으로 따로 계산했다."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.knowledge.retrieval.fusion import RRF_K, rrf, weighted
from app.knowledge.units import FtsHit, Hit

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "knowledge" / "fusion_cases.json"
DATA: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))


def hits(case: dict[str, Any]) -> tuple[list[Hit], list[FtsHit]]:
    vector = [Hit(int(unit), float(cosine)) for unit, cosine in case["vector"]]
    keyword = [FtsHit(int(unit), float(score)) for unit, score in case["keyword"]]
    return vector, keyword


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda case: str(case["question"]))
def test_rrf_matches_the_hand_computed_order(case: dict[str, Any]) -> None:
    vector, keyword = hits(case)

    ranked = [item.unit_no for item in rrf(vector, keyword, 1.0, 1.0)]

    assert ranked[:5] == case["expected_top"]["rrf_1_1"]


def test_rrf_pulls_down_a_confident_vector_answer() -> None:
    # 전문 후보에 정답이 없으면 RRF는 벡터 1위를 4위로 내리고, min-max 가중합은 1위로 둔다(3.5절).
    case = next(item for item in DATA["cases"] if item["question"].startswith("아파서"))
    vector, keyword = hits(case)

    assert [item.unit_no for item in rrf(vector, keyword, 1.0, 1.0)].index(2) == 3
    assert weighted(vector, keyword, 0.7)[0].unit_no == 2


def test_rrf_is_below_the_default_over_the_whole_set() -> None:
    first = 0
    for row in DATA["all"]:
        vector, keyword = hits(row)
        first += rrf(vector, keyword, 1.0, 1.0)[0].unit_no == row["expected"]

    assert round(first / len(DATA["all"]), 4) == DATA["r1"]["rrf_1_1"]
    assert DATA["r1"]["rrf_1_1"] < DATA["r1"]["minmax_0.7"]


def test_rrf_adds_weighted_reciprocal_ranks() -> None:
    fused = {
        item.unit_no: item
        for item in rrf(
            [Hit(1, 0.9), Hit(2, 0.8)], [FtsHit(2, 5.0), FtsHit(3, 1.0)], 0.7, 0.3, k=RRF_K
        )
    }

    assert fused[1].score == pytest.approx(0.7 / 61)
    assert fused[2].score == pytest.approx(0.7 / 62 + 0.3 / 61)
    assert fused[3].score == pytest.approx(0.3 / 62)
    assert (fused[2].vector_rank, fused[2].keyword_rank) == (2, 1)


def test_crossed_ranks_tie_and_the_raw_cosine_breaks_it() -> None:
    # 3.5절의 ERR-4417 실패: 두 채널 순위가 엇갈려 점수가 같았고 넣은 순서로 오답이 앞섰다.
    # 여기서는 코사인이 높은 쪽(1)이 앞선다. 입력 순서를 바꿔도 같다.
    vector = [Hit(1, 0.74), Hit(2, 0.73)]
    keyword = [FtsHit(2, 3.0), FtsHit(1, 2.0)]

    forward = rrf(vector, keyword, 1.0, 1.0)
    backward = rrf(list(reversed(vector)), list(reversed(keyword)), 1.0, 1.0)

    assert forward[0].score == forward[1].score
    assert [item.unit_no for item in forward] == [item.unit_no for item in backward] == [1, 2]
