"""점수 문턱(3단계 설계 7.3). 코사인 원값으로 벡터 후보만 거르고 min-max는 남은 후보로 한다."""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.knowledge.retrieval.service import KbHits, SearchChannel, UnitRef, fuse
from app.knowledge.units import FtsHit, Hit
from app.schemas.knowledge import RetrievalSettings

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "knowledge" / "fusion_cases.json"
DATA: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
KB = uuid.UUID(int=1)


def kb_hits(
    vector: list[Hit],
    keyword: list[FtsHit],
    channels: tuple[SearchChannel, ...] = ("vector", "keyword"),
) -> KbHits:
    units = {
        hit.unit_no: UnitRef(hit.unit_no, uuid.UUID(int=hit.unit_no), uuid.UUID(int=hit.unit_no))
        for hit in [*vector, *keyword]
    }
    return KbHits(KB, frozenset(channels), vector, keyword, units)


def settings(threshold: float | None, **extra: Any) -> RetrievalSettings:
    return RetrievalSettings(
        score_threshold_enabled=threshold is not None,
        score_threshold=threshold if threshold is not None else 0.5,
        **extra,
    )


def fixture_case(prefix: str) -> tuple[list[Hit], list[FtsHit]]:
    case = next(item for item in DATA["cases"] if item["question"].startswith(prefix))
    return (
        [Hit(int(unit), float(cosine)) for unit, cosine in case["vector"]],
        [FtsHit(int(unit), float(score)) for unit, score in case["keyword"]],
    )


@pytest.mark.parametrize("prefix", ["아파서", "4분기", "ERR-4417"])
def test_threshold_matches_the_hand_computed_order(prefix: str) -> None:
    case = next(item for item in DATA["cases"] if item["question"].startswith(prefix))
    vector, keyword = fixture_case(prefix)

    hybrid = fuse(kb_hits(vector, keyword), settings(0.65))
    semantic = fuse(kb_hits(vector, keyword, ("vector",)), settings(0.65))

    assert [item.unit_no for item in hybrid[:5]] == case["expected_top"][
        "minmax_0.7_threshold_0.65"
    ]
    assert [item.unit_no for item in semantic[:5]] == case["expected_top"]["vector_threshold_0.65"]
    assert all(item.cosine is not None and item.cosine >= 0.65 for item in semantic)


def test_minmax_runs_over_the_candidates_left_after_the_threshold() -> None:
    vector, keyword = fixture_case("ERR-4417")

    kept = {item.unit_no: item for item in fuse(kb_hits(vector, keyword), settings(0.65))}

    # 0.65를 넘는 벡터 후보는 42와 39 둘뿐이라 39의 min-max 점수가 0이 된다. 그래서 문턱을 켜면
    # 문턱이 없을 때(39가 1위) 와 달리 42가 앞선다.
    assert set(kept) == {42, 39}
    assert kept[42].vector_score == 1.0 and kept[39].vector_score == 0.0
    assert kept[39].keyword_score == 1.0
    assert kept[42].score > kept[39].score


def test_keyword_candidates_survive_a_threshold_that_drops_their_vector() -> None:
    # 결합 점수에 문턱을 걸면 전문 검색으로만 잡힌 "ERR-4417" 같은 질문이 사라진다(7.3).
    vector = [Hit(1, 0.8), Hit(2, 0.3)]
    keyword = [FtsHit(2, 5.0)]

    fused = {item.unit_no: item for item in fuse(kb_hits(vector, keyword), settings(0.5))}

    assert fused[2].cosine is None and fused[2].keyword_score == 1.0
    assert fused[2].score == pytest.approx(0.3)


def test_full_text_search_has_no_threshold() -> None:
    keyword = [FtsHit(1, 0.1), FtsHit(2, 9.0)]

    fused = fuse(kb_hits([], keyword, ("keyword",)), settings(0.99))

    assert [item.unit_no for item in fused] == [2, 1]


def test_a_threshold_above_every_cosine_leaves_only_keyword_hits() -> None:
    fused = fuse(kb_hits([Hit(1, 0.4)], [FtsHit(2, 1.0)]), settings(0.9))

    assert [item.unit_no for item in fused] == [2]
