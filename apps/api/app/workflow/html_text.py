# pyright: strict
"""HTML을 읽을 글로 바꾼다. document_extractor의 html 파일과 web_fetch 도구가 쓴다.

표준 라이브러리 html.parser만 쓴다. script·style처럼 보이지 않는 요소는 버리고 블록 요소 경계에서
줄을 바꾼 뒤 공백을 줄인다. <title>은 따로 돌려준다. 깨진 HTML도 예외 없이 읽은 만큼 돌려준다.
"""

import re
from dataclasses import dataclass
from html.parser import HTMLParser

_HIDDEN = frozenset(
    {"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "canvas", "head"}
)
_BLOCK = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tr",
        "ul",
    }
)
_SPACES = re.compile(r"[ \t\r\f\v ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class HtmlText:
    title: str
    text: str


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._hidden = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in _HIDDEN:
            self._hidden += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "hr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _HIDDEN:
            # 닫는 태그만 있는 깨진 문서에서 음수가 되면 이후 글이 모두 사라진다.
            self._hidden = max(0, self._hidden - 1)
        elif tag in _BLOCK:
            # li는 여는 태그에서 줄을 바꾸므로 닫을 때는 두지 않는다(항목 사이에 빈 줄이 생긴다).
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif self._hidden == 0:
            self.parts.append(data)


def _tidy(text: str) -> str:
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(source: str) -> HtmlText:
    parser = _Parser()
    parser.feed(source)
    parser.close()
    title = _SPACES.sub(" ", "".join(parser.title)).strip()
    return HtmlText(title=title, text=_tidy("".join(parser.parts)))
