"""검색 결과를 응답 모양으로(3단계 설계 7.7, 8.1, 8.5).

app.db 행은 부르는 쪽이 아래 데이터 클래스로 옮겨 넘긴다. 같은 결과 하나가 노드·도구·인용에서는
RetrieverResource로, 검색 시험·datasets retrieve에서는 Dify retrieve의 record로 나간다.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.knowledge.retrieval.fusion import Scored
from app.knowledge.retrieval.service import RankedSegment
from app.schemas.knowledge import IndexingStatus
from app.schemas.retrieval import (
    ChildChunkHit,
    FusionDetail,
    RetrieveDocument,
    RetrieveRecord,
    RetrieverResource,
    RetrieveSegment,
)


@dataclass(frozen=True, slots=True)
class KbInfo:
    id: UUID
    name: str


@dataclass(frozen=True)
class DocumentInfo:
    id: UUID
    name: str
    data_source_type: str
    indexing_status: IndexingStatus
    # 필드 이름 → 값.
    doc_metadata: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True, slots=True)
class SegmentInfo:
    id: UUID
    document_id: UUID
    position: int
    content: str
    answer: str | None
    word_count: int
    tokens: int
    hit_count: int
    enabled: bool
    status: str


@dataclass(frozen=True, slots=True)
class ChildInfo:
    id: UUID
    content: str
    position: int


def fusion_detail(scored: Scored) -> FusionDetail:
    return FusionDetail(
        cosine=scored.cosine,
        vector_score=scored.vector_score,
        keyword_score=scored.keyword_score,
        vector_rank=scored.vector_rank,
        keyword_rank=scored.keyword_rank,
    )


@dataclass(frozen=True)
class RetrievedRecord:
    """최종 결과 하나. position은 1부터의 순위다."""

    position: int
    kb: KbInfo
    ranked: RankedSegment
    segment: SegmentInfo
    document: DocumentInfo
    # Parent-child에서 맞은 자식(점수순). 자식 id → 내용.
    children: tuple[ChildInfo, ...] = ()

    @property
    def score(self) -> float:
        return self.ranked.score

    def child_hits(self) -> list[ChildChunkHit] | None:
        if not self.ranked.children:
            return None
        by_id = {child.id: child for child in self.children}
        hits: list[ChildChunkHit] = []
        for child_hit in self.ranked.children:
            child = by_id.get(child_hit.child_id)
            if child is not None:
                hits.append(
                    ChildChunkHit(
                        id=child.id,
                        content=child.content,
                        position=child.position,
                        score=child_hit.scored.score,
                    )
                )
        return hits

    def resource(self) -> RetrieverResource:
        return RetrieverResource(
            position=self.position,
            dataset_id=self.kb.id,
            dataset_name=self.kb.name,
            document_id=self.document.id,
            document_name=self.document.name,
            data_source_type=self.document.data_source_type,
            segment_id=self.segment.id,
            segment_position=self.segment.position,
            score=self.ranked.score,
            hit_count=self.segment.hit_count,
            word_count=self.segment.word_count,
            content=self.segment.content,
            answer=self.segment.answer,
            doc_metadata=dict(self.document.doc_metadata),
            child_chunks=self.child_hits(),
            fusion=fusion_detail(self.ranked.best),
            fusion_score=self.ranked.fusion_score,
            indexing_status=self.document.indexing_status,
        )

    def record(self) -> RetrieveRecord:
        return RetrieveRecord(
            segment=RetrieveSegment(
                id=self.segment.id,
                position=self.segment.position,
                document_id=self.document.id,
                content=self.segment.content,
                answer=self.segment.answer,
                word_count=self.segment.word_count,
                tokens=self.segment.tokens,
                hit_count=self.segment.hit_count,
                enabled=self.segment.enabled,
                status=self.segment.status,
                document=RetrieveDocument(
                    id=self.document.id,
                    data_source_type=self.document.data_source_type,
                    name=self.document.name,
                    doc_metadata=dict(self.document.doc_metadata),
                    indexing_status=self.document.indexing_status,
                ),
            ),
            child_chunks=self.child_hits(),
            score=self.ranked.score,
            fusion=fusion_detail(self.ranked.best),
            fusion_score=self.ranked.fusion_score,
        )

    def passage(self) -> str:
        """LLM 재순위에 보내는 글. Q&A는 질문과 답을 같이 보낸다."""
        if self.segment.answer:
            return f"{self.segment.content}\n{self.segment.answer}"
        return self.segment.content
