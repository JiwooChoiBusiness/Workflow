import unicodedata

from app.knowledge.cleaning import CleaningRules, clean_text, normalize


def test_decomposed_hangul_is_composed() -> None:
    decomposed = unicodedata.normalize("NFD", "회의록")

    assert decomposed != "회의록"
    assert clean_text(decomposed) == "회의록"


def test_control_characters_and_bom_are_removed_and_newlines_unified() -> None:
    text = chr(0xFEFF) + "첫 줄\r\n둘째" + chr(0) + " 줄\r셋째 줄" + chr(0x85)

    assert normalize(text) == "첫 줄\n둘째 줄\n셋째 줄"


def test_extra_spaces_are_collapsed_like_dify() -> None:
    text = "가  나\t다" + chr(0xA0) + "라" + chr(0x3000) + "마\n\n\n\n바 \n \n사"

    assert clean_text(text) == "가 나 다 라 마\n\n바\n\n사"


def test_extra_spaces_rule_can_be_turned_off() -> None:
    rules = CleaningRules(remove_extra_spaces=False)

    assert clean_text("가  나\n\n\n다", rules) == "가  나\n\n\n다"


def test_urls_and_emails_are_kept_by_default() -> None:
    text = "문의는 help@example.com 또는 https://example.com/faq 로"

    assert clean_text(text) == text


def test_urls_and_emails_are_removed_when_asked() -> None:
    rules = CleaningRules(remove_urls_emails=True)
    text = "문의: help@example.co.kr (https://example.com/a?b=1). 자세한 건 www.example.org 참고"

    assert clean_text(text, rules) == "문의: (). 자세한 건 참고"
