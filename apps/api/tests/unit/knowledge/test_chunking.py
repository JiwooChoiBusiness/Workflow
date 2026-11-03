import pytest

from app.knowledge.chunking import (
    WARN_FULL_DOC_TRUNCATED,
    WARN_QA_EMPTY_ROWS,
    WARN_SEPARATOR_OVERLAP,
    ChildRule,
    GeneralRule,
    ParentChildRule,
    ParentRule,
    QaPair,
    effective_limit,
    parse_qa_csv,
    qa_chunks,
    split_general,
    split_parent_child,
)
from app.knowledge.tokens import estimate_tokens

# 한글 140자는 100 weight, 추정 110토큰이다.
PARAGRAPH = "가" * 140


def test_small_paragraphs_are_merged_up_to_the_limit_and_separators_dropped() -> None:
    text = "\n\n".join([PARAGRAPH] * 6)

    chunks = split_general(text, GeneralRule(max_tokens=250, overlap_tokens=0))

    # 두 문단이 221토큰, 세 문단이면 331토큰이라 둘씩 묶인다. 묶인 안쪽 구분자는 되살아난다.
    assert [chunk.content for chunk in chunks] == [f"{PARAGRAPH}\n\n{PARAGRAPH}"] * 3
    assert all(chunk.tokens == estimate_tokens(chunk.content) <= 250 for chunk in chunks)


def test_overlap_prepends_the_tail_of_the_previous_chunk() -> None:
    text = "\n\n".join([PARAGRAPH] * 6)

    chunks = split_general(text, GeneralRule(max_tokens=250, overlap_tokens=20))

    assert chunks[0].content == f"{PARAGRAPH}\n\n{PARAGRAPH}"
    # 공백 없는 한글 구간이라 글자 단위로 자른 끝 25자(20토큰)가 문단 구분자와 함께 붙는다.
    assert chunks[1].content == "가" * 25 + f"\n\n{PARAGRAPH}\n\n{PARAGRAPH}"
    assert len(chunks) == 3
    assert max(chunk.tokens for chunk in chunks) <= 250


def test_overlap_starts_at_a_word_boundary() -> None:
    text = " ".join(f"word{index}" for index in range(300))

    chunks = split_general(text, GeneralRule(max_tokens=100, overlap_tokens=10))

    assert len(chunks) > 3
    for previous, current in zip(chunks, chunks[1:], strict=False):
        words = current.content.split(" ")
        carried = next(
            count
            for count in range(len(words), 0, -1)
            if previous.content.endswith(" " + " ".join(words[:count]))
        )
        assert 1 <= carried and estimate_tokens(" ".join(words[:carried])) <= 10
    assert max(chunk.tokens for chunk in chunks) <= 100


def test_oversized_piece_is_split_by_the_next_separator() -> None:
    text = "\n".join([PARAGRAPH] * 4)

    chunks = split_general(text, GeneralRule(max_tokens=250, overlap_tokens=0))

    assert [chunk.content for chunk in chunks] == [f"{PARAGRAPH}\n{PARAGRAPH}"] * 2


def test_sentences_and_words_are_the_last_separators() -> None:
    sentence = "가" * 70
    text = ". ".join([sentence] * 6)

    chunks = split_general(text, GeneralRule(max_tokens=120, overlap_tokens=0))

    # 문장 하나가 55토큰, 둘이면 111토큰이다. 묶음 끝의 ". "는 빠진다.
    assert [chunk.content for chunk in chunks] == [f"{sentence}. {sentence}"] * 3


def test_text_without_any_separator_is_cut_by_characters() -> None:
    text = "가" * 1000

    chunks = split_general(text, GeneralRule(max_tokens=100, overlap_tokens=0))

    # 100토큰은 한글 127자다(128자면 101토큰).
    assert [len(chunk.content) for chunk in chunks] == [127] * 7 + [111]
    assert "".join(chunk.content for chunk in chunks) == text
    assert max(chunk.tokens for chunk in chunks) <= 100


def test_embedding_input_limit_cuts_at_80_percent() -> None:
    assert effective_limit(4000, 2048) == 1638
    assert effective_limit(1024, 8192) == 1024
    assert effective_limit(4000, None) == 4000

    chunks = split_general(
        "가" * 5000, GeneralRule(max_tokens=4000, overlap_tokens=0), input_limit=2048
    )

    assert 1600 < max(chunk.tokens for chunk in chunks) <= 1638


def test_blank_text_has_no_chunks() -> None:
    assert split_general(" \n\n \n") == []


def test_parent_child_splits_parents_by_paragraph_and_children_by_line() -> None:
    lines = "\n".join([PARAGRAPH] * 3)
    text = "\n\n".join([lines] * 6)

    result = split_parent_child(text)

    # 문단 하나가 331토큰이라 부모(1024)에 셋씩, 자식(256)에는 줄이 둘씩 들어간다.
    assert result.warnings == []
    assert len(result.chunks) == 2
    assert [len(chunk.children) for chunk in result.chunks] == [5, 5]
    assert all(chunk.tokens <= 1024 for chunk in result.chunks)
    for chunk in result.chunks:
        assert all(child.tokens <= 256 for child in chunk.children)
        assert chunk.children[0].content == f"{PARAGRAPH}\n{PARAGRAPH}"


def test_full_doc_parent_takes_the_first_10000_tokens() -> None:
    rule = ParentChildRule(parent=ParentRule(mode="full_doc"))

    long = split_parent_child("다" * 20_000, rule)
    short = split_parent_child("짧은 문서다.", rule)

    assert long.warnings == [WARN_FULL_DOC_TRUNCATED]
    assert [chunk.tokens for chunk in long.chunks] == [10_000]
    assert all(child.tokens <= 256 for child in long.chunks[0].children)
    assert short.warnings == []
    assert [chunk.content for chunk in short.chunks] == ["짧은 문서다."]


def test_child_separators_inside_parent_separators_warn() -> None:
    rule = ParentChildRule(
        parent=ParentRule(separators=("\n\n", "\n")), child=ChildRule(separators=("\n",))
    )

    assert split_parent_child("가\n나", rule).warnings == [WARN_SEPARATOR_OVERLAP]


def test_child_chunks_respect_the_embedding_input_limit() -> None:
    # 부모는 임베딩하지 않아 4000토큰까지 두고, 자식만 입력 상한의 80%(1638)에서 자른다.
    rule = ParentChildRule(parent=ParentRule(max_tokens=4000), child=ChildRule(max_tokens=4000))

    result = split_parent_child("가" * 3000, rule, input_limit=2048)

    assert [chunk.tokens for chunk in result.chunks] == [estimate_tokens("가" * 3000)]
    assert [child.tokens for child in result.chunks[0].children] == [1638, 720]


def test_qa_csv_uses_header_names_in_any_order() -> None:
    text = chr(0xFEFF) + 'answer,question\n"답, 하나",질문1\n\n답2,질문2\n'

    assert parse_qa_csv(text) == [QaPair("질문1", "답, 하나"), QaPair("질문2", "답2")]


def test_qa_csv_without_named_header_uses_first_two_columns() -> None:
    assert parse_qa_csv("Q,A,메모\n질문1,답1,무시\n") == [QaPair("질문1", "답1")]


def test_qa_chunks_embed_the_question_and_keep_the_answer() -> None:
    pairs = [QaPair(" 연차는 며칠? ", "15일"), QaPair("빈 답", " "), QaPair("", "질문 없음")]

    result = qa_chunks(pairs)

    assert result.warnings == [WARN_QA_EMPTY_ROWS]
    assert [(chunk.content, chunk.answer) for chunk in result.chunks] == [("연차는 며칠?", "15일")]
    assert result.chunks[0].tokens == estimate_tokens("연차는 며칠?")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_tokens": 0},
        {"max_tokens": 4001},
        {"max_tokens": 100, "overlap_tokens": 100},
        {"separators": ("\n", "")},
    ],
)
def test_invalid_rules_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        GeneralRule(**kwargs)  # type: ignore[arg-type]
