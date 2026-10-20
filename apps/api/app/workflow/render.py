# pyright: strict
"""텍스트 렌더러(설계 3.3, 9.3).

Jinja2 ImmutableSandboxedEnvironment 두 벌을 기동 때 한 번 만들어 둔다.
- lenient: 2단계 노드의 텍스트 필드. 없는 변수는 빈 문자열로 렌더하고 이름을 돌려준다
  (missing_variable 경고). 전역은 range(1만 개)·dict·len뿐이고 tojson·fromjson·regex_replace·
  truncate_tokens 필터를 더한다. 반복 10만 회, 출력 1MB를 넘으면 template_limit이다.
- strict: 1단계 과업 지시(render_instruction). 없는 변수는 오류이고 필터·전역은 Jinja2 기본
  그대로다.
환경은 렌더 중 상태를 바꾸지 않으므로 asyncio.to_thread에서 같이 써도 된다. 렌더 한 번의 반복 횟수와
빠진 이름은 ContextVar로 렌더마다 따로 센다.
"""

import contextvars
import json
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, cast

from jinja2 import ChainableUndefined, StrictUndefined, Template, TemplateSyntaxError, nodes
from jinja2.exceptions import SecurityError
from jinja2.ext import Extension
from jinja2.runtime import Context
from jinja2.sandbox import ImmutableSandboxedEnvironment

from app.workflow.variables import ScopeView, is_file, scope_name, scope_values

MAX_OUTPUT_BYTES = 1024 * 1024
MAX_RANGE = 10_000
MAX_LOOP_ITERATIONS = 100_000
MAX_REGEX_PATTERN_CHARS = 512
MAX_REGEX_INPUT_BYTES = 1024 * 1024
# 정수 거듭제곱은 계산만으로 루프를 오래 붙잡을 수 있어 결과 비트 수를 막는다.
MAX_POWER_BITS = 100_000
CHARS_PER_TOKEN = 3.5
# 템플릿 안에서 선언 없이 쓰는 Jinja2 이름. 변수 참조로 보지 않는다.
_IMPLICIT_NAMES = frozenset({"loop", "caller", "varargs", "kwargs", "self", "super"})
_NAME_NODES = (
    nodes.Name,
    nodes.Macro,
    nodes.Import,
    nodes.FromImport,
    nodes.Getattr,
    nodes.Getitem,
)


class TemplateError(Exception):
    """code는 template_syntax, template_forbidden, template_limit, template_error 중 하나."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    # 컨텍스트에 없어 빈 문자열로 렌더한 이름. "llm_1.nope"처럼 scope를 붙인다.
    missing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateNames:
    # 템플릿이 컨텍스트에서 읽는 최상위 이름(전역과 템플릿 안에서 만든 이름 제외).
    names: frozenset[str]
    # 그 이름 바로 아래에서 읽는 속성. {{ a.b }}와 {{ a['b'] }} 모두 ("a", "b")다.
    attrs: frozenset[tuple[str, str]]


@dataclass(slots=True)
class _RenderState:
    loops: int = 0
    missing: list[str] = field(default_factory=list[str])


_state: contextvars.ContextVar[_RenderState | None] = contextvars.ContextVar(
    "workflow_render_state", default=None
)


def _limit(message: str) -> TemplateError:
    return TemplateError("template_limit", message)


def _syntax_error(exc: TemplateSyntaxError) -> TemplateError:
    return TemplateError("template_syntax", f"템플릿 문법 오류({exc.lineno}행): {exc.message}")


def _utf8_len(text: str) -> int:
    return len(text) if text.isascii() else len(text.encode("utf-8"))


class _Missing(ChainableUndefined):
    __slots__ = ()

    def __str__(self) -> str:
        # 샌드박스가 막은 속성 접근도 Undefined로 오는데, 빈칸으로 넘기면 거부가 드러나지 않는다.
        if self._undefined_exception is SecurityError:
            self._fail_with_undefined_error()
        state = _state.get()
        if state is not None:
            owner = self._undefined_obj
            name = self._undefined_name or "?"
            state.missing.append(
                f"{scope_name(owner)}.{name}" if isinstance(owner, ScopeView) else name
            )
        return ""


class _LoopGuard(Extension):
    def guard(self, iterable: Any) -> Iterator[Any]:
        state = _state.get()
        for item in iterable:
            if state is not None:
                state.loops += 1
                if state.loops > MAX_LOOP_ITERATIONS:
                    raise _limit(f"반복이 {MAX_LOOP_ITERATIONS}회를 넘는다")
            yield item


def _guard_loops(tree: nodes.Template) -> None:
    # {% for %}에는 훅이 없어 반복 대상을 _LoopGuard.guard 호출로 감싼다. ExtensionAttribute는
    # 컨텍스트 이름을 거치지 않으므로 템플릿이 같은 이름을 만들어도 가로챌 수 없다.
    for loop in tree.find_all(nodes.For):
        loop.iter = nodes.Call(
            nodes.ExtensionAttribute(_LoopGuard.identifier, "guard", lineno=loop.lineno),
            [loop.iter],
            [],
            None,
            None,
            lineno=loop.lineno,
        )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class _LenientEnvironment(ImmutableSandboxedEnvironment):
    intercepted_binops = frozenset({"*", "**"})

    def call_binop(self, context: Context, operator: str, left: Any, right: Any) -> Any:
        if operator == "**" and _is_int(left) and _is_int(right):
            base, exponent = cast(int, left), cast(int, right)
            if (
                abs(base) > 1
                and exponent > 0
                and abs(base).bit_length() * exponent > MAX_POWER_BITS
            ):
                raise _limit("거듭제곱 결과가 너무 크다")
        if operator == "*":
            for sequence, count in ((left, right), (right, left)):
                if isinstance(sequence, str | list | tuple) and _is_int(count):
                    if len(cast(str, sequence)) * cast(int, count) > MAX_OUTPUT_BYTES:
                        raise _limit("반복해 만든 값이 1MB를 넘는다")
        return super().call_binop(context, operator, left, right)


def _json_default(value: object) -> Any:
    if isinstance(value, ScopeView):
        return scope_values(value)
    return str(value)


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _finalize(value: Any) -> Any:
    # 템플릿 안에서는 모든 값이 문자열이다(3.2). null은 빈칸, 객체·배열은 compact JSON,
    # 파일은 파일명이다.
    if value is None:
        return ""
    if isinstance(value, ScopeView):
        return _compact(scope_values(value))
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        return mapping["filename"] if is_file(mapping) else _compact(mapping)
    if isinstance(value, list | tuple):
        return _compact(value)
    return value


def _tojson(value: Any, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, default=_json_default)


def _fromjson(value: Any) -> Any:
    text = str(value)
    if _utf8_len(text) > MAX_OUTPUT_BYTES:
        raise _limit("fromjson 입력이 1MB를 넘는다")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise TemplateError("template_error", f"fromjson: JSON이 아니다({exc})") from None


def _regex_replace(value: Any, pattern: Any, replacement: Any = "", count: int = 0) -> str:
    # 표준 re라 백트래킹 상한이 없다. 길이·입력 크기만 막는다(9.5).
    pattern_text, text = str(pattern), str(value)
    if len(pattern_text) > MAX_REGEX_PATTERN_CHARS:
        raise _limit(f"정규식은 {MAX_REGEX_PATTERN_CHARS}자까지다")
    if _utf8_len(text) > MAX_REGEX_INPUT_BYTES:
        raise _limit("regex_replace 입력이 1MB를 넘는다")
    try:
        return re.sub(pattern_text, str(replacement), text, count=int(count))
    except re.error as exc:
        raise TemplateError("template_error", f"regex_replace: 정규식 오류({exc})") from None


def _truncate_tokens(value: Any, tokens: int) -> str:
    return str(value)[: max(0, int(int(tokens) * CHARS_PER_TOKEN))]


def _range(*args: int) -> range:
    numbers = range(*args)
    if len(numbers) > MAX_RANGE:
        raise _limit(f"range는 {MAX_RANGE}개까지다")
    return numbers


def _make_lenient() -> ImmutableSandboxedEnvironment:
    env = _LenientEnvironment(
        autoescape=False, undefined=_Missing, finalize=_finalize, extensions=[_LoopGuard]
    )
    # Jinja2가 globals·filters의 값 타입을 적지 않아 strict 검사가 모르는 타입으로 본다.
    names = cast(dict[str, Any], env.globals)  # pyright: ignore[reportUnknownMemberType]
    names.clear()
    names.update({"range": _range, "dict": dict, "len": len})
    cast(dict[str, Any], env.filters).update(  # pyright: ignore[reportUnknownMemberType]
        {
            "tojson": _tojson,
            "fromjson": _fromjson,
            "regex_replace": _regex_replace,
            "truncate_tokens": _truncate_tokens,
        }
    )
    return env


def _make_strict() -> ImmutableSandboxedEnvironment:
    return ImmutableSandboxedEnvironment(undefined=StrictUndefined, autoescape=False)


@dataclass(frozen=True, slots=True)
class _Environments:
    strict: ImmutableSandboxedEnvironment
    lenient: ImmutableSandboxedEnvironment


_lock = threading.Lock()
_environments: _Environments | None = None


def _get() -> _Environments:
    global _environments
    if _environments is None:
        with _lock:
            if _environments is None:
                _environments = _Environments(strict=_make_strict(), lenient=_make_lenient())
    return _environments


def setup() -> None:
    """기동 때 한 번 부른다(app/main.py). 부르지 않은 경로(CLI, 테스트)는 처음 쓸 때 만들어진다."""
    _get()


def strict_environment() -> ImmutableSandboxedEnvironment:
    return _get().strict


def lenient_environment() -> ImmutableSandboxedEnvironment:
    return _get().lenient


# 다른 템플릿을 읽는 태그. 로더가 없어 지금도 실패하지만 로더가 생겨도 열리지 않게 먼저
# 거부한다(9.3).
_LOADING_NODES = (nodes.Include, nodes.Import, nodes.FromImport, nodes.Extends)


@lru_cache(maxsize=256)
def _compiled(source: str) -> Template:
    # iteration 1,000회처럼 같은 템플릿을 거듭 렌더하므로 컴파일 결과를 원문 기준으로 둔다.
    env = _get().lenient
    try:
        tree = env.parse(source)
        if any(True for _ in tree.find_all(_LOADING_NODES)):
            raise TemplateError("template_forbidden", "include·import·extends는 쓸 수 없다")
        _guard_loops(tree)
        tree.set_environment(env)
        return env.from_string(tree)
    except TemplateSyntaxError as exc:
        raise _syntax_error(exc) from None


def render(template: str, context: Mapping[str, Any]) -> Rendered:
    """lenient 환경으로 렌더한다. 실패는 모두 TemplateError다."""
    if not template:
        return Rendered("")
    compiled = _compiled(template)
    state = _RenderState()
    token = _state.set(state)
    parts: list[str] = []
    try:
        size = 0
        for chunk in compiled.generate(dict(context)):
            size += _utf8_len(chunk)
            if size > MAX_OUTPUT_BYTES:
                raise _limit("렌더 결과가 1MB를 넘는다")
            parts.append(chunk)
    except TemplateError:
        raise
    except SecurityError as exc:
        raise TemplateError("template_forbidden", f"허용되지 않은 접근이다: {exc}") from None
    except Exception as exc:
        raise TemplateError(
            "template_error", f"템플릿을 렌더하지 못했다: {type(exc).__name__}: {exc}"
        ) from None
    finally:
        _state.reset(token)
    return Rendered("".join(parts), tuple(dict.fromkeys(state.missing)))


@lru_cache(maxsize=1024)
def template_names(source: str) -> TemplateNames:
    """검증용 정적 분석. 문법 오류면 TemplateError(template_syntax).

    jinja2.meta.find_undeclared_variables는 코드 생성기를 한 번 돌려 노드 100개 그래프에서 너무
    느리다. 여기서는 템플릿 어디선가 만든 이름(set, for 대상, 매크로 인자 등)을 모두 선언된 것으로
    본다. 만들기 전에 읽는 드문 경우를 놓치지만 그때는 렌더가 missing_variable로 알린다.
    """
    env = _get().lenient
    try:
        tree = env.parse(source)
    except TemplateSyntaxError as exc:
        raise _syntax_error(exc) from None
    declared = set(_IMPLICIT_NAMES) | set(cast(dict[str, Any], env.globals))  # pyright: ignore[reportUnknownMemberType]
    loaded: set[str] = set()
    accessed: set[tuple[str, str]] = set()
    # 트리를 한 번만 돈다. 노드 100개 그래프에서는 이 순회가 파싱 다음으로 큰 몫이다.
    for item in tree.find_all(_NAME_NODES):
        if isinstance(item, nodes.Name):
            (loaded if item.ctx == "load" else declared).add(item.name)
        elif isinstance(item, nodes.Macro):
            declared.add(item.name)
        elif isinstance(item, nodes.Import):
            declared.add(item.target)
        elif isinstance(item, nodes.FromImport):
            declared.update(name[1] if isinstance(name, tuple) else name for name in item.names)
        elif isinstance(item.node, nodes.Name):
            if isinstance(item, nodes.Getattr):
                accessed.add((item.node.name, item.attr))
            elif isinstance(item.arg, nodes.Const) and isinstance(item.arg.value, str):
                accessed.add((item.node.name, item.arg.value))
    names = frozenset(loaded - declared)
    return TemplateNames(names, frozenset(pair for pair in accessed if pair[0] in names))


@dataclass(frozen=True, slots=True)
class Segment:
    """answer 템플릿의 조각(3.8). 글자 조각은 text만, 변수 조각은 expr·scope가 있다."""

    text: str = ""
    # 변수 조각만 렌더하는 템플릿("{{ llm_1.text }}"). render()에 그대로 넘긴다.
    expr: str | None = None
    scope: str | None = None
    # scope 바로 아래 이름. {{ llm_1 }}처럼 없으면 None이다.
    attr: str | None = None
    # attr 아래로 더 내려가는지({{ http_1.json.items }}).
    deep: bool = False


def _selector(node: nodes.Node) -> tuple[list[str], str] | None:
    """이름·속성·상수 첨자만으로 된 식이면 (경로, 같은 뜻의 식). 아니면 None.

    a.b와 a["b"]는 dict 값에서 결과가 다를 수 있어(a.items는 메서드) 원래 꼴을 그대로 살린다.
    """
    if isinstance(node, nodes.Name):
        return ([node.name], node.name) if node.ctx == "load" else None
    if isinstance(node, nodes.Getattr):
        found = _selector(node.node)
        return None if found is None else ([*found[0], node.attr], f"{found[1]}.{node.attr}")
    if isinstance(node, nodes.Getitem) and isinstance(node.arg, nodes.Const):
        value: object = node.arg.value
        if isinstance(value, str):
            item = json.dumps(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            item = str(value)
        else:
            return None
        found = _selector(node.node)
        return None if found is None else ([*found[0], str(value)], f"{found[1]}[{item}]")
    return None


@lru_cache(maxsize=256)
def stream_segments(source: str) -> tuple[Segment, ...] | None:
    """answer 템플릿을 글자 조각과 변수 조각으로 나눈다.

    글자와 {{ a.b }} 꼴의 식만 있을 때만 나눈다. 블록 문법·필터·연산이 있거나 문법 오류면 None이고,
    그 answer는 미리 흘리지 않는다. 조각을 차례로 렌더해 이으면 템플릿 전체를 렌더한 것과 같다.
    """
    if not source:
        return ()
    env = _get().lenient
    try:
        tree = env.parse(source)
    except TemplateSyntaxError:
        return None
    segments: list[Segment] = []
    for item in tree.body:
        if not isinstance(item, nodes.Output):
            return None
        for child in item.nodes:
            if isinstance(child, nodes.TemplateData):
                if child.data:
                    segments.append(Segment(text=child.data))
                continue
            found = _selector(child)
            if found is None:
                return None
            path, expr = found
            segments.append(
                Segment(
                    expr="{{ " + expr + " }}",
                    scope=path[0],
                    attr=path[1] if len(path) > 1 else None,
                    deep=len(path) > 2,
                )
            )
    return tuple(segments)
