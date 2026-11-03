"""검색 결과의 결합·묶기·병합(3단계 설계 7.1의 3~5단계). DB와 index.db는 모른다.

app/services/knowledge_search.py가 KB마다 후보(벡터·전문)와 살아 있는 유닛 표를 모아 넘기면, 여기서
문턱 → 결합 → 청크로 묶기 → KB 안 top_k → KB 사이 병합까지 한다. 정렬은 모두 결정적이다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal
from uuid import UUID

from app.knowledge.retrieval.fusion import (
    RRF_K,
    Scored,
    above_threshold,
    keyword_only,
    rrf,
    tie_key,
    vector_only,
    weighted,
)
from app.knowledge.units import FtsHit, Hit
from app.schemas.knowledge import RerankingMode, RetrievalSettings

SearchChannel = Literal["vector", "keyword"]
# 점수의 종류. 종류와 임베딩 설정이 같은 KB끼리만 점수를 그대로 섞는다(7.6).
ScoreKind = Literal["cosine", "keyword", "weighted", "rrf"]
# LLM 재순위에 한 번에 보내는 후보 상한(7.4).
MAX_RERANK_POOL = 20


@dataclass(frozen=True, slots=True)
class UnitRef:
    """index.db 유닛 하나가 가리키는 app.db 행. 검색할 수 없는 유닛(끈 청크, 지운·보관 문서)은
    표에 넣지 않는다."""

    unit_no: int
    # 돌려주는 청크. Parent-child면 부모다.
    segment_id: UUID
    document_id: UUID
    # Parent-child의 자식 청크.
    child_id: UUID | None = None


@dataclass(frozen=True)
class KbHits:
    kb_id: UUID
    # 실제로 돌린 채널. economy KB나 질의 임베딩이 실패한 하이브리드는 keyword만이다.
    channels: frozenset[SearchChannel]
    vector: Sequence[Hit]
    keyword: Sequence[FtsHit]
    units: Mapping[int, UnitRef]
    config_key: str | None = None


@dataclass(frozen=True, slots=True)
class ChildHit:
    child_id: UUID
    scored: Scored


@dataclass(frozen=True)
class RankedSegment:
    kb_id: UUID
    segment_id: UUID
    document_id: UUID
    score: float
    # 점수를 정한 유닛(Parent-child면 자식 중 가장 높은 것).
    best: Scored
    children: tuple[ChildHit, ...] = ()
    # KB 사이 RRF 병합이나 LLM 재순위로 score가 바뀌면 원래 결합 점수를 남긴다.
    fusion_score: float | None = None

    def rescored(self, score: float) -> "RankedSegment":
        original = self.fusion_score if self.fusion_score is not None else self.score
        return replace(self, score=score, fusion_score=original)


@dataclass(frozen=True)
class KbRanking:
    kb_id: UUID
    kind: ScoreKind
    config_key: str | None
    segments: list[RankedSegment] = field(default_factory=list[RankedSegment])


def segment_key(segment: RankedSegment) -> tuple[float, float, int]:
    return tie_key(segment.score, segment.best.cosine, segment.best.unit_no)


def candidate_depth(top_k: int, candidates: int) -> int:
    # 후보 깊이는 top_k보다 커야 결합이 의미 있다. top_k × 4와 설정값 중 큰 값(7.3).
    return max(candidates, top_k * 4)


def rerank_pool(settings: RetrievalSettings) -> int:
    # 돌려주는 결과가 모두 LLM 관련도를 갖도록 top_k보다 적게 보내지 않는다.
    return min(MAX_RERANK_POOL, max(settings.rerank.candidates, settings.top_k))


def kb_keep(settings: RetrievalSettings) -> int:
    """KB 안에서 남길 청크 수. LLM 재순위면 재순위 후보만큼 남긴다."""
    if settings.reranking_mode == "llm":
        return rerank_pool(settings)
    return settings.top_k


def score_kind(channels: frozenset[SearchChannel], mode: RerankingMode) -> ScoreKind:
    if channels == frozenset({"vector"}):
        return "cosine"
    if "vector" not in channels:
        return "keyword"
    return "rrf" if mode == "rrf" else "weighted"


def fuse(hits: KbHits, settings: RetrievalSettings) -> list[Scored]:
    """문턱 → 결합. 문턱은 결합 전에 벡터 후보의 코사인 원값에만 걸고 min-max는 남은 후보로
    한다(7.3). 결합 점수에 걸면 전문 검색으로만 잡힌 후보가 다 잘린다."""
    vector: list[Hit] = list(hits.vector) if "vector" in hits.channels else []
    if vector and settings.score_threshold_enabled:
        vector = above_threshold(vector, settings.score_threshold)
    keyword: list[FtsHit] = list(hits.keyword) if "keyword" in hits.channels else []
    weights = settings.weights
    match score_kind(hits.channels, settings.reranking_mode):
        case "cosine":
            return vector_only(vector)
        case "keyword":
            return keyword_only(keyword)
        case "rrf":
            return rrf(vector, keyword, weights.vector_weight, weights.keyword_weight)
        case "weighted":
            return weighted(vector, keyword, weights.vector_weight)


def group(
    kb_id: UUID, scored: Sequence[Scored], units: Mapping[int, UnitRef]
) -> list[RankedSegment]:
    """유닛을 돌려줄 청크로 묶는다. 청크 점수는 유닛 최대값이고 자식은 점수순으로 붙는다.

    scored가 정렬돼 있으므로 청크마다 처음 만난 유닛이 대표다. 표에 없는 유닛(고아, 끈 청크)은
    버린다(5.4).
    """
    order: list[UUID] = []
    best: dict[UUID, tuple[UnitRef, Scored]] = {}
    children: dict[UUID, list[ChildHit]] = {}
    for item in scored:
        ref = units.get(item.unit_no)
        if ref is None:
            continue
        if ref.segment_id not in best:
            order.append(ref.segment_id)
            best[ref.segment_id] = (ref, item)
            children[ref.segment_id] = []
        if ref.child_id is not None:
            children[ref.segment_id].append(ChildHit(ref.child_id, item))
    segments: list[RankedSegment] = []
    for segment_id in order:
        ref, item = best[segment_id]
        segments.append(
            RankedSegment(
                kb_id=kb_id,
                segment_id=segment_id,
                document_id=ref.document_id,
                score=item.score,
                best=item,
                children=tuple(children[segment_id]),
            )
        )
    return segments


def rank_kb(hits: KbHits, settings: RetrievalSettings) -> KbRanking:
    segments = group(hits.kb_id, fuse(hits, settings), hits.units)[: kb_keep(settings)]
    return KbRanking(
        kb_id=hits.kb_id,
        kind=score_kind(hits.channels, settings.reranking_mode),
        config_key=hits.config_key if "vector" in hits.channels else None,
        segments=segments,
    )


def comparable(rankings: Sequence[KbRanking], mode: RerankingMode) -> bool:
    """점수를 그대로 섞어도 되는지(7.6). 같은 질의 벡터에서 나온 같은 종류의 점수여야 한다.

    전문 검색만 쓴 목록끼리는 임베딩 설정과 상관없이 섞는다(벡터가 없다).
    """
    if mode == "rrf":
        return False
    kinds = {ranking.kind for ranking in rankings}
    if len(kinds) != 1:
        return False
    if kinds == {"keyword"}:
        return True
    keys = {ranking.config_key for ranking in rankings}
    return len(keys) == 1 and None not in keys


def merge(rankings: Sequence[KbRanking], mode: RerankingMode) -> tuple[list[RankedSegment], bool]:
    """KB 목록을 하나로. 점수를 섞을 수 없으면 KB 목록 사이를 RRF(k=60, 가중치 1)로 섞고 두 번째
    값이 참이다(경고 mixed_kb_rrf)."""
    filled = [ranking for ranking in rankings if ranking.segments]
    if len(filled) <= 1:
        return (list(filled[0].segments) if filled else []), False
    if comparable(filled, mode):
        merged = [segment for ranking in filled for segment in ranking.segments]
        return sorted(merged, key=segment_key), False
    rescored = [
        segment.rescored(1 / (RRF_K + rank))
        for ranking in filled
        for rank, segment in enumerate(ranking.segments, start=1)
    ]
    return sorted(rescored, key=segment_key), True


def apply_relevance(
    segments: Sequence[RankedSegment], relevance: Sequence[float | None]
) -> list[RankedSegment]:
    """LLM 관련도로 다시 줄 세운다(7.4). 관련도가 없는 후보는 0으로 보고, 같으면 원래 순서다."""
    scored = [
        (segment.rescored(value if value is not None else 0.0), index)
        for index, (segment, value) in enumerate(zip(segments, relevance, strict=True))
    ]
    scored.sort(key=lambda pair: (-pair[0].score, pair[1]))
    return [segment for segment, _ in scored]
