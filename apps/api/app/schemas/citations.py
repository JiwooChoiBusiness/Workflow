"""인용 항목(3단계 설계 8.3, 8.4, 12.4). 검색 결과 하나를 메시지·런·이벤트가 같은 모양으로 싣는다.

필드 이름은 Dify 인용 API(RetrieverResource)를 따르고 검색 서비스의 RetrieverResource와 같다.
이벤트 스키마가 지식 베이스 화면 쪽 모델(app/schemas/knowledge.py)에 묶이지 않게 따로 둔다.

번호(position)는 런 전체의 번호표(세그먼트 id → 번호)에서 받는다. 워크플로우는 엔진, 에이전트는
루프가 번호표를 들고 체크포인트·스텝 메시지로 재개 뒤에도 잇는다. 그래서 iteration 안의 llm이나
context를 쓰는 llm이 여럿이어도 같은 번호가 두 세그먼트를 가리키지 않는다.

저장(체크포인트, message·wf_message·wf_run의 retriever_resources)은 position 순으로 50개까지이고
content·answer는 2,000자, 자식 청크 글은 500자로 자른다. 전문은 segment_id로 청크 API에서 본다.
이벤트(workflow.completed, tool_call.completed)는 20개까지, 글을 500자로 줄여 싣는다.

모델에게 보여 줄 때는 자료마다 <document> 표식으로 감싼다(14.1). 이름의 개행·제어 문자는 공백으로
접고, 본문 속 닫는 표식은 무력화해 문서 내용이 자료 밖의 지시나 가짜 항목처럼 보이지 않게 한다.
"""

import html
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from pydantic import BaseModel, Field, ValidationError

RUN_RESOURCES_MAX = 50
EVENT_RESOURCES_MAX = 20
EVENT_TEXT_CHARS = 500
STORED_TEXT_CHARS = 2000
STORED_CHILD_CHARS = 500

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
# 본문 속 자료 표식(<document, </document). 앞에 \를 넣어 표식으로 읽히지 않게 한다.
_MARKER = re.compile(r"<(/?)(document)", re.IGNORECASE)


class CitedChildChunk(BaseModel):
    id: uuid.UUID
    content: str
    position: int
    score: float


class CitedFusion(BaseModel):
    cosine: float | None = None
    vector_score: float | None = None
    keyword_score: float | None = None
    vector_rank: int | None = None
    keyword_rank: int | None = None


class RetrieverResourceData(BaseModel):
    """인용 하나. position은 런 전체에서 세그먼트마다 하나이고, 모델이 본 [n]과 같다."""

    position: int
    dataset_id: uuid.UUID
    dataset_name: str
    document_id: uuid.UUID
    document_name: str
    data_source_type: str
    segment_id: uuid.UUID
    segment_position: int
    score: float
    hit_count: int = 0
    word_count: int = 0
    # Parent-child는 부모 청크의 글이다. 저장본은 2,000자까지다.
    content: str
    # Q&A 문서의 답.
    answer: str | None = None
    doc_metadata: dict[str, Any] = Field(default_factory=dict[str, Any])
    child_chunks: list[CitedChildChunk] | None = None
    fusion: CitedFusion | None = None
    fusion_score: float | None = None
    indexing_status: str | None = None


def cite(numbers: dict[str, int], segment_id: str) -> int:
    """런 전체 번호표에서 세그먼트의 번호. 처음 보는 세그먼트는 지금까지의 가장 큰 번호 다음이다.

    실패한 노드가 받아 간 번호는 비어 있을 수 있다. 같은 세그먼트로 다시 하면 같은 번호를 받는다.
    """
    number = numbers.get(segment_id)
    if number is None:
        number = max(numbers.values(), default=0) + 1
        numbers[segment_id] = number
    return number


def _segment(item: Mapping[str, Any]) -> str:
    return str(item.get("segment_id") or "")


def _position(item: Mapping[str, Any]) -> int:
    value = item.get("position")
    return value if isinstance(value, int) else 0


def _cut(value: object, limit: int) -> object:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


def _trimmed(item: Mapping[str, Any], text_chars: int, child_chars: int) -> dict[str, Any]:
    data = dict(item)
    data["content"] = _cut(data.get("content"), text_chars)
    if data.get("answer") is not None:
        data["answer"] = _cut(data["answer"], text_chars)
    children = data.get("child_chunks")
    if isinstance(children, list):
        data["child_chunks"] = [
            {**child, "content": _cut(child.get("content"), child_chars)}
            for child in cast(list[Any], children)
            if isinstance(child, dict)
        ]
    return data


def merge_resources(
    groups: Iterable[Sequence[Mapping[str, Any]]], limit: int = RUN_RESOURCES_MAX
) -> list[dict[str, Any]]:
    """여러 목록을 합친 저장 모양. 같은 세그먼트는 먼저 온 것만 남기고 position 순으로 limit개다."""
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            key = _segment(item)
            if key and key not in merged:
                merged[key] = _trimmed(item, STORED_TEXT_CHARS, STORED_CHILD_CHARS)
    return sorted(merged.values(), key=_position)[:limit]


def event_resources(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """이벤트에 싣는 모양. 앞 20개, content·answer·자식 청크 글은 500자까지다(8.3)."""
    return [
        _trimmed(item, EVENT_TEXT_CHARS, EVENT_TEXT_CHARS) for item in items[:EVENT_RESOURCES_MAX]
    ]


def stored_resources(raw: object) -> list[RetrieverResourceData]:
    """저장 JSON(목록)을 응답 모델로. 읽지 못한 항목은 버린다."""
    if not isinstance(raw, list):
        return []
    found: list[RetrieverResourceData] = []
    for item in cast(list[Any], raw):
        try:
            found.append(RetrieverResourceData.model_validate(item))
        except ValidationError:
            continue
    return found


def one_line(text: str) -> str:
    """개행·제어 문자를 공백으로 접은 한 줄. knowledge_index 이름 템플릿이나 서비스 API의 문서
    이름에 개행이 들어와도 자료 머리가 여러 줄로 갈라지지 않는다."""
    return " ".join(_CONTROL.sub(" ", text).split())


def document_block(
    body: str,
    *,
    index: int | None,
    name: str = "",
    source: str = "",
    score: float | None = None,
) -> str:
    """자료 하나를 <document index="n" name="…">…</document>로 감싼다.

    본문의 "</document"·"<document"는 "<\\/document"·"<\\document"로 바꿔 자료를 일찍 닫거나
    가짜 자료를 열지 못하게 한다. index가 없으면 인용이 아닌 글(검색 결과 모양이 아닌 context
    항목)이다.
    """
    attributes: list[str] = []
    if index is not None:
        attributes.append(f'index="{index}"')
    for key, value in (("name", name), ("source", source)):
        folded = one_line(value)
        if folded:
            attributes.append(f'{key}="{html.escape(folded, quote=True)}"')
    if score is not None:
        attributes.append(f'score="{score:.2f}"')
    head = " ".join(["<document", *attributes])
    safe = _MARKER.sub(r"<\\\1\2", body)
    return f"{head}>\n{safe}\n</document>"
