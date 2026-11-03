"""손으로 고친 청크와 분할기가 같은 토큰 추정·정규화를 쓴다(M21 검토 2-1, 2-2).

서비스 쪽에 따로 있던 추정은 한글 길이 149곳에서 1이 달랐고 자모를 절반으로 셌다. 분할기가 상한에
딱 맞춰 만든 청크를 오타만 고쳐도 content_too_long이 되거나, 고친 청크의 content_hash가 인제스트
결과와 달라 임베딩 캐시를 놓쳤다.
"""

from app.knowledge.chunking import GeneralRule, split_general
from app.knowledge.cleaning import normalize
from app.knowledge.tokens import estimate_tokens
from app.llm.embeddings import content_hash
from app.services import knowledge as knowledge_service
from app.services.errors import Problems
from app.services.knowledge_segments import clean_chunk


def tokens_of(text: str, limit: int = 100_000) -> tuple[str, int]:
    problems = Problems()
    return clean_chunk(text, limit, ("content",), problems)


def test_the_service_counts_tokens_like_the_chunker() -> None:
    samples = ["한" * length for length in range(1, 3001, 7)] + [
        "ㅋ" * 14,
        "ﾊﾝｶｸ",
        "𠀀𠀁 확장 B",
        "영문 mixed 텍스트 123",
        "a" * 999,
    ]
    for text in samples:
        normalized, tokens = tokens_of(text)
        assert tokens == estimate_tokens(normalize(text)), text[:20]
    assert tokens_of("ㅋ" * 14)[1] == 11
    assert not hasattr(knowledge_service, "estimate_tokens")
    assert not hasattr(knowledge_service, "normalize_text")
    assert not hasattr(knowledge_service, "content_hash")


def test_a_chunk_cut_at_the_limit_can_be_saved_unchanged() -> None:
    text = "\n\n".join("가나다라마바사아자차카타파하 " * 30 for _ in range(10))
    rule = GeneralRule(max_tokens=97, overlap_tokens=0)

    for chunk in split_general(text, rule):
        problems = Problems()
        _, tokens = clean_chunk(chunk.content, rule.max_tokens, ("content",), problems)
        assert tokens == chunk.tokens <= rule.max_tokens
        assert problems.items == []


def test_edited_chunks_are_normalized_like_ingested_text() -> None:
    normalized, _ = tokens_of("첫 줄\r\n둘째\x00 줄﻿")

    assert normalized == "첫 줄\n둘째 줄"
    assert content_hash(normalized) == content_hash(normalize("첫 줄\r\n둘째\x00 줄﻿"))
