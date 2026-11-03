import re
import unicodedata
from dataclasses import dataclass

from app.knowledge.tokens import char_ranges


@dataclass(frozen=True)
class CleaningRules:
    remove_extra_spaces: bool = True
    remove_urls_emails: bool = False


# 탭·줄바꿈 계열은 공백 규칙이 다루므로 남기고, 나머지 C0·C1 제어 문자와 BOM을 지운다.
_CONTROL = re.compile(
    "[" + char_ranges(((0x00, 0x08), (0x0E, 0x1F), (0x7F, 0x9F), (0xFEFF, 0xFEFF))) + "]"
)
_NEWLINE = re.compile(r"\r\n?")
# 탭·세로 탭·폼 피드와 NBSP 같은 특수 공백.
_SPECIAL_SPACE = re.compile(
    "["
    + char_ranges(
        (
            (0x09, 0x09),
            (0x0B, 0x0C),
            (0xA0, 0xA0),
            (0x1680, 0x1680),
            (0x180E, 0x180E),
            (0x2000, 0x200A),
            (0x202F, 0x202F),
            (0x205F, 0x205F),
            (0x3000, 0x3000),
        )
    )
    + "]"
)
_SPACES = re.compile(r" {2,}")
# 줄 끝·줄 앞 공백이 남으면 빈 줄이 "\n \n"이 되어 문단 구분자 "\n\n"에 걸리지 않는다.
_SPACE_AROUND_NEWLINE = re.compile(r" *\n *")
_BLANK_LINES = re.compile(r"\n{3,}")
# 끝의 문장 부호는 URL에 넣지 않는다("(https://a.b/c)." 같은 경우).
_URL = re.compile(r"(?:(?:https?|ftp)://|www\.)[^\s<>\"']*[^\s<>\"'.,;:!?)\]}]")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def normalize(text: str) -> str:
    # 색인·질의 양쪽에 거는 공통 단계(설계 6.3 "항상").
    text = unicodedata.normalize("NFC", text)
    text = _NEWLINE.sub("\n", text)
    return _CONTROL.sub("", text)


def clean_text(text: str, rules: CleaningRules | None = None) -> str:
    rules = rules or CleaningRules()
    text = normalize(text)
    if rules.remove_urls_emails:
        text = _EMAIL.sub("", _URL.sub("", text))
    if rules.remove_extra_spaces:
        text = _SPECIAL_SPACE.sub(" ", text)
        text = _SPACES.sub(" ", text)
        text = _SPACE_AROUND_NEWLINE.sub("\n", text)
        text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()
