"""청크로 묶기와 여러 KB 병합(3단계 설계 7.1의 6단계, 7.6)."""

import uuid

import pytest

from app.knowledge.retrieval.fusion import Scored
from app.knowledge.retrieval.service import (
    KbHits,
    KbRanking,
    RankedSegment,
    ScoreKind,
    SearchChannel,
    UnitRef,
    apply_relevance,
    candidate_depth,
    group,
    kb_keep,
    merge,
    rank_kb,
)
from app.knowledge.units import FtsHit, Hit
from app.schemas.knowledge import RerankingMode, RerankSettings, RetrievalSettings

KB_A = uuid.UUID(int=0xA)
KB_B = uuid.UUID(int=0xB)
DOC = uuid.UUID(int=0xD)


def seg(n: int) -> uuid.UUID:
    return uuid.UUID(int=1000 + n)


def segment(kb_id: uuid.UUID, n: int, score: float, cosine: float | None = None) -> RankedSegment:
    return RankedSegment(
        kb_id=kb_id,
        segment_id=seg(n),
        document_id=DOC,
        score=score,
        best=Scored(unit_no=n, score=score, cosine=cosine),
    )


def ranking(
    kb_id: uuid.UUID, kind: ScoreKind, key: str | None, *items: tuple[int, float]
) -> KbRanking:
    return KbRanking(
        kb_id=kb_id,
        kind=kind,
        config_key=key,
        segments=[segment(kb_id, n, score) for n, score in items],
    )


def ids(segments: list[RankedSegment]) -> list[int]:
    return [item.best.unit_no for item in segments]


def test_same_embedding_and_weighted_scores_merge_by_score() -> None:
    merged, mixed = merge(
        [
            ranking(KB_A, "weighted", "m:768", (1, 0.9), (2, 0.4)),
            ranking(KB_B, "weighted", "m:768", (11, 0.7), (12, 0.6)),
        ],
        "weighted_score",
    )

    assert ids(merged) == [1, 11, 12, 2]
    assert not mixed
    assert merged[0].fusion_score is None


@pytest.mark.parametrize(
    ("first", "second", "mode"),
    [
        (("weighted", "m:768"), ("weighted", "m:1536"), "weighted_score"),
        (("weighted", "m:768"), ("keyword", None), "weighted_score"),
        (("cosine", "m:768"), ("keyword", None), "weighted_score"),
        (("rrf", "m:768"), ("rrf", "m:768"), "rrf"),
    ],
    ids=["other-embedding", "economy-mixed", "semantic-and-keyword", "rrf-mode"],
)
def test_other_lists_merge_by_rank_and_say_so(
    first: tuple[ScoreKind, str | None],
    second: tuple[ScoreKind, str | None],
    mode: RerankingMode,
) -> None:
    merged, mixed = merge(
        [
            ranking(KB_A, first[0], first[1], (1, 0.9), (2, 0.8)),
            ranking(KB_B, second[0], second[1], (11, 0.2), (12, 0.1)),
        ],
        mode,
    )

    assert mixed
    # 같은 순위끼리는 점수가 같아 unit_no가 작은 쪽이 앞선다.
    assert ids(merged) == [1, 11, 2, 12]
    assert merged[0].score == pytest.approx(1 / 61)
    assert merged[1].fusion_score == 0.2


def test_full_text_lists_merge_by_score_whatever_the_embedding() -> None:
    merged, mixed = merge(
        [
            ranking(KB_A, "keyword", None, (1, 1.0), (2, 0.3)),
            ranking(KB_B, "keyword", None, (11, 1.0), (12, 0.5)),
        ],
        "weighted_score",
    )

    assert not mixed
    assert ids(merged) == [1, 11, 12, 2]


def test_an_empty_list_does_not_force_rrf() -> None:
    merged, mixed = merge(
        [
            ranking(KB_A, "weighted", "m:768", (1, 0.9), (2, 0.4)),
            ranking(KB_B, "keyword", None),
        ],
        "weighted_score",
    )

    assert not mixed
    assert [item.score for item in merged] == [0.9, 0.4]


def test_parent_child_units_fold_into_their_parent() -> None:
    parent_a, parent_b = seg(1), seg(2)
    units = {
        10: UnitRef(10, parent_a, DOC, child_id=uuid.UUID(int=10)),
        11: UnitRef(11, parent_a, DOC, child_id=uuid.UUID(int=11)),
        12: UnitRef(12, parent_b, DOC, child_id=uuid.UUID(int=12)),
    }
    scored = [
        Scored(11, 0.9, cosine=0.8),
        Scored(12, 0.7, cosine=0.7),
        Scored(10, 0.5, cosine=0.6),
        # 표에 없는 유닛(고아, 끈 청크)은 버린다.
        Scored(99, 0.95, cosine=0.9),
    ]

    segments = group(KB_A, sorted(scored, key=lambda s: -s.score), units)

    assert [item.segment_id for item in segments] == [parent_a, parent_b]
    assert segments[0].score == 0.9
    assert [hit.child_id for hit in segments[0].children] == [uuid.UUID(int=11), uuid.UUID(int=10)]
    assert [hit.scored.score for hit in segments[0].children] == [0.9, 0.5]


def test_rank_kb_keeps_top_k_segments_and_more_for_llm_rerank() -> None:
    vector = [Hit(n, 1 - n / 100) for n in range(1, 31)]
    units = {n: UnitRef(n, seg(n), DOC) for n in range(1, 31)}
    channels: frozenset[SearchChannel] = frozenset({"vector", "keyword"})
    hits = KbHits(KB_A, channels, vector, [FtsHit(5, 1.0)], units, config_key="m:768")

    llm_settings = RetrievalSettings(
        top_k=4, reranking_mode="llm", rerank=RerankSettings(candidates=12)
    )

    plain = rank_kb(hits, RetrievalSettings(top_k=4))
    llm = rank_kb(hits, llm_settings)

    assert len(plain.segments) == 4 and plain.kind == "weighted"
    assert plain.config_key == "m:768"
    assert len(llm.segments) == 12
    # 돌려줄 결과가 모두 LLM 점수를 갖도록 top_k보다 적게 보내지 않는다.
    assert kb_keep(llm_settings.model_copy(update={"top_k": 15})) == 15


def test_economy_hits_rank_as_keyword_scores() -> None:
    channels: frozenset[SearchChannel] = frozenset({"keyword"})
    units = {1: UnitRef(1, seg(1), DOC), 2: UnitRef(2, seg(2), DOC)}
    hits = KbHits(KB_B, channels, [], [FtsHit(1, 2.0), FtsHit(2, 4.0)], units)

    ranked = rank_kb(hits, RetrievalSettings(search_method="semantic_search"))

    assert ranked.kind == "keyword" and ranked.config_key is None
    assert [item.score for item in ranked.segments] == [1.0, 0.5]


def test_candidate_depth_is_at_least_four_times_top_k() -> None:
    assert candidate_depth(4, 50) == 50
    assert candidate_depth(20, 50) == 80


def test_llm_relevance_reorders_and_keeps_the_fused_score() -> None:
    first, second, third = (segment(KB_A, n, score) for n, score in [(1, 0.9), (2, 0.8), (3, 0.7)])

    reordered = apply_relevance([first, second, third], [0.2, None, 0.9])

    assert ids(reordered) == [3, 1, 2]
    assert [item.score for item in reordered] == [0.9, 0.2, 0.0]
    assert [item.fusion_score for item in reordered] == [0.7, 0.9, 0.8]
