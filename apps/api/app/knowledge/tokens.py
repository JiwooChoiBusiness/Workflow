import math
import re
from collections.abc import Iterable

from app.llm.token_estimate import CHARS_PER_TOKEN

# 설계 6.4. 한국어 730자가 임베딩 토크나이저로 521토큰(약 1.4자/토큰)이었다. 정확한 값은 네이티브
# countTokens가 있어야 하므로(4-1 뒤) 그때까지는 추정이 실제보다 크게 나오도록 10%를 더한다.
CJK_CHARS_PER_TOKEN = 1.4
OTHER_CHARS_PER_TOKEN = CHARS_PER_TOKEN
MARGIN = 1.1

# (첫 코드 포인트, 끝 코드 포인트). 한글 음절·자모(호환 자모 포함), 그리고 한자·가나.
# fts_index의 bigram 구간도 이 범위를 쓴다.
HANGUL_SPANS = ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7A3))
HAN_KANA_SPANS = (
    (0x3040, 0x30FF),
    (0x31F0, 0x31FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFF66, 0xFF9F),
    (0x20000, 0x3134F),
)


def char_ranges(spans: Iterable[tuple[int, int]]) -> str:
    # 정규식 문자 집합([...]) 안에 넣을 조각. 이 범위들에는 정규식 특수 문자가 없다.
    return "".join(f"{chr(start)}-{chr(end)}" for start, end in spans)


HANGUL_RANGES = char_ranges(HANGUL_SPANS)
HAN_KANA_RANGES = char_ranges(HAN_KANA_SPANS)
_CJK_RUN = re.compile(f"[{HANGUL_RANGES}{HAN_KANA_RANGES}]+")
_FIRST_CJK = HANGUL_SPANS[0][0]
_CJK_WEIGHT = 1 / CJK_CHARS_PER_TOKEN
_OTHER_WEIGHT = 1 / OTHER_CHARS_PER_TOKEN


def token_weight(text: str) -> float:
    # 여유를 붙이기 전 값이라 이어 붙인 글의 weight는 조각 weight의 합이다(분할이 이것으로 더한다).
    cjk = sum(len(run) for run in _CJK_RUN.findall(text))
    return cjk * _CJK_WEIGHT + (len(text) - cjk) * _OTHER_WEIGHT


def tokens_from_weight(weight: float) -> int:
    if weight <= 0:
        return 0
    # 10 * 1.1이 11.000000000000002가 되어 올림이 1 커지는 것을 막는다.
    return math.ceil(round(weight * MARGIN, 6))


def estimate_tokens(text: str) -> int:
    return tokens_from_weight(token_weight(text))


def char_weight(char: str) -> float:
    if ord(char) < _FIRST_CJK:
        return _OTHER_WEIGHT
    return _CJK_WEIGHT if _CJK_RUN.match(char) else _OTHER_WEIGHT
