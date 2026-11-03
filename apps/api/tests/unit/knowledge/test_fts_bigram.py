import unicodedata
import uuid
from typing import Any

import pytest

from app.knowledge import index_db as index_db_module
from app.knowledge.fts_index import (
    MAX_QUERY_TERMS,
    SqliteFtsIndex,
    bigram_text,
    bigram_tokens,
    match_expression,
    query_terms,
)
from app.knowledge.index_db import IndexDb
from app.knowledge.units import FtsRow, fts_text

WORKSPACE = uuid.UUID("01890000-0000-7000-8000-000000000004")


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("회의록", ["회의", "의록"]),
        ("학회의", ["학회", "회의"]),
        ("주간 회의록 작성", ["주간", "회의", "의록", "작성"]),
        ("책", ["책"]),
        ("ERR-4417은 시간 초과", ["err-4417", "은", "시간", "초과"]),
        ("27인치 4K모니터", ["27", "인치", "4k", "모니", "니터"]),
        ("東京タワー", ["東京", "京タ", "タワ", "ワー"]),
        ("(VPN), OTP!", ["vpn", "otp"]),
        ("--- ... ???", []),
    ],
)
def test_bigram_tokens_follow_design_5_3(text: str, tokens: list[str]) -> None:
    assert bigram_tokens(text) == tokens


def test_decomposed_hangul_gives_the_same_tokens() -> None:
    assert bigram_text(unicodedata.normalize("NFD", "회의록 작성")) == "회의 의록 작성"


def test_query_terms_are_deduplicated_and_pruned() -> None:
    assert query_terms("회의 회의록 회의") == ["회의", "의록"]
    assert query_terms("회의록 작성", stop={"회의"}) == ["의록", "작성"]
    # 전부 가지치기되면 가지치기 없이 보낸다.
    assert query_terms("회의", stop={"회의"}) == ["회의"]
    assert len(query_terms("가나다라마바사아자차카타파하" * 20)) <= MAX_QUERY_TERMS


def test_match_expression_quotes_every_term() -> None:
    assert match_expression(["and", "not", "*", 'a"b']) == '"and" OR "not" OR "*" OR "a""b"'


async def ready(
    db: IndexDb, texts: dict[int, str], **options: Any
) -> tuple[SqliteFtsIndex, uuid.UUID]:
    fts = SqliteFtsIndex(db, **options)
    kb_id = uuid.uuid4()
    await fts.ensure(kb_id, workspace_id=WORKSPACE)
    await fts.upsert(kb_id, [FtsRow(unit_no, text) for unit_no, text in texts.items()])
    return fts, kb_id


async def test_exact_bigram_matches_rank_first(fts_db: IndexDb) -> None:
    fts, kb_id = await ready(
        fts_db,
        {1: "주간 회의록 작성 방법", 2: "학회의 발표 순서", 3: "회의 일정 공지", 4: "출장비 정산"},
    )

    by_record = await fts.search(kb_id, "회의록", 10)
    by_meeting = await fts.search(kb_id, "회의", 10)

    assert by_record[0].unit_no == 1
    assert {hit.unit_no for hit in by_record} == {1, 2, 3}
    # "학회의"는 부분 문자열로 "회의"에 걸린다(설계 5.3·연구 3.1의 예문).
    assert {hit.unit_no for hit in by_meeting} == {1, 2, 3}
    assert all(hit.score > 0 for hit in by_record)
    assert [hit.score for hit in by_record] == sorted(
        (hit.score for hit in by_record), reverse=True
    )


async def test_operator_words_in_queries_are_plain_text(fts_db: IndexDb) -> None:
    fts, kb_id = await ready(fts_db, {1: "AND NOT OR 연산자", 2: '따옴표 "인용" 문장'})

    assert [hit.unit_no for hit in await fts.search(kb_id, "NOT", 5)] == [1]
    assert [hit.unit_no for hit in await fts.search(kb_id, '"인용"*', 5)] == [2]
    assert await fts.search(kb_id, "*** ---", 5) == []


async def test_upsert_replaces_and_delete_removes(fts_db: IndexDb) -> None:
    fts, kb_id = await ready(fts_db, {1: "회의록", 2: "출장비"})

    await fts.upsert(kb_id, [FtsRow(1, "경조사 휴가")])
    await fts.delete(kb_id, [2])

    assert await fts.search(kb_id, "회의록", 5) == []
    assert [hit.unit_no for hit in await fts.search(kb_id, "경조사", 5)] == [1]
    assert await fts.search(kb_id, "출장비", 5) == []
    assert await fts.count(kb_id) == 1


async def test_qa_body_holds_both_question_and_answer(fts_db: IndexDb) -> None:
    fts, kb_id = await ready(fts_db, {1: fts_text("연차는 며칠이야?", "입사 첫해 15일이다")})

    assert [hit.unit_no for hit in await fts.search(kb_id, "15일", 5)] == [1]


async def test_unit_numbers_are_listed_in_pages(
    fts_db: IndexDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_db_module, "LIST_PAGE", 2)
    fts, kb_id = await ready(fts_db, {unit_no: f"문단 {unit_no}" for unit_no in (5, 1, 4, 2, 3)})

    assert [unit_no async for unit_no in fts.list_unit_nos(kb_id)] == [1, 2, 3, 4, 5]


async def test_unknown_kb_has_no_index(fts_db: IndexDb) -> None:
    fts = SqliteFtsIndex(fts_db)
    kb_id = uuid.uuid4()

    assert await fts.search(kb_id, "회의", 5) == []
    assert await fts.count(kb_id) == 0
    await fts.delete(kb_id, [1])
    with pytest.raises(LookupError):
        await fts.upsert(kb_id, [FtsRow(1, "회의")])


async def test_common_bigrams_are_pruned_from_queries(fts_db: IndexDb) -> None:
    texts = {1: "회의 일정", 2: "회의 장소", 3: "회의 안건", 4: "출장 일정"}
    fts, kb_id = await ready(fts_db, texts, stop_ratio=0.5, stop_min_units=0)

    stop = await fts.refresh_stop_bigrams(kb_id)

    # "회의"는 4개 중 3개(75%)에 있어 빠진다. "일정"은 2개(50%)라 남는다.
    assert stop == ("회의",)
    meta = await fts_db.describe(kb_id)
    assert meta is not None and meta.stop_bigrams == ("회의",)
    assert {hit.unit_no for hit in await fts.search(kb_id, "회의 일정", 5)} == {1, 4}
    assert {hit.unit_no for hit in await fts.search(kb_id, "회의", 5)} == {1, 2, 3}


async def test_small_kb_is_not_pruned(fts_db: IndexDb) -> None:
    fts, kb_id = await ready(fts_db, {1: "회의 일정", 2: "회의 장소", 3: "회의 안건"})

    assert await fts.refresh_stop_bigrams(kb_id) == ()


async def test_stop_list_is_read_back_after_restart(fts_db: IndexDb) -> None:
    texts = {1: "회의 일정", 2: "회의 장소", 3: "회의 안건", 4: "출장 일정"}
    first, kb_id = await ready(fts_db, texts, stop_ratio=0.5, stop_min_units=0)
    await first.refresh_stop_bigrams(kb_id)

    fresh = SqliteFtsIndex(fts_db)

    assert {hit.unit_no for hit in await fresh.search(kb_id, "회의 일정", 5)} == {1, 4}


async def test_drop_removes_the_table(fts_db: IndexDb) -> None:
    fts, kb_id = await ready(fts_db, {1: "회의록"})

    await fts.drop(kb_id)

    assert await fts.search(kb_id, "회의록", 5) == []
    meta = await fts_db.describe(kb_id)
    assert meta is not None and meta.fts_table is None


async def test_keyword_questions_rank_first_on_the_synthetic_set(
    fts_db: IndexDb, ko_eval: Any
) -> None:
    # K3의 가짜 쪽: 키워드 질문 12개와 띄어쓰기 없는 질문 2개는 전문 검색만으로 1위다.
    # 연구 3.5절의 fts-bigram 실측(R@1 0.811)과 같은 값이 나온다.
    names = list(ko_eval.docs)
    fts, kb_id = await ready(
        fts_db, {position + 1: text for position, text in enumerate(ko_eval.docs.values())}
    )

    firsts: dict[str, list[bool]] = {}
    for question, expected, kind in ko_eval.questions:
        hits = await fts.search(kb_id, question, 50)
        top = names[hits[0].unit_no - 1] if hits else None
        firsts.setdefault(kind, []).append(top == expected)

    assert len(firsts["kw"]) == 12 and all(firsts["kw"])
    assert len(firsts["nsp"]) == 2 and all(firsts["nsp"])
    overall = [hit for values in firsts.values() for hit in values]
    assert sum(overall) / len(overall) == pytest.approx(0.811, abs=0.001)
