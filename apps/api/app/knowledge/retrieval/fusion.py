"""벡터·전문 후보의 결합(3단계 설계 7.2, 7.3). 입출력이 목록뿐인 순수 함수다.

기본은 후보 안 min-max 점수 가중합이다. 코사인 원값을 그대로 더하면 상위 후보 사이의 폭(질문당
평균 0.19)이 작아 전문 점수가 순위를 끌고 가고, RRF는 벡터가 확신하는 결과를 전문 검색의 엉뚱한
1위가 끌어내린다(docs/research/rag-text2sql-2026-11.md 3.5).
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.knowledge.units import FtsHit, Hit

RRF_K = 60


@dataclass(frozen=True, slots=True)
class Scored:
    unit_no: int
    score: float
    cosine: float | None = None
    vector_score: float | None = None
    keyword_score: float | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None


def tie_key(score: float, cosine: float | None, unit_no: int) -> tuple[float, float, int]:
    # 동점은 코사인 원값이 높은 쪽, 그다음 unit_no가 작은 쪽(7.2). 코사인이 없으면 뒤로 간다.
    return (-score, -cosine if cosine is not None else math.inf, unit_no)


def sort_key(item: Scored) -> tuple[float, float, int]:
    return tie_key(item.score, item.cosine, item.unit_no)


def vector_order(hits: Iterable[Hit]) -> list[Hit]:
    """코사인 내림차순, 같으면 unit_no. 같은 유닛이 두 번 오면 앞의 것만 둔다."""
    unique: dict[int, Hit] = {}
    for hit in hits:
        unique.setdefault(hit.unit_no, hit)
    return sorted(unique.values(), key=lambda hit: (-hit.cosine, hit.unit_no))


def keyword_order(hits: Iterable[FtsHit]) -> list[FtsHit]:
    # FTS5의 ORDER BY bm25는 같은 점수의 순서를 정하지 않는다.
    unique: dict[int, FtsHit] = {}
    for hit in hits:
        unique.setdefault(hit.unit_no, hit)
    return sorted(unique.values(), key=lambda hit: (-hit.score, hit.unit_no))


def above_threshold(hits: Sequence[Hit], threshold: float | None) -> list[Hit]:
    """코사인 원값 문턱(7.3). 결합 전에 벡터 후보에만 건다."""
    if threshold is None:
        return list(hits)
    return [hit for hit in hits if hit.cosine >= threshold]


def minmax(hits: Sequence[Hit]) -> dict[int, float]:
    """후보 안에서 코사인을 0~1로 맞춘다. 후보가 하나거나 모두 같으면 1.0이다."""
    if not hits:
        return {}
    low = min(hit.cosine for hit in hits)
    high = max(hit.cosine for hit in hits)
    if high - low <= 0:
        return {hit.unit_no: 1.0 for hit in hits}
    return {hit.unit_no: (hit.cosine - low) / (high - low) for hit in hits}


def keyword_scores(hits: Sequence[FtsHit]) -> dict[int, float]:
    """bm25(부호를 바꾼 값)를 그 목록의 최대값으로 나눈다."""
    if not hits:
        return {}
    top = max(hit.score for hit in hits)
    if top <= 0:
        return {hit.unit_no: 1.0 for hit in hits}
    return {hit.unit_no: min(max(hit.score, 0.0) / top, 1.0) for hit in hits}


def _ranks(unit_nos: Iterable[int]) -> dict[int, int]:
    return {unit_no: rank for rank, unit_no in enumerate(unit_nos, start=1)}


def _union(vector: Sequence[Hit], keyword: Sequence[FtsHit]) -> list[int]:
    return list(dict.fromkeys([*(hit.unit_no for hit in vector), *(h.unit_no for h in keyword)]))


def weighted(
    vector: Sequence[Hit], keyword: Sequence[FtsHit], vector_weight: float
) -> list[Scored]:
    """후보 안 min-max 점수 가중합 a·s_v + (1-a)·s_k. 한쪽 목록에만 있으면 그쪽만 더한다."""
    vector = vector_order(vector)
    keyword = keyword_order(keyword)
    s_v = minmax(vector)
    s_k = keyword_scores(keyword)
    cosine = {hit.unit_no: hit.cosine for hit in vector}
    v_rank = _ranks(hit.unit_no for hit in vector)
    k_rank = _ranks(hit.unit_no for hit in keyword)
    items: list[Scored] = []
    for unit_no in _union(vector, keyword):
        sv = s_v.get(unit_no)
        sk = s_k.get(unit_no)
        score = vector_weight * (sv or 0.0) + (1 - vector_weight) * (sk or 0.0)
        items.append(
            Scored(
                unit_no=unit_no,
                score=score,
                cosine=cosine.get(unit_no),
                vector_score=sv,
                keyword_score=sk,
                vector_rank=v_rank.get(unit_no),
                keyword_rank=k_rank.get(unit_no),
            )
        )
    return sorted(items, key=sort_key)


def rrf(
    vector: Sequence[Hit],
    keyword: Sequence[FtsHit],
    vector_weight: float,
    keyword_weight: float,
    *,
    k: int = RRF_K,
) -> list[Scored]:
    """가중 RRF w_v/(k+rank_v) + w_k/(k+rank_k). 순위는 1부터다."""
    vector = vector_order(vector)
    keyword = keyword_order(keyword)
    s_v = minmax(vector)
    s_k = keyword_scores(keyword)
    cosine = {hit.unit_no: hit.cosine for hit in vector}
    v_rank = _ranks(hit.unit_no for hit in vector)
    k_rank = _ranks(hit.unit_no for hit in keyword)
    items: list[Scored] = []
    for unit_no in _union(vector, keyword):
        rv = v_rank.get(unit_no)
        rk = k_rank.get(unit_no)
        score = (vector_weight / (k + rv) if rv is not None else 0.0) + (
            keyword_weight / (k + rk) if rk is not None else 0.0
        )
        items.append(
            Scored(
                unit_no=unit_no,
                score=score,
                cosine=cosine.get(unit_no),
                vector_score=s_v.get(unit_no),
                keyword_score=s_k.get(unit_no),
                vector_rank=rv,
                keyword_rank=rk,
            )
        )
    return sorted(items, key=sort_key)


def vector_only(vector: Sequence[Hit]) -> list[Scored]:
    """벡터 검색만이면 점수는 코사인 원값이다. 문턱과 같은 척도라 화면에서 읽기 쉽다."""
    vector = vector_order(vector)
    s_v = minmax(vector)
    return [
        Scored(
            unit_no=hit.unit_no,
            score=hit.cosine,
            cosine=hit.cosine,
            vector_score=s_v[hit.unit_no],
            vector_rank=rank,
        )
        for rank, hit in enumerate(vector, start=1)
    ]


def keyword_only(keyword: Sequence[FtsHit]) -> list[Scored]:
    """전문 검색만이면 점수는 bm25 / 목록 최대값이다. 전문 검색에는 문턱이 없다(7.3)."""
    keyword = keyword_order(keyword)
    s_k = keyword_scores(keyword)
    items = [
        Scored(
            unit_no=hit.unit_no,
            score=s_k[hit.unit_no],
            keyword_score=s_k[hit.unit_no],
            keyword_rank=rank,
        )
        for rank, hit in enumerate(keyword, start=1)
    ]
    return sorted(items, key=sort_key)
