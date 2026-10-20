# pyright: strict
"""list_operator 노드(2단계 설계 2.4). 배열을 거르고, 정렬하고, 잘라 내고, 원소 하나를 꺼낸다.

순서는 filter → sort → slice다. 그 결과가 result이고 first_record·last_record는 그 처음·마지막
원소, item은 extract_index(0부터, 음수면 뒤에서) 자리의 원소다. 원소에서 비교·정렬할 값은 key로
정한다. 파일 배열은 name·size·mime·extension, 객체 배열은 키 이름(점으로 하위 키), 글·숫자 배열은
비워 원소 자체를 쓴다. 필터 조건은 if_else의 연산자와 평가 규칙을 그대로 쓴다.
"""

import json
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.workflow import Issue, VarType
from app.workflow.nodes.base import (
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    VarRef,
)
from app.workflow.nodes.if_else import Op, evaluate, operator_issue, value_issue
from app.workflow.variables import ARRAY_ITEM, MAX_ARRAY_ITEMS, is_file

# 파일 원소에서 쓸 수 있는 key와 그 값의 타입.
FILE_KEYS: dict[str, VarType] = {
    "name": "string",
    "size": "number",
    "mime": "string",
    "extension": "string",
}


class ItemCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 비우면 원소 자체. 파일이면 name·size·mime·extension, 객체면 키(점으로 하위 키).
    key: str = ""
    op: Op
    value: Any = None


class ListFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logical: Literal["and", "or"] = "and"
    conditions: list[ItemCondition] = Field(min_length=1)


class ListSort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = ""
    order: Literal["asc", "desc"] = "asc"


class ListSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 앞에서 건너뛸 원소 수.
    offset: int = Field(default=0, ge=0)
    # 남길 원소 수. null이면 끝까지.
    limit: int | None = Field(default=None, ge=0)


class ListOperatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_var: str
    filter: ListFilter | None = None
    sort: ListSort | None = None
    slice: ListSlice | None = None
    # item으로 꺼낼 result의 자리. 0부터, 음수면 뒤에서. null이면 item은 null이다.
    extract_index: int | None = None


def _extension(name: str) -> str:
    stem, dot, extension = name.rpartition(".")
    return extension.lower() if dot and stem else ""


def item_value(item: Any, key: str) -> Any:
    """원소에서 key가 가리키는 값. 없으면 None."""
    if not key:
        return item
    if is_file(item):
        file = cast(dict[str, Any], item)
        if key == "name":
            return file.get("filename")
        if key == "size":
            return file.get("size_bytes")
        if key == "mime":
            return file.get("mime")
        if key == "extension":
            return _extension(str(file.get("filename") or ""))
        return None
    value: Any = item
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = cast(dict[str, Any], value)[part]
    return value


def _sort_key(value: Any) -> tuple[int, Any]:
    # 타입이 섞인 배열도 정렬되게 수, 글, 그 밖(JSON 글) 순으로 묶는다.
    if isinstance(value, bool):
        return 0, int(value)
    if isinstance(value, int | float):
        return 0, value
    if isinstance(value, str):
        return 1, value
    return 2, json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _key_type(item_type: VarType | None, key: str) -> VarType | None:
    """검증에서 연산자를 맞춰 볼 값의 타입. 모르면 None."""
    if not key:
        return item_type
    if item_type == "file":
        return FILE_KEYS.get(key)
    return None


class ListOperatorRunner(NodeRunner[ListOperatorConfig]):
    type = "list_operator"
    Config = ListOperatorConfig
    fixed_outputs = (
        OutputSpec("result", "array[any]"),
        OutputSpec("count", "number"),
        OutputSpec("first_record", "any"),
        OutputSpec("last_record", "any"),
        OutputSpec("item", "any"),
    )

    def var_refs(self, config: ListOperatorConfig) -> list[VarRef]:
        return [VarRef("input_var", config.input_var, "array[any]")]

    def validate(self, config: ListOperatorConfig, graph: GraphView) -> list[Issue]:
        source = graph.output_type(config.input_var)
        item_type = ARRAY_ITEM.get(source) if source is not None else None
        issues: list[Issue] = []
        keys: list[tuple[str, str]] = []
        if config.filter is not None:
            for index, condition in enumerate(config.filter.conditions):
                field = f"filter.conditions[{index}]"
                keys.append((condition.key, f"{field}.key"))
                key_type = _key_type(item_type, condition.key)
                problem = operator_issue(condition.op, key_type)
                code, at = "invalid_operator", f"{field}.op"
                if problem is None:
                    problem = value_issue(condition.op, condition.value, key_type)
                    code, at = "invalid_config", f"{field}.value"
                if problem is not None:
                    issues.append(Issue(level="error", code=code, message=problem, field=at))
        if config.sort is not None:
            keys.append((config.sort.key, "sort.key"))
        if item_type == "file":
            for key, field in keys:
                if key not in FILE_KEYS:
                    message = "파일 배열의 key는 name·size·mime·extension 중 하나다"
                    issues.append(
                        Issue(level="error", code="invalid_config", message=message, field=field)
                    )
        return issues

    async def run(
        self, ctx: NodeContext, config: ListOperatorConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        value = ctx.resolve(config.input_var, "array[any]")
        # 건너뛴 상류(null)는 빈 배열로 본다.
        items: list[Any] = list(cast(list[Any], value)) if isinstance(value, list) else []
        if len(items) > MAX_ARRAY_ITEMS:
            raise NodeFailure("variable_limit", f"배열 원소가 {MAX_ARRAY_ITEMS}개를 넘는다")
        process: dict[str, Any] = {"input_count": len(items)}
        if config.filter is not None:
            items = [item for item in items if self._matches(config.filter, item)]
            process["filtered_count"] = len(items)
        if config.sort is not None:
            key = config.sort.key
            present = [item for item in items if item_value(item, key) is not None]
            # 값이 없는 원소는 정렬 방향과 관계없이 뒤에 둔다.
            missing = [item for item in items if item_value(item, key) is None]
            present.sort(
                key=lambda item: _sort_key(item_value(item, key)),
                reverse=config.sort.order == "desc",
            )
            items = present + missing
        if config.slice is not None:
            start = config.slice.offset
            end = None if config.slice.limit is None else start + config.slice.limit
            items = items[start:end]
        picked: Any = None
        index = config.extract_index
        if index is not None and -len(items) <= index < len(items):
            picked = items[index]
        return NodeResult(
            status="succeeded",
            outputs={
                "result": items,
                "count": len(items),
                "first_record": items[0] if items else None,
                "last_record": items[-1] if items else None,
                "item": picked,
            },
            process=process,
        )

    def _matches(self, rule: ListFilter, item: Any) -> bool:
        results = (
            evaluate(condition.op, item_value(item, condition.key), condition.value)
            for condition in rule.conditions
        )
        return all(results) if rule.logical == "and" else any(results)
