# pyright: strict
"""변수 풀과 타입(설계 3.2).

풀은 런 하나의 노드 출력, sys·env·conv 값을 scope와 이름으로 담는다. 노드 출력은 넣은 뒤 바꾸지
않고 대화 변수(conv)와 돌고 있는 loop의 반복 변수("<loop id>" 자리)만 variable_assigner가 바꾼다.
loop가 끝나면 반복 변수 자리는 loop 노드의 최종 출력으로 바뀐다. iteration 하위 노드는 풀을
복제하지 않고 "<node_id>@<index>"를 scope로 써서 저장하며, PoolView가 현재 프레임의 index로 이름을
푼다.
크기 상한은 직렬화하지 않고 값을 넣을 때 잰 크기를 누적해 판정한다.
"""

import json
import math
import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel

from app.schemas.workflow import VarType, WorkflowKind

ARRAY_ITEM: dict[VarType, VarType] = {
    "array[string]": "string",
    "array[number]": "number",
    "array[boolean]": "boolean",
    "array[object]": "object",
    "array[file]": "file",
    "array[any]": "any",
}
SCALAR_TYPES: frozenset[VarType] = frozenset({"string", "number", "boolean"})

MAX_STRING_BYTES = 1024 * 1024
MAX_OBJECT_BYTES = 1024 * 1024
MAX_OBJECT_DEPTH = 20
MAX_ARRAY_ITEMS = 10_000
POOL_MAX_BYTES = 16 * 1024 * 1024
PREVIEW_BYTES = 4096

SYS = "sys"
ENV = "env"
CONV = "conv"
RESERVED_SCOPES = frozenset({SYS, ENV, CONV})

_WORKFLOW_SYS: dict[str, VarType] = {
    "workflow_id": "string",
    "run_id": "string",
    "kind": "string",
    "now": "string",
}
_CHATFLOW_SYS: dict[str, VarType] = {
    **_WORKFLOW_SYS,
    "query": "string",
    "files": "array[file]",
    "conversation_id": "string",
    "message_id": "string",
    "user": "string",
    "turn": "number",
}

MASK = "***"
# 이보다 짧은 비밀값은 일반 글자와 겹치기 쉬워 치환하지 않고 경고만 한다(9.4).
MIN_SECRET_CHARS = 8


def sys_variables(kind: WorkflowKind) -> Mapping[str, VarType]:
    return _CHATFLOW_SYS if kind == "chatflow" else _WORKFLOW_SYS


class VariableError(Exception):
    """노드 오류로 끝나는 값 문제. code는 type_mismatch 또는 variable_limit."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class PoolLimitError(Exception):
    """풀 전체가 상한을 넘었다. 노드가 아니라 런이 variable_pool_limit으로 실패한다."""


class FileRef(BaseModel):
    file_id: str
    filename: str
    mime: str
    size_bytes: int
    storage_key: str
    source: Literal["wf_file", "session_file"]


_FILE_KEYS = frozenset(FileRef.model_fields)


def is_file(value: object) -> bool:
    return isinstance(value, dict) and _FILE_KEYS <= cast(dict[str, Any], value).keys()


class SelectorError(ValueError):
    pass


_SELECTOR = re.compile(
    r"(?P<scope>[a-z][a-z0-9_]{0,31})\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P<path>(?:\.[^.\[\]\s]+|\[\d+\])*)"
)
_PATH_STEP = re.compile(r"\.([^.\[\]\s]+)|\[(\d+)\]")


@dataclass(frozen=True, slots=True)
class Selector:
    scope: str
    name: str
    path: tuple[str | int, ...] = ()

    def __str__(self) -> str:
        steps = "".join(f"[{step}]" if isinstance(step, int) else f".{step}" for step in self.path)
        return f"{self.scope}.{self.name}{steps}"


def parse_selector(text: str) -> Selector:
    """scope는 노드 id 또는 sys·env·conv이고 이름 뒤에 .key·[0]이 이어질 수 있다."""
    match = _SELECTOR.fullmatch(text.strip())
    if match is None:
        raise SelectorError(f"변수 선택자 형식이 아니다: {text!r}")
    path: list[str | int] = []
    for key, index in _PATH_STEP.findall(match.group("path")):
        path.append(int(index) if index else key)
    return Selector(match.group("scope"), match.group("name"), tuple(path))


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return not isinstance(value, float) or math.isfinite(value)


def type_matches(value: object, expected: VarType) -> bool:
    """null은 모든 타입에 대입된다. bool은 number가 아니다."""
    if value is None or expected == "any":
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return _is_number(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "file":
        return is_file(value)
    if not isinstance(value, list):
        return False
    item_type = ARRAY_ITEM[expected]
    return item_type == "any" or all(
        type_matches(item, item_type) for item in cast(list[Any], value)
    )


_NUMBER_TEXT = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


def parse_number(text: str) -> int | float | None:
    stripped = text.strip()
    if not _NUMBER_TEXT.fullmatch(stripped):
        return None
    if re.fullmatch(r"[+-]?\d+", stripped):
        return int(stripped)
    number = float(stripped)
    return number if math.isfinite(number) else None


def describe(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if _is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    if is_file(value):
        return "file"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def coerce(value: Any, expected: VarType | None) -> Any:
    """구조화 필드의 런타임 변환(3.2).

    string←number/boolean, number←숫자 문자열, array[any]←단일값만 받고 나머지는 type_mismatch다.
    """
    if expected is None or type_matches(value, expected):
        return value
    if expected == "string" and (isinstance(value, bool) or _is_number(value)):
        return str(value)
    if expected == "number" and isinstance(value, str):
        number = parse_number(value)
        if number is not None:
            return number
    if expected == "array[any]":
        return [value]
    raise VariableError("type_mismatch", f"{expected}이(가) 필요한데 {describe(value)} 값이 왔다")


Assignable = Literal["yes", "maybe", "no"]


def assignable(source: VarType | None, target: VarType | None) -> Assignable:
    """선언 타입끼리의 대입 가능성. 검증의 type_mismatch 경고가 쓴다."""
    if source is None or target is None or source == target or "any" in (source, target):
        return "yes"
    if target == "array[any]":
        return "yes"
    if target == "string" and source in ("number", "boolean"):
        return "yes"
    if target == "object" and source == "file":
        return "yes"
    if target == "number" and source == "string":
        return "maybe"
    if source == "array[any]" and target in ARRAY_ITEM:
        return "maybe"
    return "no"


def _utf8_len(text: str) -> int:
    return len(text) if text.isascii() else len(text.encode("utf-8"))


def _measure(value: Any, depth: int) -> int:
    if depth > MAX_OBJECT_DEPTH:
        raise VariableError("variable_limit", f"값의 깊이가 {MAX_OBJECT_DEPTH}을 넘는다")
    if value is None or isinstance(value, bool):
        return 5
    if isinstance(value, str):
        size = _utf8_len(value)
        if size > MAX_STRING_BYTES:
            raise VariableError("variable_limit", "문자열이 1MB를 넘는다")
        return size + 2
    if isinstance(value, int | float):
        return 8
    if isinstance(value, dict):
        items = cast(dict[Any, Any], value)
        size = 2 + sum(
            _utf8_len(str(key)) + 4 + _measure(item, depth + 1) for key, item in items.items()
        )
        if size > MAX_OBJECT_BYTES:
            raise VariableError("variable_limit", "객체가 1MB를 넘는다")
        return size
    if isinstance(value, list | tuple):
        sequence = cast(list[Any], value)
        if len(sequence) > MAX_ARRAY_ITEMS:
            raise VariableError("variable_limit", f"배열 원소가 {MAX_ARRAY_ITEMS}개를 넘는다")
        return 2 + sum(_measure(item, depth + 1) + 1 for item in sequence)
    return _utf8_len(str(value))


def measure(value: Any) -> int:
    """값 하나의 대략적인 직렬화 크기(바이트). 3.2의 상한을 넘으면 variable_limit."""
    return _measure(value, 0)


@dataclass(frozen=True, slots=True)
class Variable:
    value: Any
    type: VarType | None
    size: int


@dataclass(frozen=True, slots=True)
class Lookup:
    value: Any
    # scope와 이름(과 경로)이 풀에 있었는지. skipped·빈 scope는 값이 null이어도 찾은 것으로 본다.
    found: bool
    skipped: bool = False
    type: VarType | None = None


class VariablePool:
    def __init__(self, max_bytes: int = POOL_MAX_BYTES) -> None:
        self._scopes: dict[str, dict[str, Variable]] = {}
        # 건너뛴 노드와 출력 없이 끝난 노드(오류 분기). 둘 다 어떤 이름을 읽어도 null이다.
        self._skipped: set[str] = set()
        self._empty: set[str] = set()
        self._size = 0
        self._max = max_bytes

    @property
    def size(self) -> int:
        return self._size

    def _reserve(self, added: int, released: int = 0) -> None:
        total = self._size + added - released
        if total > self._max:
            raise PoolLimitError(
                f"변수 풀이 {self._max // (1024 * 1024)}MB를 넘는다({total}바이트)"
            )
        self._size = total

    def put(self, scope: str, name: str, value: Any, type_: VarType | None = None) -> None:
        """sys·env·conv 초기값처럼 하나씩 넣는다. 같은 이름이 있으면 바꾼다."""
        size = measure(value)
        old = self._scopes.get(scope, {}).get(name)
        self._reserve(size, old.size if old else 0)
        self._scopes.setdefault(scope, {})[name] = Variable(value, type_, size)

    def put_outputs(
        self,
        scope: str,
        values: Mapping[str, Any],
        types: Mapping[str, VarType | None] | None = None,
    ) -> None:
        """노드 출력을 한 번에 넣는다. 이미 출력이 있는 노드에는 다시 넣지 않는다."""
        if scope in self._scopes or scope in self._skipped:
            raise ValueError(f"{scope}의 출력은 이미 있다")
        entries = {
            name: Variable(value, (types or {}).get(name), measure(value))
            for name, value in values.items()
        }
        self._reserve(sum(entry.size for entry in entries.values()))
        self._scopes[scope] = entries

    def assign(self, name: str, value: Any, scope: str = CONV) -> None:
        """대화 변수(또는 loop의 반복 변수)를 바꾼다. 선언 타입은 처음 넣을 때의 것을 유지한다."""
        old = self._scopes.get(scope, {}).get(name)
        self.put(scope, name, value, old.type if old else None)

    def drop(self, scope: str) -> None:
        """scope의 값을 모두 뺀다. loop가 끝나 반복 변수 자리를 최종 출력으로 바꿀 때 쓴다."""
        variables = self._scopes.pop(scope, None)
        if variables:
            self._size -= sum(variable.size for variable in variables.values())

    def mark_skipped(self, scope: str) -> None:
        self._skipped.add(scope)

    def mark_empty(self, scope: str) -> None:
        self._empty.add(scope)

    def is_skipped(self, scope: str) -> bool:
        return scope in self._skipped

    def scope(self, scope: str) -> Mapping[str, Variable] | None:
        return self._scopes.get(scope)

    def lookup(self, scope: str, name: str) -> Lookup:
        if scope in self._skipped:
            return Lookup(None, True, skipped=True)
        variables = self._scopes.get(scope)
        if variables is None:
            return Lookup(None, scope in self._empty)
        variable = variables.get(name)
        if variable is None:
            return Lookup(None, False)
        return Lookup(variable.value, True, type=variable.type)

    def dump(self, *, exclude: Collection[str] = ()) -> dict[str, Any]:
        """체크포인트용 평면 사전. 키는 "<scope>.<name>"이다."""
        return {
            f"{scope}.{name}": variable.value
            for scope, variables in self._scopes.items()
            if scope not in exclude
            for name, variable in variables.items()
        }

    def snapshot(self) -> "VariablePool":
        """다른 스레드에서 dump하려고 사전 구조만 얕게 복사한다. 값은 넣은 뒤 바꾸지 않는다."""
        copy = VariablePool(self._max)
        copy._scopes = {scope: dict(variables) for scope, variables in self._scopes.items()}
        copy._skipped = set(self._skipped)
        copy._empty = set(self._empty)
        copy._size = self._size
        return copy

    @property
    def skipped(self) -> frozenset[str]:
        return frozenset(self._skipped)

    @property
    def empty(self) -> frozenset[str]:
        return frozenset(self._empty)

    def restore(
        self,
        values: Mapping[str, Any],
        *,
        skipped: Iterable[str] = (),
        empty: Iterable[str] = (),
        types: Mapping[str, Mapping[str, VarType]] | None = None,
    ) -> None:
        """dump()의 결과를 다시 넣는다. scope에 '.'이 없으므로 첫 '.'에서 나눈다."""
        for key, value in values.items():
            scope, _, name = key.partition(".")
            self.put(scope, name, value, (types or {}).get(scope, {}).get(name))
        self._skipped.update(skipped)
        self._empty.update(empty)

    def scopes(self) -> Iterator[str]:
        yield from self._scopes
        yield from self._skipped
        yield from self._empty


class ScopeView:
    """템플릿 컨텍스트의 최상위 값. {{ llm_1.text }}의 llm_1이 이것이다.

    dict를 그대로 넘기면 출력 이름이 items·values·keys일 때 dict 메서드가 먼저 잡히므로 공개
    메서드를 두지 않는다. 건너뛴 노드는 어떤 이름을 읽어도 null이다(3.4).
    """

    __slots__ = ("_name", "_values", "_nullable")

    def __init__(self, name: str, values: Mapping[str, Any], *, nullable: bool = False) -> None:
        self._name = name
        self._values = values
        self._nullable = nullable

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __getitem__(self, name: str) -> Any:
        if name in self._values:
            return self._values[name]
        if self._nullable:
            return None
        raise KeyError(name)

    def __contains__(self, name: object) -> bool:
        return name in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"ScopeView({self._name!r})"


def scope_name(view: ScopeView) -> str:
    return view._name  # pyright: ignore[reportPrivateUsage]


def scope_values(view: ScopeView) -> dict[str, Any]:
    return dict(view._values)  # pyright: ignore[reportPrivateUsage]


def _walk(value: Any, path: tuple[str | int, ...]) -> tuple[Any, bool]:
    for step in path:
        if isinstance(step, int):
            if not isinstance(value, list) or step >= len(cast(list[Any], value)):
                return None, False
            value = cast(list[Any], value)[step]
        else:
            if not isinstance(value, dict) or step not in value:
                return None, False
            value = cast(dict[str, Any], value)[step]
    return value, True


class PoolView:
    """노드가 보는 읽기 전용 풀. 현재 프레임에 속한 노드 id는 "<id>@<index>"로 푼다."""

    def __init__(
        self, pool: VariablePool, *, inner: Collection[str] = (), index: int | None = None
    ) -> None:
        self._pool = pool
        self._inner = frozenset(inner) if index is not None else frozenset[str]()
        self._index = index

    def scope_key(self, scope: str) -> str:
        if scope in self._inner:
            return f"{scope}@{self._index}"
        return scope

    def is_skipped(self, scope: str) -> bool:
        return self._pool.is_skipped(self.scope_key(scope))

    def lookup(self, selector: Selector) -> Lookup:
        found = self._pool.lookup(self.scope_key(selector.scope), selector.name)
        if not found.found or found.skipped or not selector.path:
            return found
        value, ok = _walk(found.value, selector.path)
        return Lookup(value, ok, type=None)

    def template_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {}
        for key in self._pool.scopes():
            scope, _, index = key.partition("@")
            if index and (scope not in self._inner or index != str(self._index)):
                continue
            variables = self._pool.scope(key)
            values = {name: var.value for name, var in (variables or {}).items()}
            nullable = self._pool.is_skipped(key) or variables is None
            context[scope] = ScopeView(scope, values, nullable=nullable)
        return context


class Masker:
    """비밀 env 값을 ***로 바꾼다. 이벤트·노드 런·체크포인트·오류 메시지에 쓰기 전에 거친다(9.4)."""

    def __init__(self, secrets: Iterable[object] = ()) -> None:
        values = {secret for secret in secrets if isinstance(secret, str) and secret}
        # 긴 값부터 바꿔야 한 값이 다른 값의 일부일 때 조각이 남지 않는다.
        self.secrets = tuple(
            sorted((s for s in values if len(s) >= MIN_SECRET_CHARS), key=len, reverse=True)
        )
        self.short = tuple(sorted(s for s in values if len(s) < MIN_SECRET_CHARS))

    def text(self, value: str) -> str:
        for secret in self.secrets:
            if secret in value:
                value = value.replace(secret, MASK)
        return value

    def mask(self, value: Any) -> Any:
        if not self.secrets:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            items = cast(dict[Any, Any], value)
            return {key: self.mask(item) for key, item in items.items()}
        if isinstance(value, list | tuple):
            return [self.mask(item) for item in cast(list[Any], value)]
        return value


class StreamMasker:
    """델타로 나뉘어 흐르는 글의 비밀값을 가린다(9.4).

    조각마다 Masker를 거치면 조각 경계에 걸친 비밀값이 두 조각으로 나뉘어 그대로 나간다. 끝이
    비밀값의 앞부분과 겹치는 만큼은 내보내지 않고 다음 조각과 이어서 보며, 스트림이 끝나면 flush로
    남은 글을 낸다.
    """

    def __init__(self, masker: Masker) -> None:
        self._masker = masker
        self._held = ""

    def feed(self, text: str) -> str:
        secrets = self._masker.secrets
        if not secrets:
            return text
        joined = self._masker.text(self._held + text)
        keep = 0
        for secret in secrets:
            for size in range(min(len(secret) - 1, len(joined)), keep, -1):
                if joined.endswith(secret[:size]):
                    keep = size
                    break
        cut = len(joined) - keep
        self._held = joined[cut:]
        return joined[:cut]

    def flush(self) -> str:
        held, self._held = self._held, ""
        return held


def _json_default(value: object) -> str:
    return str(value)


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _cut(text: str, limit: int) -> str:
    data = text.encode("utf-8")[: max(limit, 0)]
    return data.decode("utf-8", errors="ignore")


def preview(values: Mapping[str, Any], limit: int = PREVIEW_BYTES) -> tuple[dict[str, Any], bool]:
    """이벤트에 싣는 미리보기. 넘치면 값을 글자로 바꿔 자르고 두 번째 값이 True다(3.9)."""
    whole = _to_json(dict(values))
    if _utf8_len(whole) <= limit:
        return dict(values), False
    result: dict[str, Any] = {}
    room = limit - 2
    for name, value in values.items():
        room -= _utf8_len(name) + 6
        if room <= 0:
            break
        text = value if isinstance(value, str) else _to_json(value)
        size = _utf8_len(text)
        result[name] = value if size <= room else _cut(text, room)
        room -= min(size, room)
    return result, True
