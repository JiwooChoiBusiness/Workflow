import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.knowledge.tokens import char_weight, estimate_tokens, token_weight, tokens_from_weight

DocForm = Literal["text_model", "hierarchical_model", "qa_model"]

# 설계 6.3. Dify 문서에 기본값이 없어 우리 값이다. 단위는 tokens.py의 추정 토큰이다.
DEFAULT_SEPARATORS = ("\n\n", "\n", ". ", " ")
DEFAULT_MAX_TOKENS = 1024
MAX_TOKENS_LIMIT = 4000
DEFAULT_OVERLAP_TOKENS = 50
PARENT_SEPARATORS = ("\n\n",)
CHILD_SEPARATORS = ("\n", ". ")
CHILD_MAX_TOKENS = 256
FULL_DOC_MAX_TOKENS = 10_000
# 입력 상한을 넘는 글은 임베딩 API가 오류 없이 잘라 버리므로 상한의 80%에서 우리가 자른다.
INPUT_LIMIT_RATIO = 0.8

WARN_SEPARATOR_OVERLAP = "separator_overlap"
WARN_FULL_DOC_TRUNCATED = "full_doc_truncated"
WARN_QA_EMPTY_ROWS = "qa_empty_rows"

QA_QUESTION_COLUMN = "question"
QA_ANSWER_COLUMN = "answer"
BOM = chr(0xFEFF)


def _check_rule(separators: Sequence[str], max_tokens: int) -> None:
    if any(not separator for separator in separators):
        raise ValueError("빈 구분자는 쓸 수 없다")
    if not 1 <= max_tokens <= MAX_TOKENS_LIMIT:
        raise ValueError(f"max_tokens는 1~{MAX_TOKENS_LIMIT}이다: {max_tokens}")


@dataclass(frozen=True)
class GeneralRule:
    separators: tuple[str, ...] = DEFAULT_SEPARATORS
    max_tokens: int = DEFAULT_MAX_TOKENS
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS

    def __post_init__(self) -> None:
        _check_rule(self.separators, self.max_tokens)
        if not 0 <= self.overlap_tokens < self.max_tokens:
            raise ValueError("overlap_tokens는 0 이상 max_tokens 미만이다")


@dataclass(frozen=True)
class ParentRule:
    mode: Literal["paragraph", "full_doc"] = "paragraph"
    separators: tuple[str, ...] = PARENT_SEPARATORS
    max_tokens: int = DEFAULT_MAX_TOKENS

    def __post_init__(self) -> None:
        _check_rule(self.separators, self.max_tokens)


@dataclass(frozen=True)
class ChildRule:
    separators: tuple[str, ...] = CHILD_SEPARATORS
    max_tokens: int = CHILD_MAX_TOKENS

    def __post_init__(self) -> None:
        _check_rule(self.separators, self.max_tokens)


@dataclass(frozen=True)
class ParentChildRule:
    parent: ParentRule = field(default_factory=ParentRule)
    child: ChildRule = field(default_factory=ChildRule)


@dataclass(frozen=True)
class ChildChunk:
    content: str
    tokens: int


@dataclass(frozen=True)
class Chunk:
    # tokens는 content의 추정 토큰이다. Q&A에서 content는 질문이고 임베딩 입력도 질문뿐이다.
    content: str
    tokens: int
    answer: str | None = None
    children: tuple[ChildChunk, ...] = ()


@dataclass(frozen=True)
class ChunkResult:
    chunks: list[Chunk]
    warnings: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class QaPair:
    question: str
    answer: str


def effective_limit(max_tokens: int, input_limit: int | None) -> int:
    if input_limit is None:
        return max_tokens
    return max(1, min(max_tokens, int(input_limit * INPUT_LIMIT_RATIO)))


def split_general(
    text: str, rule: GeneralRule | None = None, *, input_limit: int | None = None
) -> list[Chunk]:
    """재귀 분할. input_limit은 임베딩 모델의 입력 상한(토큰)이고 색인만 하는 글이면 None이다."""
    rule = rule or GeneralRule()
    limit = effective_limit(rule.max_tokens, input_limit)
    overlap = min(rule.overlap_tokens, limit - 1)
    # 겹침을 붙여도 상한을 넘지 않게 본문 몫을 미리 줄여 둔다.
    budget = limit - overlap
    atoms: list[tuple[str, str]] = []
    _collect(text, rule.separators, budget, "", atoms)
    return _with_overlap(_merge(atoms, budget), overlap, limit)


def split_parent_child(
    text: str, rule: ParentChildRule | None = None, *, input_limit: int | None = None
) -> ChunkResult:
    # 부모는 검색 결과로 돌려주는 단위라 임베딩하지 않는다. 입력 상한은 자식에만 건다.
    rule = rule or ParentChildRule()
    warnings: list[str] = []
    if set(rule.child.separators) <= set(rule.parent.separators):
        warnings.append(WARN_SEPARATOR_OVERLAP)
    if rule.parent.mode == "full_doc":
        body, truncated = _head(text.strip(), FULL_DOC_MAX_TOKENS)
        if truncated:
            warnings.append(WARN_FULL_DOC_TRUNCATED)
        parents = [body] if body else []
    else:
        parent_rule = GeneralRule(rule.parent.separators, rule.parent.max_tokens, 0)
        parents = [chunk.content for chunk in split_general(text, parent_rule)]
    child_rule = GeneralRule(rule.child.separators, rule.child.max_tokens, 0)
    chunks: list[Chunk] = []
    for parent in parents:
        children = split_general(parent, child_rule, input_limit=input_limit)
        chunks.append(
            Chunk(
                content=parent,
                tokens=estimate_tokens(parent),
                children=tuple(ChildChunk(child.content, child.tokens) for child in children),
            )
        )
    return ChunkResult(chunks, warnings)


def qa_chunks(pairs: Iterable[QaPair]) -> ChunkResult:
    chunks: list[Chunk] = []
    empty = 0
    for pair in pairs:
        question, answer = pair.question.strip(), pair.answer.strip()
        if not question or not answer:
            empty += 1
            continue
        chunks.append(Chunk(content=question, tokens=estimate_tokens(question), answer=answer))
    return ChunkResult(chunks, [WARN_QA_EMPTY_ROWS] if empty else [])


def parse_qa_csv(text: str) -> list[QaPair]:
    # 첫 줄은 언제나 머리글이다. question·answer 이름이 있으면 그 열을, 없으면 1·2열을 쓴다.
    rows = list(csv.reader(io.StringIO(text.removeprefix(BOM))))
    if not rows:
        return []
    header = [cell.strip().casefold() for cell in rows[0]]
    if QA_QUESTION_COLUMN in header and QA_ANSWER_COLUMN in header:
        question_at, answer_at = header.index(QA_QUESTION_COLUMN), header.index(QA_ANSWER_COLUMN)
    else:
        question_at, answer_at = 0, 1
    pairs: list[QaPair] = []
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        question = row[question_at] if question_at < len(row) else ""
        answer = row[answer_at] if answer_at < len(row) else ""
        pairs.append(QaPair(question, answer))
    return pairs


def _fits(text: str, budget: int) -> bool:
    return tokens_from_weight(token_weight(text)) <= budget


def _collect(
    text: str, separators: Sequence[str], budget: int, lead: str, out: list[tuple[str, str]]
) -> None:
    # 상한 안의 조각(atom)과 그 앞에 있던 구분자를 순서대로 모은다. 구분자는 조각에서 빠진다.
    if not text.strip():
        return
    if _fits(text, budget):
        out.append((text, lead))
        return
    for position, separator in enumerate(separators):
        if separator not in text:
            continue
        rest = separators[position + 1 :]
        first = True
        for part in text.split(separator):
            if part.strip():
                _collect(part, rest, budget, lead if first else separator, out)
                first = False
        return
    for index, piece in enumerate(_hard_split(text, budget)):
        out.append((piece, lead if index == 0 else ""))


def _merge(atoms: Sequence[tuple[str, str]], budget: int) -> list[tuple[str, str]]:
    # 작은 조각을 상한까지 합친다. 합칠 때는 원래 사이에 있던 구분자를 되살린다.
    merged: list[tuple[str, str]] = []
    parts: list[str] = []
    weight = 0.0
    boundary = ""
    for atom, separator in atoms:
        atom_weight = token_weight(atom)
        if parts:
            joined = weight + token_weight(separator) + atom_weight
            if tokens_from_weight(joined) <= budget:
                parts.extend((separator, atom))
                weight = joined
                continue
            merged.append(("".join(parts), boundary))
        parts, weight, boundary = [atom], atom_weight, separator
    if parts:
        merged.append(("".join(parts), boundary))
    return merged


def _with_overlap(pieces: Sequence[tuple[str, str]], overlap: int, limit: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    previous: str | None = None
    for text, boundary in pieces:
        body = text.strip()
        if not body:
            continue
        content = body
        if previous is not None and overlap > 0:
            content = _prepend_tail(previous, boundary, body, overlap, limit)
        chunks.append(Chunk(content=content, tokens=estimate_tokens(content)))
        previous = body
    return chunks


def _prepend_tail(previous: str, separator: str, body: str, overlap: int, limit: int) -> str:
    room = min(overlap, limit - estimate_tokens(separator + body))
    if room <= 0:
        return body
    tail = _tail(previous, room)
    # 조각마다 따로 올림한 값의 합이라 이어 붙이면 1토큰 넘칠 수 있다. 앞 글자를 떼어 맞춘다.
    for _ in range(8):
        if not tail:
            break
        content = tail + separator + body
        if estimate_tokens(content) <= limit:
            return content
        tail = tail[1:].lstrip()
    return body


def _tail(text: str, max_tokens: int) -> str:
    start = len(text)
    weight = 0.0
    while start > 0:
        char_tokens = char_weight(text[start - 1])
        if tokens_from_weight(weight + char_tokens) > max_tokens:
            break
        weight += char_tokens
        start -= 1
    tail = text[start:]
    if start > 0 and not text[start - 1].isspace():
        # 단어 중간에서 시작하지 않게 첫 공백 뒤로 옮긴다. 공백이 없는 긴 한글 구간이면 그대로 둔다.
        cut = next((index for index, char in enumerate(tail) if char.isspace()), -1)
        if cut != -1:
            tail = tail[cut + 1 :]
    return tail.strip()


def _hard_split(text: str, budget: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    weight = 0.0
    for index, char in enumerate(text):
        char_tokens = char_weight(char)
        if index > start and tokens_from_weight(weight + char_tokens) > budget:
            pieces.append(text[start:index])
            start, weight = index, 0.0
        weight += char_tokens
    pieces.append(text[start:])
    return [piece for piece in pieces if piece.strip()]


def _head(text: str, max_tokens: int) -> tuple[str, bool]:
    if _fits(text, max_tokens):
        return text, False
    weight = 0.0
    for index, char in enumerate(text):
        char_tokens = char_weight(char)
        if tokens_from_weight(weight + char_tokens) > max_tokens:
            return text[:index].strip(), True
        weight += char_tokens
    return text, False
