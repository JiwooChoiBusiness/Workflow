from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from app.knowledge.chunking import DocForm

# vec0의 doc_id 열은 글자 비교라 색인(M22)과 검색 필터(M23)가 같은 꼴을 써야 한다. 이 함수 하나로만
# 만든다. app.db의 UUID 열과 같은 하이픈 있는 소문자 36자다.


def doc_id_of(document_id: UUID | str) -> str:
    return str(document_id if isinstance(document_id, UUID) else UUID(document_id))


# 유닛은 벡터·전문 색인의 행 하나다(설계 2장). General·Q&A는 세그먼트, Parent-child는 자식 청크이고
# app.db의 unit_no가 index.db의 rowid다. summary는 요약 인덱스(후속)의 자리다.
UnitKind = Literal["segment", "child", "summary"]


@dataclass(frozen=True, slots=True)
class VecRow:
    unit_no: int
    vector: bytes
    doc_id: str
    unit_kind: UnitKind = "segment"


@dataclass(frozen=True, slots=True)
class FtsRow:
    # 원문이다. bigram 변환은 FtsIndex가 한다.
    unit_no: int
    text: str


@dataclass(frozen=True, slots=True)
class Hit:
    unit_no: int
    cosine: float


@dataclass(frozen=True, slots=True)
class FtsHit:
    # FTS5 bm25()는 작을수록(음수) 관련이 높아 부호를 바꿔 담는다.
    unit_no: int
    score: float


@dataclass(frozen=True)
class IndexMeta:
    # index.db kb_index 행. config_key·embedding_version이 app.db와 다르면 다시 만든다(5.4).
    kb_id: UUID
    workspace_id: UUID
    vec_table: str | None
    fts_table: str | None
    dims: int | None
    config_key: str | None
    embedding_version: int
    unit_count: int
    stop_bigrams: tuple[str, ...]
    updated_at: datetime


def unit_kind_for(doc_form: DocForm) -> UnitKind:
    return "child" if doc_form == "hierarchical_model" else "segment"


def vec_table_name(kb_id: UUID) -> str:
    # 테이블 이름을 SQL 문자열에 넣으므로 UUID의 hex(0-9a-f)에서만 만든다.
    return f"vec_{kb_id.hex}"


def fts_table_name(kb_id: UUID) -> str:
    return f"fts_{kb_id.hex}"


def fts_text(content: str, answer: str | None = None) -> str:
    # Q&A는 질문만 임베딩하지만 전문 색인에는 질문과 답을 같이 넣는다(5.3).
    return f"{content}\n{answer}" if answer else content
