"""메타데이터 필터 조건 검사(3단계 설계 7.5). SQL로 바꾸는 일은 app/db/repositories/
knowledge_search.py가 하고, 여기서는 필드 타입에 맞는 연산자인지 보고 값을 비교할 꼴로 바꾼다.

값 비교는 대소문자를 구분하는 정확 일치다(Dify와 같다). time 값은 시간대가 있어야 한다(저장된 값이
UTC라 시간대 없는 값은 몇 시인지 정할 수 없다).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from app.schemas.knowledge import MetadataType
from app.schemas.retrieval import LogicalOperator, MetadataCondition, MetadataFilter

FieldType = MetadataType
ClauseValue = str | float | datetime | tuple[str, ...] | tuple[float, ...] | None

OPERATORS: dict[FieldType, frozenset[str]] = {
    "string": frozenset(
        {
            "is",
            "is not",
            "contains",
            "not contains",
            "start with",
            "end with",
            "empty",
            "not empty",
            "in",
            "not in",
        }
    ),
    "number": frozenset({"=", "≠", ">", "<", "≥", "≤", "empty", "not empty", "in", "not in"}),
    "time": frozenset({"is", "before", "after", "empty", "not empty"}),
}
NO_VALUE = frozenset({"empty", "not empty"})
LIST_VALUE = frozenset({"in", "not in"})
# 빈 글로 찾으면 모든 값이 맞으므로 값을 요구한다.
NON_EMPTY_TEXT = frozenset({"contains", "not contains", "start with", "end with"})


@dataclass(frozen=True, slots=True)
class FieldInfo:
    id: UUID
    name: str
    type: FieldType


@dataclass(frozen=True, slots=True)
class FilterClause:
    name: str
    field_type: FieldType
    operator: str
    value: ClauseValue


@dataclass(frozen=True, slots=True)
class FilterPlan:
    logical_operator: LogicalOperator
    clauses: tuple[FilterClause, ...]


class FilterError(ValueError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _table(fields: Iterable[FieldInfo]) -> dict[str, FieldType]:
    table: dict[str, FieldType] = {}
    for field in fields:
        table[field.name] = field.type
    return table


def common_fields(per_kb: Sequence[Iterable[FieldInfo]]) -> dict[str, FieldType]:
    """모든 KB에 같은 이름·타입으로 있는 필드(7.5). 이름 → 타입, 첫 KB의 필드 순서."""
    if not per_kb:
        return {}
    tables: list[dict[str, FieldType]] = [_table(fields) for fields in per_kb]
    first = tables[0]
    return {
        name: kind
        for name, kind in first.items()
        if all(table.get(name) == kind for table in tables[1:])
    }


def check_fields(
    conditions: Sequence[MetadataCondition], per_kb: Mapping[UUID, Iterable[FieldInfo]]
) -> dict[str, FieldType]:
    """조건이 쓰는 필드가 모든 KB에 같은 타입으로 있는지. 없으면 metadata_field_not_found, 타입이
    다르면 metadata_field_mismatch."""
    tables = {kb_id: _table(fields) for kb_id, fields in per_kb.items()}
    types: dict[str, FieldType] = {}
    for condition in conditions:
        name = condition.name
        missing = [kb_id for kb_id, table in tables.items() if name not in table]
        if missing:
            raise FilterError(
                "metadata_field_not_found",
                f"메타데이터 필드 {name}이(가) 없는 지식 베이스가 있다",
                details={"name": name, "kb_ids": [str(kb_id) for kb_id in missing]},
            )
        kinds: set[FieldType] = {table[name] for table in tables.values()}
        if len(kinds) != 1:
            raise FilterError(
                "metadata_field_mismatch",
                f"메타데이터 필드 {name}의 타입이 지식 베이스마다 다르다",
                details={"name": name},
            )
        types[name] = kinds.pop()
    return types


def plan(metadata_filter: MetadataFilter, types: Mapping[str, FieldType]) -> FilterPlan:
    clauses: list[FilterClause] = []
    for index, condition in enumerate(metadata_filter.conditions):
        field_type = types.get(condition.name)
        if field_type is None:
            raise FilterError(
                "metadata_field_not_found",
                f"메타데이터 필드 {condition.name}이(가) 없다",
                details={"name": condition.name, "index": index},
            )
        clauses.append(clause(condition, field_type, index))
    return FilterPlan(metadata_filter.logical_operator, tuple(clauses))


def clause(condition: MetadataCondition, field_type: FieldType, index: int = 0) -> FilterClause:
    operator = condition.comparison_operator
    if operator not in OPERATORS[field_type]:
        allowed = ", ".join(sorted(OPERATORS[field_type]))
        raise _invalid(
            condition,
            index,
            f"{field_type} 필드에는 {operator} 연산자를 쓸 수 없다({allowed})",
        )
    if operator in NO_VALUE:
        return FilterClause(condition.name, field_type, operator, None)
    raw = condition.value
    if raw is None:
        raise _invalid(condition, index, f"{operator} 조건에는 값이 필요하다")
    if operator in LIST_VALUE:
        items = _split(raw)
        if not items:
            raise _invalid(condition, index, f"{operator} 조건에는 값이 하나 이상 필요하다")
        if field_type == "number":
            numbers = tuple(_number(item, condition, index) for item in items)
            return FilterClause(condition.name, field_type, operator, numbers)
        texts = tuple(_text(item, condition, index) for item in items)
        return FilterClause(condition.name, field_type, operator, texts)
    if isinstance(raw, list):
        raise _invalid(condition, index, f"{operator} 조건의 값은 목록이 아니어야 한다")
    if field_type == "number":
        return FilterClause(condition.name, field_type, operator, _number(raw, condition, index))
    if field_type == "time":
        return FilterClause(condition.name, field_type, operator, _time(raw, condition, index))
    text = _text(raw, condition, index)
    if operator in NON_EMPTY_TEXT and not text:
        raise _invalid(condition, index, f"{operator} 조건의 값은 빈 글이 아니어야 한다")
    return FilterClause(condition.name, field_type, operator, text)


def _invalid(condition: MetadataCondition, index: int, message: str) -> FilterError:
    return FilterError(
        "metadata_condition_invalid",
        message,
        details={"name": condition.name, "index": index},
    )


def _split(raw: object) -> list[object]:
    # Dify처럼 쉼표로 이은 글도 목록으로 받는다.
    if isinstance(raw, list):
        return list(cast(list[object], raw))
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return [raw]


def _text(raw: object, condition: MetadataCondition, index: int) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool):
        return str(raw)
    raise _invalid(condition, index, "string 필드의 값은 글이어야 한다")


def _number(raw: object, condition: MetadataCondition, index: int) -> float:
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            pass
    raise _invalid(condition, index, "number 필드의 값은 수여야 한다")


def _time(raw: object, condition: MetadataCondition, index: int) -> datetime:
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(raw, UTC)
        except (OverflowError, OSError, ValueError):
            pass
    elif isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.strip())
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    raise _invalid(
        condition,
        index,
        "time 필드의 값은 시간대가 있는 ISO 8601 글(예: 2026-11-02T09:00:00+09:00)이나 Unix 초다",
    )
