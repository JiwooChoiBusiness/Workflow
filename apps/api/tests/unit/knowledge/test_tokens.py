import math

from app.knowledge.tokens import char_weight, estimate_tokens, token_weight, tokens_from_weight


def test_korean_is_counted_at_1_4_chars_per_token_with_ten_percent_margin() -> None:
    # 14자 / 1.4 = 10토큰, 여유 10%를 더해 11. 부동소수 오차로 12가 되면 안 된다.
    assert estimate_tokens("가" * 14) == 11


def test_other_text_is_counted_at_3_5_chars_per_token() -> None:
    assert estimate_tokens("a" * 35) == 11
    # 한글 14자(10) + 공백과 영문 35자(10) = 20, 여유를 더해 22.
    assert estimate_tokens("가" * 14 + " " + "a" * 34) == 22


def test_han_and_kana_count_like_hangul() -> None:
    assert token_weight("東京タワー") == token_weight("가나다라마")


def test_estimate_errs_on_the_large_side_of_the_measured_count() -> None:
    # countTokens 실측: 한국어 730자가 521토큰(설계 6.4). 추정은 그보다 크게 나와야 한다.
    assert estimate_tokens("한" * 730) >= 521


def test_weights_add_up_across_joined_pieces() -> None:
    pieces = ["회의록을 정리했다", "\n\n", "Meeting notes 2026"]

    assert math.isclose(token_weight("".join(pieces)), sum(token_weight(p) for p in pieces))


def test_empty_text_has_no_tokens() -> None:
    assert estimate_tokens("") == 0
    assert tokens_from_weight(0.0) == 0


def test_char_weight_matches_the_text_rule() -> None:
    assert char_weight("가") == token_weight("가")
    assert char_weight("a") == token_weight("a")
    assert char_weight("ㅋ") == token_weight("ㅋ")
