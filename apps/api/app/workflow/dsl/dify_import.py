# pyright: strict
"""Dify DSL 부분 가져오기(2단계 설계 5.2).

app.mode가 workflow·advanced-chat인 Dify DSL(app, workflow.graph.nodes[].data)을 우리 DSL 문서로
바꾼다. Dify 소스는 보지 않고 공개 문서와 내보낸 파일의 구조만 보고 맞췄다. Dify 버전마다 필드가
조금씩 달라 없는 필드는 기본값으로 읽는다.

- 1차 노드 13종은 같은 뜻의 우리 노드로 옮긴다. 옮기지 못한 옵션은 경고로 남기거나, 뜻이 바뀌면
  안 되는 것(조건 연산자 등)은 원래 값을 그대로 두어 검증 오류로 드러낸다.
- 그 밖의 노드(2차 노드, 지식 검색, 에이전트, 트리거, 자바스크립트 code)는 원래 data를 담은
  unsupported 노드로 두고 엣지는 그대로 잇는다.
- 노드 id는 우리 형식([a-z][a-z0-9_]{0,31})으로 바꾼다. 숫자 id는 n<id>다. {{#node.var#}}와
  value_selector는 {{ node.var }}·"node.var"로, sys.user_id 같은 시스템 변수는 우리 이름으로,
  env는 그대로, conversation은 conv로 바꾼다. iteration 안의 item·index는 Dify에서 iteration
  노드 이름으로 읽지만 우리는 iteration_start의 출력이다.
- 모델은 Gemini 폴백 목록의 첫 모델(model_id 없음)로 바꾸고 model_replaced 경고를 남긴다.
- Dify의 기본 프롬프트는 {{#...#}}만 변수라서 그 밖의 {{·{%·{#는 글자 그대로 남도록 이스케이프한다.
"""

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from app.schemas.agent_version import MAX_ENV_VALUE_CHARS
from app.schemas.workflow import (
    FAIL_HANDLE,
    OUT_HANDLE,
    DslWarning,
    VarType,
    WorkflowKind,
)
from app.workflow.dsl.schema import DSL_VERSION, DslDocument, DslError, validate_document
from app.workflow.graph import UNSUPPORTED_TYPE
from app.workflow.nodes.if_else import ELSE_HANDLE
from app.workflow.nodes.iteration import MAX_PARALLEL
from app.workflow.variables import RESERVED_SCOPES

MODES: dict[str, WorkflowKind] = {"workflow": "workflow", "advanced-chat": "chatflow"}
# 우리 2차 노드(M19)에 같은 것이 있어 나중에 옮길 노드. 지금은 unsupported다.
LATER_TYPES = frozenset(
    {"loop", "loop-start", "loop-end", "list-operator", "document-extractor", "tool", "human-input"}
)
SYS_NAMES = {
    "query": "query",
    "files": "files",
    "conversation_id": "conversation_id",
    "user_id": "user",
    "dialogue_count": "turn",
    "app_id": "workflow_id",
    "workflow_id": "workflow_id",
    "workflow_run_id": "run_id",
}
START_TYPES = {
    "text-input": "string",
    "paragraph": "paragraph",
    "select": "select",
    "number": "number",
    "checkbox": "boolean",
    "file": "file",
    "file-list": "file_list",
}
VALUE_TYPES: dict[str, VarType] = {
    "string": "string",
    "secret": "string",
    "number": "number",
    "integer": "number",
    "boolean": "boolean",
    "bool": "boolean",
    "object": "object",
    "file": "file",
    "array[string]": "array[string]",
    "array[number]": "array[number]",
    "array[boolean]": "array[boolean]",
    "array[bool]": "array[boolean]",
    "array[object]": "array[object]",
    "array[file]": "array[file]",
    "array[any]": "array[any]",
    "any": "any",
}
PARAM_TYPES = {
    "string": "string",
    "select": "string",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "object": "object",
    "array[string]": "array[string]",
    "array[number]": "array[number]",
    "array[object]": "array[object]",
    "array[bool]": "array[boolean]",
    "array[boolean]": "array[boolean]",
}
COMPARISONS = {
    "contains": "contains",
    "not contains": "not_contains",
    "start with": "starts_with",
    "end with": "ends_with",
    "is": "=",
    "is not": "≠",
    "=": "=",
    "≠": "≠",
    ">": ">",
    "<": "<",
    "≥": "≥",
    "≤": "≤",
    "empty": "is_empty",
    "not empty": "is_not_empty",
    "null": "is_empty",
    "not null": "is_not_empty",
    "exists": "is_not_empty",
    "not exists": "is_empty",
    "in": "in",
    "not in": "not_in",
}
UNARY = frozenset({"is_empty", "is_not_empty"})
ASSIGN_OPS = {
    "over-write": "overwrite",
    "overwrite": "overwrite",
    "set": "overwrite",
    "append": "append",
    "clear": "clear",
    "+=": "+=",
    "-=": "-=",
}
ERROR_MODES = {
    "terminated": "terminate",
    "continue-on-error": "continue",
    "remove-abnormal-output": "remove",
}
BODY_TYPES = {
    "none": "none",
    "json": "json",
    "raw-text": "raw",
    "x-www-form-urlencoded": "form",
    "form-data": "form",
    "binary": "file",
}
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
# 켜져 있으면 경고만 남기는 Dify 앱 기능. 여는 말·추천 질문·파일 업로드는 옮긴다.
IGNORED_FEATURES = (
    "speech_to_text",
    "text_to_speech",
    "suggested_questions_after_answer",
    "retriever_resource",
    "sensitive_word_avoidance",
    "annotation_reply",
    "citation",
    "more_like_this",
)
MAX_ID = 32
MAX_SUGGESTED = 10
_ID_UNSAFE = re.compile(r"[^a-z0-9_]")
_BRANCH_UNSAFE = re.compile(r"[^A-Za-z0-9_]")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_DIFY_REF = re.compile(r"\{\{#([^{}#]+)#\}\}")
_JINJA_OPEN = re.compile(r"\{([{%#])")

Converter = Callable[[dict[str, Any], str, str], tuple[str, dict[str, Any]]]


class _Unsupported(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _dicts(value: object) -> list[dict[str, Any]]:
    items = cast(list[object], value) if isinstance(value, list) else []
    return [cast(dict[str, Any], item) for item in items if isinstance(item, dict)]


def _list(value: object) -> list[Any]:
    return cast(list[Any], value) if isinstance(value, list) else []


def _str(value: object) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _int(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _env_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def node_id_like(raw: str) -> str:
    """Dify 노드 id를 우리 형식으로. 숫자로 시작하면 n을 붙인다(1711528914102 → n1711528914102)."""
    text = _ID_UNSAFE.sub("_", raw.strip().lower())
    if not text or not "a" <= text[0] <= "z":
        text = "n" + text
    text = text[:MAX_ID]
    return f"n_{text}" if text in RESERVED_SCOPES else text


def _unique(base: str, taken: set[str]) -> str:
    base = base[:MAX_ID]
    candidate, number = base, 2
    while candidate in taken:
        suffix = f"_{number}"
        candidate = base[: MAX_ID - len(suffix)] + suffix
        number += 1
    taken.add(candidate)
    return candidate


def _branch_id(raw: object, index: int, prefix: str, taken: set[str]) -> str:
    """if_else 케이스·분류 클래스 id. 우리 형식은 [A-Za-z0-9_]{1,32}이고 핸들 이름이 된다."""
    base = _BRANCH_UNSAFE.sub("", _str(raw))[:MAX_ID] or f"{prefix}{index + 1}"
    return _unique(base, taken)


def _literal(text: str) -> str:
    # Jinja2가 읽을 여는 구분자를 글자 그대로 찍히게 바꾼다. 닫는 구분자는 블록 밖에서 글자다.
    return _JINJA_OPEN.sub(lambda match: "{{ '{" + match.group(1) + "' }}", text)


def _position(value: object) -> dict[str, float]:
    raw = _dict(value)
    return {"x": _number(raw.get("x")) or 0.0, "y": _number(raw.get("y")) or 0.0}


class _Converter:
    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.raw = raw
        self.kind: WorkflowKind = "workflow"
        self.warnings: list[DslWarning] = []
        # Dify 노드 id → 우리 id, Dify 노드 타입.
        self.ids: dict[str, str] = {}
        self.types: dict[str, str] = {}
        self.taken: set[str] = set()
        # Dify iteration id → 그 하위 그래프의 iteration_start(우리 id). item·index가 여기 출력이다.
        self.items: dict[str, str] = {}
        # iteration_start가 없던 옛 Dify 파일: (우리 iteration id, 만든 iteration_start id,
        # 첫 하위 노드의 Dify id).
        self.synthetic: list[tuple[str, str, str]] = []
        # 묶음을 쓰는 변수 집계 노드: Dify id → 옮긴 첫 묶음 이름.
        self.groups: dict[str, str] = {}
        # 분기 노드의 Dify 핸들 → 우리 핸들.
        self.handles: dict[str, dict[str, str]] = {}
        # 워크플로우에 Dify 파일 업로드 기능이 켜져 있으면 sys.files를 start의 files 입력으로
        # 읽는다.
        self.files_ref: str | None = None
        self.converters: dict[str, Converter] = {
            "start": self._start,
            "end": self._end,
            "answer": self._answer,
            "llm": self._llm,
            "code": self._code,
            "http-request": self._http,
            "if-else": self._if_else,
            "iteration": self._iteration,
            "iteration-start": self._iteration_start,
            "template-transform": self._template,
            "variable-aggregator": self._aggregator,
            # 옛 Dify에서 변수 집계 노드의 타입 이름이었다.
            "variable-assigner": self._aggregator,
            "assigner": self._assigner,
            "question-classifier": self._classifier,
            "parameter-extractor": self._extractor,
        }

    def warn(self, code: str, message: str, node_id: str | None = None) -> None:
        self.warnings.append(DslWarning(code=code, message=message, node_id=node_id))

    def run(self) -> DslDocument:
        app = _dict(self.raw.get("app"))
        mode = _str(app.get("mode"))
        kind = MODES.get(mode)
        if kind is None:
            raise DslError(
                "unsupported_app_mode",
                f"Dify {mode or '(mode 없음)'} 앱은 가져오지 않는다. "
                "workflow와 advanced-chat(챗플로우)만 가져온다",
            )
        self.kind = kind
        workflow = _dict(self.raw.get("workflow"))
        graph = _dict(workflow.get("graph"))
        raw_nodes = _dicts(graph.get("nodes"))
        if not raw_nodes:
            raise DslError("invalid_dsl", "Dify DSL에 workflow.graph.nodes가 없다")
        features = _dict(workflow.get("features"))
        kept = self._scan(raw_nodes, features)
        nodes = [self._node(item) for item in kept]
        nodes += [
            {
                "id": start,
                "type": "iteration_start",
                "title": "반복 시작",
                "parent_id": iteration,
                "position": {"x": 24.0, "y": 68.0},
                "config": {},
            }
            for iteration, start, _first in self.synthetic
        ]
        edges = self._edges(_dicts(graph.get("edges")))
        viewport = {
            key: number
            for key in ("x", "y", "zoom")
            if (number := _number(_dict(graph.get("viewport")).get(key))) is not None
        }
        dependencies = _list(self.raw.get("dependencies"))
        if dependencies:
            message = f"플러그인 의존성 {len(dependencies)}개는 무시했다(모델 공급자·도구 플러그인)"
            self.warn("dependencies_ignored", message)
        name = _str(app.get("name")).strip()[:100] or "가져온 Dify 앱"
        return validate_document(
            {
                "dsl_version": DSL_VERSION,
                "kind": self.kind,
                "name": name,
                "icon": self._icon(app),
                "description": _str(app.get("description")).strip()[:2000],
                "env_vars": self._env(_dicts(workflow.get("environment_variables"))),
                "conversation_vars": self._conversation(
                    _dicts(workflow.get("conversation_variables"))
                ),
                "settings": {"webapp": self._webapp(features)},
                "graph": {"nodes": nodes, "edges": edges, "viewport": viewport},
            }
        )

    def _scan(
        self, raw_nodes: list[dict[str, Any]], features: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        notes = 0
        for item in raw_nodes:
            data = _dict(item.get("data"))
            dify_id = _str(item.get("id")).strip()
            dify_type = _str(data.get("type"))
            if item.get("type") == "custom-note":
                notes += 1
                continue
            if not dify_id or dify_id in self.ids:
                self.warn(
                    "node_dropped", f"id가 없거나 겹치는 Dify 노드({dify_type or '?'})를 버렸다"
                )
                continue
            self.ids[dify_id] = _unique(node_id_like(dify_id), self.taken)
            self.types[dify_id] = dify_type
            kept.append(item)
        if notes:
            self.warn("notes_skipped", f"캔버스 메모 {notes}개는 가져오지 않았다")
        for item in kept:
            dify_id = _str(item.get("id")).strip()
            parent = self._parent(item)
            if self.types[dify_id] == "iteration-start" and parent is not None:
                self.items.setdefault(parent, self.ids[dify_id])
        for item in kept:
            dify_id = _str(item.get("id")).strip()
            data = _dict(item.get("data"))
            dify_type = self.types[dify_id]
            if dify_type == "iteration" and dify_id not in self.items:
                start = _unique(f"{self.ids[dify_id]}_start", self.taken)
                self.items[dify_id] = start
                self.synthetic.append((self.ids[dify_id], start, _str(data.get("start_node_id"))))
            elif dify_type in ("variable-aggregator", "variable-assigner"):
                advanced = _dict(data.get("advanced_settings"))
                groups = _dicts(advanced.get("groups"))
                if advanced.get("group_enabled") is True and groups:
                    self.groups[dify_id] = _str(groups[0].get("group_name"))
        upload = _dict(features.get("file_upload"))
        enabled = upload.get("enabled") is True or _dict(upload.get("image")).get("enabled") is True
        if enabled and self.kind == "workflow":
            start = next(
                (self.ids[key] for key, kind in self.types.items() if kind == "start"), None
            )
            if start is not None:
                self.files_ref = f"{start}.files"
        return kept

    def _parent(self, item: Mapping[str, Any]) -> str | None:
        data = _dict(item.get("data"))
        parent = _str(item.get("parentId")).strip()
        if not parent and data.get("isInIteration") is True:
            parent = _str(data.get("iteration_id")).strip()
        if not parent and data.get("isInLoop") is True:
            parent = _str(data.get("loop_id")).strip()
        return parent if parent in self.ids else None

    def ref(self, parts: Sequence[str]) -> str | None:
        """Dify 변수 경로(노드 id, 이름, 하위 키...)를 우리 선택자 글로."""
        if len(parts) < 2 or not all(parts):
            return None
        scope, name, rest = parts[0], parts[1], list(parts[2:])
        if scope == "sys":
            if name == "files" and self.files_ref is not None:
                return self.files_ref + "".join(f".{part}" for part in rest)
            target, name = "sys", SYS_NAMES.get(name, name)
        elif scope == "env":
            target = "env"
        elif scope == "conversation":
            target = "conv"
        elif scope in self.items and name in ("item", "index"):
            target = self.items[scope]
        else:
            target = self.ids.get(scope) or node_id_like(scope)
            dify_type = self.types.get(scope)
            if dify_type == "llm" and name == "structured_output":
                name = "json"
            elif scope in self.groups and name == self.groups[scope] and rest[:1] == ["output"]:
                name, rest = "output", rest[1:]
        path = "".join(f"[{part}]" if part.isdigit() else f".{part}" for part in rest)
        return f"{target}.{name}{path}"

    def selector(self, value: object) -> str | None:
        parts = _list(value)
        if not parts or not all(isinstance(part, str | int) for part in parts):
            return None
        return self.ref([str(part).strip() for part in parts])

    def text(self, value: object, *, context: str | None = None) -> str:
        """Dify 기본 프롬프트(변수는 {{#...#}}만)를 우리 템플릿 글로."""
        source = _str(value)
        pieces: list[str] = []
        position = 0
        for match in _DIFY_REF.finditer(source):
            pieces.append(_literal(source[position : match.start()]))
            inner = match.group(1).strip()
            if inner == "context":
                # 지식 검색 결과 자리. 지식 검색 노드가 unsupported라 보통 비어 있다.
                pieces.append("{{ " + context + " }}" if context else "")
            else:
                found = self.ref([part.strip() for part in inner.split(".")])
                pieces.append("{{ " + found + " }}" if found else _literal(match.group(0)))
            position = match.end()
        pieces.append(_literal(source[position:]))
        return "".join(pieces)

    def _value_type(self, value: object, node_id: str | None, what: str) -> VarType:
        found = VALUE_TYPES.get(_str(value))
        if found is None:
            if value not in (None, ""):
                self.warn("value_type_any", f"{what}의 타입 {value}을(를) any로 두었다", node_id)
            return "any"
        return found

    def _model(self, data: Mapping[str, Any], node_id: str) -> dict[str, Any]:
        model = _dict(data.get("model"))
        given = _dict(model.get("completion_params"))
        params: dict[str, Any] = {}
        temperature = _number(given.get("temperature"))
        if temperature is not None and 0 <= temperature <= 2:
            params["temperature"] = temperature
        top_p = _number(given.get("top_p"))
        if top_p is not None and 0 < top_p <= 1:
            params["top_p"] = top_p
        max_tokens = _int(given.get("max_tokens"))
        if max_tokens is not None and max_tokens >= 1:
            params["max_output_tokens"] = max_tokens
        original = "/".join(
            part for part in (_str(model.get("provider")), _str(model.get("name"))) if part
        )
        message = f"모델 {original or '(지정 없음)'} 대신 Gemini 폴백 목록의 첫 모델을 쓴다"
        self.warn("model_replaced", message, node_id)
        # model_id가 없으면 실행 때 폴백 목록의 첫 모델이다.
        return {"params": params}

    def _node(self, item: Mapping[str, Any]) -> dict[str, Any]:
        dify_id = _str(item.get("id")).strip()
        node_id = self.ids[dify_id]
        data = _dict(item.get("data"))
        dify_type = self.types[dify_id]
        node: dict[str, Any] = {
            "id": node_id,
            "title": _str(data.get("title")) or dify_type or node_id,
            "position": _position(item.get("position")),
        }
        parent = self._parent(item)
        if parent is not None:
            node["parent_id"] = self.ids[parent]
        convert = self.converters.get(dify_type)
        try:
            if convert is None:
                what = "아직 옮기지 않는" if dify_type in LATER_TYPES else "지원하지 않는"
                raise _Unsupported(f"Dify {dify_type or '(타입 없음)'} 노드는 {what} 노드다")
            node_type, config = convert(data, node_id, dify_id)
        except _Unsupported as exc:
            node["type"] = UNSUPPORTED_TYPE
            node["config"] = {"source": "dify", "original_type": dify_type, "data": data}
            self.warn("unsupported_node", f"{exc.reason}. 바꿔 끼우거나 지운다", node_id)
            return node
        node["type"] = node_type
        node["config"] = config
        self._error_policy(node, data)
        return node

    def _error_policy(self, node: dict[str, Any], data: Mapping[str, Any]) -> None:
        strategy = data.get("error_strategy")
        if strategy == "fail-branch":
            node["error"] = {"strategy": "branch"}
        elif strategy == "default-value":
            defaults: dict[str, Any] = {}
            for entry in _dicts(data.get("default_value")):
                key = _str(entry.get("key"))
                value = entry.get("value")
                if isinstance(value, str) and _str(entry.get("type")).startswith(
                    ("object", "array")
                ):
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                if key:
                    defaults[key] = value
            node["error"] = {"strategy": "default", "default_outputs": defaults}
        retry = _dict(data.get("retry_config"))
        if retry.get("enabled") is True:
            node["retry"] = {
                "max_attempts": _clamp(_int(retry.get("max_retries")) or 0, 0, 5),
                "interval_ms": _clamp(_int(retry.get("retry_interval")) or 1000, 0, 10_000),
            }

    def _bindings(self, value: object) -> list[dict[str, str]]:
        # template·code 노드의 [{variable, value_selector}] → [{name, var}].
        return [
            {
                "name": _str(item.get("variable")),
                "var": self.selector(item.get("value_selector")) or "",
            }
            for item in _dicts(value)
        ]

    def _start(
        self, data: dict[str, Any], node_id: str, dify_id: str
    ) -> tuple[str, dict[str, Any]]:
        fields: list[dict[str, Any]] = []
        for item in _dicts(data.get("variables")):
            name = _str(item.get("variable"))
            given = _str(item.get("type")) or "text-input"
            field_type = START_TYPES.get(given)
            if field_type is None:
                self.warn(
                    "start_field_type",
                    f"입력 {name}의 Dify 타입 {given}을(를) 글(string)로 바꿨다",
                    node_id,
                )
                field_type = "string"
            field: dict[str, Any] = {
                "name": name,
                "type": field_type,
                "label": _str(item.get("label")),
                "required": item.get("required") is True,
            }
            default = item.get("default")
            if default not in (None, "", []) and field_type not in ("file", "file_list"):
                field["default"] = default
            if field_type == "select":
                field["options"] = [_str(option) for option in _list(item.get("options"))]
            max_length = _int(item.get("max_length"))
            if field_type in ("string", "paragraph") and max_length is not None and max_length >= 1:
                field["max_length"] = max_length
            fields.append(field)
        if self.files_ref is not None and not any(field["name"] == "files" for field in fields):
            fields.append(
                {"name": "files", "type": "file_list", "label": "파일", "required": False}
            )
            message = (
                "Dify 파일 업로드 기능을 start 노드의 files 입력으로 옮겼다"
                "(sys.files → start.files)"
            )
            self.warn("file_upload_field", message, node_id)
        return "start", {"fields": fields}

    def _end(self, data: dict[str, Any], node_id: str, dify_id: str) -> tuple[str, dict[str, Any]]:
        outputs = [
            {
                "name": _str(item.get("variable")),
                "var": self.selector(item.get("value_selector")) or "",
            }
            for item in _dicts(data.get("outputs"))
        ]
        return "end", {"outputs": outputs}

    def _answer(
        self, data: dict[str, Any], node_id: str, dify_id: str
    ) -> tuple[str, dict[str, Any]]:
        return "answer", {"text": self.text(data.get("answer"))}

    def _jinja_prefix(self, value: object) -> str:
        # Dify jinja2 프롬프트는 prompt_config.jinja2_variables의 이름으로 변수를 읽는다. 우리
        # 프롬프트는 변수 풀이 컨텍스트이므로 같은 이름을 {% set %}으로 먼저 만든다.
        lines: list[str] = []
        for item in _dicts(value):
            name = _str(item.get("variable"))
            found = self.selector(item.get("value_selector"))
            if _NAME.fullmatch(name) and found:
                lines.append("{% set " + name + " = " + found + " %}")
        return "".join(lines)

    def _llm(self, data: dict[str, Any], node_id: str, dify_id: str) -> tuple[str, dict[str, Any]]:
        context = _dict(data.get("context"))
        context_ref = (
            self.selector(context.get("variable_selector"))
            if context.get("enabled") is True
            else None
        )
        given = data.get("prompt_template")
        # completion 모드 모델은 프롬프트가 블록 목록이 아니라 글 하나다.
        blocks = [cast(dict[str, Any], given)] if isinstance(given, dict) else _dicts(given)
        jinja = self._jinja_prefix(_dict(data.get("prompt_config")).get("jinja2_variables"))
        prompt: list[dict[str, str]] = []
        for block in blocks:
            role = _str(block.get("role")) or "user"
            if role not in ("system", "user", "assistant"):
                role = "user"
            if block.get("edition_type") == "jinja2":
                body = jinja + _str(block.get("jinja2_text"))
            else:
                body = self.text(block.get("text"), context=context_ref)
            prompt.append({"role": role, "text": body})
        config: dict[str, Any] = {"model": self._model(data, node_id), "prompt": prompt}
        memory = data.get("memory")
        if isinstance(memory, dict) and self.kind == "chatflow":
            memory = cast(dict[str, Any], memory)
            window = _dict(memory.get("window"))
            size = _int(window.get("size")) if window.get("enabled") is True else None
            config["memory"] = {"enabled": True, "window_turns": _clamp(size or 10, 1, 50)}
            # Dify는 메모리를 켜면 이 템플릿(기본 사용자 질문)을 마지막 user 메시지로 붙인다.
            query = _str(memory.get("query_prompt_template")) or "{{#sys.query#}}"
            prompt.append({"role": "user", "text": self.text(query, context=context_ref)})
        vision = _dict(data.get("vision"))
        if vision.get("enabled") is True:
            found = self.selector(_dict(vision.get("configs")).get("variable_selector"))
            if found is None and self.kind == "chatflow":
                found = "sys.files"
            config["vision"] = {"enabled": True, "var": found}
        if data.get("structured_output_enabled") is True:
            schema = _dict(data.get("structured_output")).get("schema")
            if isinstance(schema, dict):
                config["json_mode"] = True
                config["output_schema"] = schema
        return "llm", config

    def _code(self, data: dict[str, Any], node_id: str, dify_id: str) -> tuple[str, dict[str, Any]]:
        language = _str(data.get("code_language")) or "python3"
        if language != "python3":
            raise _Unsupported(f"Dify code 노드의 {language} 코드는 지원하지 않는다(파이썬만 돈다)")
        outputs: list[dict[str, Any]] = []
        given = data.get("outputs")
        if isinstance(given, dict):
            for name, spec in cast(dict[str, Any], given).items():
                kind = self._value_type(_dict(spec).get("type"), node_id, f"출력 {name}")
                outputs.append({"name": name, "type": kind})
        else:
            for item in _dicts(given):
                name = _str(item.get("variable") or item.get("name"))
                kind = self._value_type(item.get("type"), node_id, f"출력 {name}")
                outputs.append({"name": name, "type": kind})
        return "code", {
            "code": _str(data.get("code")),
            "inputs": self._bindings(data.get("variables")),
            "outputs": outputs,
        }

    def _pairs(self, value: object, node_id: str) -> list[dict[str, Any]]:
        """Dify 헤더·쿼리·폼 값("이름:값" 줄 글 또는 [{key, value}])을 [{name, value}]로."""
        items: list[tuple[str, object, str]] = []
        if isinstance(value, str):
            for line in value.splitlines():
                name, separator, rest = line.partition(":")
                if separator and name.strip():
                    items.append((name.strip(), rest.strip(), "text"))
        else:
            for entry in _dicts(value):
                name = _str(entry.get("key")).strip()
                items.append((name, entry.get("value"), _str(entry.get("type"))))
        pairs: list[dict[str, Any]] = []
        for name, raw, kind in items:
            if not name:
                continue
            if kind == "file":
                message = f"파일 필드 {name}은(는) 보낼 수 없어 뺐다"
                self.warn("http_file_field", message, node_id)
                continue
            pairs.append({"name": name, "value": self.text(raw)})
        return pairs

    def _http(self, data: dict[str, Any], node_id: str, dify_id: str) -> tuple[str, dict[str, Any]]:
        method = (_str(data.get("method")) or "get").upper()
        body = _dict(data.get("body"))
        given = _str(body.get("type")) or "none"
        body_type = BODY_TYPES.get(given)
        if body_type is None:
            self.warn("http_body_dropped", f"본문 형식 {given}은(는) 옮기지 못해 비웠다", node_id)
            body_type = "none"
        payload = body.get("data")
        content: str | list[dict[str, Any]] = ""
        if body_type in ("json", "raw"):
            # 새 Dify는 [{type, value}] 목록, 옛 Dify는 글 하나다.
            entries = _dicts(payload)
            if isinstance(payload, list):
                text = "".join(_str(item.get("value")) for item in entries)
            else:
                text = _str(payload)
            content = self.text(text)
        elif body_type == "form":
            content = self._pairs(payload, node_id)
            if given == "form-data":
                message = "multipart form-data 본문은 x-www-form-urlencoded로 보낸다"
                self.warn("http_form_data", message, node_id)
        config: dict[str, Any] = {
            "method": method,
            "url": self.text(data.get("url")),
            "headers": self._pairs(data.get("headers"), node_id),
            "params": self._pairs(data.get("params"), node_id),
            "body": {"type": body_type, "content": content},
        }
        authorization = _dict(data.get("authorization"))
        if authorization.get("type") == "api-key":
            settings = _dict(authorization.get("config"))
            key = self.text(settings.get("api_key"))
            kind = settings.get("type")
            if kind == "basic":
                # Dify는 api_key를 Basic 뒤에 그대로 붙인다. 우리 basic은 "사용자:비밀번호"를
                # 받으므로 헤더 인증으로 같은 값을 보낸다.
                config["auth"] = {
                    "type": "header",
                    "header": "Authorization",
                    "value": f"Basic {key}",
                }
            elif kind == "custom":
                header = _str(settings.get("header")) or "Authorization"
                config["auth"] = {"type": "header", "header": header, "value": key}
            else:
                config["auth"] = {"type": "bearer", "value": key}
        timeout = _dict(data.get("timeout"))
        seconds = [
            number
            for key in ("connect", "read", "write")
            if (number := _number(timeout.get(key))) is not None and number > 0
        ]
        if seconds:
            config["timeout_sec"] = _clamp(math.ceil(max(seconds)), 1, 300)
        if data.get("ssl_verify") is False:
            self.warn("http_ssl_verify", "인증서 검사를 끄는 설정은 가져오지 않는다", node_id)
        if method not in HTTP_METHODS:
            self.warn("http_method", f"HTTP 메서드 {method}은(는) 쓸 수 없다", node_id)
        return "http_request", config

    def _condition(self, item: Mapping[str, Any], node_id: str) -> dict[str, Any]:
        var = self.selector(item.get("variable_selector")) or ""
        given = _str(item.get("comparison_operator"))
        op = COMPARISONS.get(given)
        if item.get("sub_variable_condition"):
            # 파일 속성 조건. 원래 연산자 대신 쓸 수 없는 이름을 두어 검증 오류로 드러낸다.
            self.warn("condition_unsupported", "파일 속성 조건은 옮기지 못했다", node_id)
            return {"var": var, "op": "sub_variable_condition", "value": item.get("value")}
        if op is None:
            self.warn("condition_unsupported", f"조건 연산자 {given}은(는) 옮기지 못했다", node_id)
            return {"var": var, "op": given or "(없음)", "value": item.get("value")}
        if op in UNARY:
            return {"var": var, "op": op}
        value = item.get("value")
        if isinstance(value, str) and "{{#" in value:
            message = "조건 값 속 변수 참조는 옮기지 못해 글자로 남겼다"
            self.warn("condition_value_reference", message, node_id)
        if op in ("in", "not_in") and not isinstance(value, list):
            value = [] if value in (None, "") else [value]
        return {"var": var, "op": op, "value": value}

    def _if_else(
        self, data: dict[str, Any], node_id: str, dify_id: str
    ) -> tuple[str, dict[str, Any]]:
        cases = _dicts(data.get("cases"))
        if not cases:
            # 케이스 목록이 없던 옛 형식은 조건 묶음 하나와 true·false 핸들이다.
            cases = [
                {
                    "case_id": "true",
                    "logical_operator": data.get("logical_operator"),
                    "conditions": data.get("conditions"),
                }
            ]
        handles = {"false": ELSE_HANDLE}
        taken: set[str] = set()
        converted: list[dict[str, Any]] = []
        for index, case in enumerate(cases):
            case_id = _branch_id(case.get("case_id"), index, "case", taken)
            handles[_str(case.get("case_id"))] = f"case_{case_id}"
            converted.append(
                {
                    "id": case_id,
                    "logical": "or" if case.get("logical_operator") == "or" else "and",
                    "conditions": [
                        self._condition(item, node_id) for item in _dicts(case.get("conditions"))
                    ],
                }
            )
        self.handles[dify_id] = handles
        return "if_else", {"cases": converted}

    def _iteration(
        self, data: dict[str, Any], node_id: str, dify_id: str
    ) -> tuple[str, dict[str, Any]]:
        parallel = data.get("is_parallel") is True
        count = _int(data.get("parallel_nums")) or 1
        if parallel and count > MAX_PARALLEL:
            self.warn(
                "iteration_parallel", f"병렬 수를 {count}에서 {MAX_PARALLEL}로 줄였다", node_id
            )
        given = _str(data.get("error_handle_mode")) or "terminated"
        mode = ERROR_MODES.get(given)
        if mode is None:
            self.warn(
                "iteration_error_mode", f"오류 처리 {given}은(는) terminate로 두었다", node_id
            )
            mode = "terminate"
        return "iteration", {
            "input_var": self.selector(data.get("iterator_selector")) or "",
            "output_var": self.selector(data.get("output_selector")) or "",
            "parallel": parallel,
            "max_parallel": _clamp(count, 1, MAX_PARALLEL) if parallel else 1,
            "error_mode": mode,
        }

    def _iteration_start(
        self, data: dict[str, Any], node_id: str, dify_id: str
    ) -> tuple[str, dict[str, Any]]:
        return "iteration_start", {}

    def _template(
        self, data: dict[str, Any], node_id: str, dify_id: str
    ) -> tuple[str, dict[str, Any]]:
        # Dify 템플릿 변환 노드는 원래 Jinja2이고 변수를 이름으로 읽는다. 우리 template 노드와 같다.
        return "template", {
            "template": _str(data.get("template")),
            "vars": self._bindings(data.get("variables")),
        }

    def _aggregator(
        self, data: dict[str, Any], node_id: str, dify_id: str
    ) -> tuple[str, dict[str, Any]]:
        if "items" in data or "assigned_variable_selector" in data:
            return self._assigner(data, node_id, dify_id)
        advanced = _dict(data.get("advanced_settings"))
        groups = _dicts(advanced.get("groups"))
        if advanced.get("group_enabled") is True and groups:
            variables, output_type = groups[0].get("variables"), groups[0].get("output_type")
            if len(groups) > 1:
                message = (
                    f"묶음 {len(groups)}개 중 첫 묶음({self.groups.get(dify_id, '')})만 옮겼다. "
                    "다른 묶음을 읽는 참조는 검증 오류로 남는다"
                )
                self.warn("aggregator_groups", message, node_id)
        else:
            variables, output_type = data.get("variables"), data.get("output_type")
        refs = [found for item in _list(variables) if (found := self.selector(item)) is not None]
        return "variable_aggregator", {
            "vars": refs,
            "output_type": self._value_type(output_type, node_id, "output_type"),
        }

    def _assigner(
        self, data: dict[str, Any], node_id: str, dify_id: str
    ) -> tuple[str, dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(data.get("items"), list):
            for entry in _dicts(data.get("items")):
                given = _str(entry.get("operation")) or "over-write"
                op = ASSIGN_OPS.get(given)
                if op is None:
                    self.warn(
                        "assign_op_unsupported", f"할당 연산 {given}은(는) 옮기지 못했다", node_id
                    )
                    op = given
                item: dict[str, Any] = {
                    "target": self.selector(entry.get("variable_selector")) or "",
                    "op": op,
                }
                if op != "clear":
                    if entry.get("input_type") == "constant":
                        item["value"] = entry.get("value")
                    else:
                        item["var"] = self.selector(entry.get("value")) or ""
                items.append(item)
        else:
            given = _str(data.get("write_mode")) or "over-write"
            op = ASSIGN_OPS.get(given, given)
            item = {"target": self.selector(data.get("assigned_variable_selector")) or "", "op": op}
            if op != "clear":
                item["var"] = self.selector(data.get("input_variable_selector")) or ""
            items.append(item)
        return "variable_assigner", {"items": items}

    def _classifier(
        self, data: dict[str, Any], node_id: str, dify_id: str
    ) -> tuple[str, dict[str, Any]]:
        classes: list[dict[str, str]] = []
        handles: dict[str, str] = {}
        taken: set[str] = set()
        for index, entry in enumerate(_dicts(data.get("classes"))):
            # Dify 클래스의 name은 분류 기준 글이고 출력 class_name이 된다. 우리 class_name은
            # description이므로 거기에 두고, 제목(name)은 첫 줄로 짧게 만든다.
            text = _str(entry.get("name")).strip()
            class_id = _branch_id(entry.get("id"), index, "class", taken)
            first = next((line.strip() for line in text.splitlines() if line.strip()), "")
            classes.append(
                {
                    "id": class_id,
                    "name": first[:200] or f"분류 {index + 1}",
                    "description": text[:2000],
                }
            )
            handles[_str(entry.get("id"))] = f"class_{class_id}"
        self.handles[dify_id] = handles
        instruction = data.get("instruction") or data.get("instructions")
        return "question_classifier", {
            "model": self._model(data, node_id),
            "query_var": self.selector(data.get("query_variable_selector")) or "",
            "classes": classes,
            "instruction": self.text(instruction),
        }

    def _extractor(
        self, data: dict[str, Any], node_id: str, dify_id: str
    ) -> tuple[str, dict[str, Any]]:
        parameters: list[dict[str, Any]] = []
        for entry in _dicts(data.get("parameters")):
            name = _str(entry.get("name"))
            given = _str(entry.get("type")) or "string"
            kind = PARAM_TYPES.get(given)
            if kind is None:
                self.warn(
                    "parameter_type",
                    f"파라미터 {name}의 타입 {given}을(를) string으로 두었다",
                    node_id,
                )
                kind = "string"
            parameter: dict[str, Any] = {
                "name": name,
                "type": kind,
                "description": _str(entry.get("description"))[:2000],
                "required": entry.get("required") is True,
            }
            options = [
                option
                for option in _list(entry.get("options"))
                if isinstance(option, str | int | float) and not isinstance(option, bool)
            ]
            if given == "select" and options:
                parameter["enum"] = options
            parameters.append(parameter)
        return "parameter_extractor", {
            "model": self._model(data, node_id),
            "query_var": self.selector(data.get("query")) or "",
            "parameters": parameters,
            "instruction": self.text(data.get("instruction")),
            "reasoning_mode": "prompt" if data.get("reasoning_mode") == "prompt" else "tool_call",
        }

    def _handle(self, source: str, handle: str) -> str:
        if handle in ("", "source"):
            return OUT_HANDLE
        if handle == "fail-branch":
            return FAIL_HANDLE
        mapped = self.handles.get(source, {}).get(handle)
        if mapped is not None:
            return mapped
        return _BRANCH_UNSAFE.sub("_", handle)

    def _edges(self, raw_edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        for entry in raw_edges:
            source_id = _str(entry.get("source")).strip()
            target_id = _str(entry.get("target")).strip()
            source, target = self.ids.get(source_id), self.ids.get(target_id)
            if source is None or target is None:
                message = f"없는 노드를 잇는 엣지({source_id} → {target_id})를 버렸다"
                self.warn("edge_dropped", message)
                continue
            edges.append(
                {
                    "id": f"e{len(edges) + 1}",
                    "source": source,
                    "source_handle": self._handle(source_id, _str(entry.get("sourceHandle"))),
                    "target": target,
                }
            )
        for _iteration, start, first in self.synthetic:
            target = self.ids.get(first)
            if target is not None:
                edges.append({"id": f"e{len(edges) + 1}", "source": start, "target": target})
        return edges

    def _icon(self, app: Mapping[str, Any]) -> str | None:
        icon = _str(app.get("icon")).strip()
        if not icon:
            return None
        if app.get("icon_type") not in (None, "emoji") or len(icon) > 64:
            self.warn("icon_dropped", "Dify 이미지 아이콘은 가져오지 않는다")
            return None
        return icon

    def _env(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        env: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            name = _str(item.get("name"))
            value = item.get("value")
            secret = item.get("value_type") == "secret"
            text = (
                None
                if value in (None, "") and secret
                else _env_text("" if value is None else value)
            )
            if not _ENV_NAME.fullmatch(name) or name in seen:
                self.warn("env_dropped", f"환경 변수 이름 {name!r}을(를) 쓸 수 없어 버렸다")
                continue
            if text is not None and len(text) > MAX_ENV_VALUE_CHARS:
                self.warn("env_dropped", f"환경 변수 {name}의 값이 너무 길어 버렸다")
                continue
            seen.add(name)
            env.append({"name": name, "value": text, "secret": secret})
        return env

    def _conversation(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        variables: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            name = _str(item.get("name"))
            if not _NAME.fullmatch(name) or name in seen:
                self.warn(
                    "conversation_var_dropped", f"대화 변수 이름 {name!r}을(를) 쓸 수 없어 버렸다"
                )
                continue
            seen.add(name)
            kind = self._value_type(item.get("value_type"), None, f"대화 변수 {name}")
            variables.append({"name": name, "type": kind, "default": item.get("value")})
        return variables

    def _webapp(self, features: Mapping[str, Any]) -> dict[str, Any]:
        webapp: dict[str, Any] = {}
        opening = _str(features.get("opening_statement")).strip()
        if opening:
            webapp["opening_statement"] = opening[:4000]
        questions = [
            question.strip()[:200]
            for question in _list(features.get("suggested_questions"))
            if isinstance(question, str) and question.strip()
        ]
        if len(questions) > MAX_SUGGESTED:
            message = f"추천 질문 {len(questions)}개 중 앞의 {MAX_SUGGESTED}개만 옮겼다"
            self.warn("suggested_questions_trimmed", message)
        if questions:
            webapp["suggested_questions"] = questions[:MAX_SUGGESTED]
        ignored = [
            name for name in IGNORED_FEATURES if _dict(features.get(name)).get("enabled") is True
        ]
        if ignored:
            self.warn("features_ignored", f"가져오지 않은 Dify 앱 기능: {', '.join(ignored)}")
        return webapp


def convert_dify(raw: Mapping[str, Any]) -> tuple[DslDocument, list[DslWarning]]:
    """Dify DSL 사전 → 우리 문서와 변환 경고. 가져올 수 없는 앱이면 DslError."""
    converter = _Converter(raw)
    document = converter.run()
    return document, converter.warnings
