# pyright: strict
"""start 노드(설계 2.3). 런 입력을 필드 정의로 검사·변환해 start.<name>으로 내놓는다.

서비스가 런을 만들기 전에 같은 coerce_inputs로 입력을 미리 검사해 422를 낼 수 있다.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.workflow import Issue, VarType
from app.workflow.nodes.base import (
    NAME_PATTERN,
    GraphView,
    NodeContext,
    NodeFailure,
    NodeResult,
    NodeRunner,
    OutputSpec,
    duplicate_names,
)
from app.workflow.variables import FileRef, VariableError, coerce

FieldType = Literal["string", "number", "boolean", "select", "file", "file_list", "paragraph"]
FIELD_TYPES: dict[FieldType, VarType] = {
    "string": "string",
    "paragraph": "string",
    "select": "string",
    "number": "number",
    "boolean": "boolean",
    "file": "file",
    "file_list": "array[file]",
}


class StartField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    type: FieldType = "string"
    label: str = ""
    required: bool = False
    default: Any = None
    options: list[str] | None = None
    max_length: int | None = Field(default=None, ge=1)


class StartConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[StartField] = Field(default_factory=list[StartField])


class InputError(ValueError):
    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__(", ".join(f"{name}: {message}" for name, message in self.errors.items()))


def _file(value: Any) -> dict[str, Any]:
    try:
        return FileRef.model_validate(value).model_dump(mode="json")
    except ValidationError:
        raise ValueError("파일 참조가 아니다") from None


def _convert(field: StartField, value: Any) -> Any:
    if field.type in ("string", "paragraph", "select"):
        text = cast(str, coerce(value, "string"))
        if field.max_length is not None and len(text) > field.max_length:
            raise ValueError(f"{field.max_length}자를 넘는다")
        if field.type == "select" and field.options is not None and text not in field.options:
            raise ValueError("선택지에 없는 값이다")
        return text
    if field.type == "number":
        return coerce(value, "number")
    if field.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError("true 또는 false여야 한다")
        return value
    if field.type == "file":
        return _file(value)
    if not isinstance(value, list):
        raise ValueError("파일 목록이어야 한다")
    return [_file(item) for item in cast(list[Any], value)]


def coerce_inputs(fields: Sequence[StartField], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """필드마다 값을 검사·변환한다. 없으면 default, 그것도 없으면 null. 모르는 키는 버린다."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field in fields:
        value = inputs.get(field.name)
        if value is None:
            value = field.default
        if value is None or (field.required and value in ("", [])):
            if field.required:
                errors[field.name] = "필수 입력이다"
            else:
                values[field.name] = None
            continue
        try:
            values[field.name] = _convert(field, value)
        except VariableError as exc:
            errors[field.name] = exc.message
        except ValueError as exc:
            errors[field.name] = str(exc)
    if errors:
        raise InputError(errors)
    return values


class StartRunner(NodeRunner[StartConfig]):
    type = "start"
    Config = StartConfig
    dynamic_outputs = True

    def outputs(self, config: StartConfig) -> list[OutputSpec]:
        return [OutputSpec(field.name, FIELD_TYPES[field.type]) for field in config.fields]

    def validate(self, config: StartConfig, graph: GraphView) -> list[Issue]:
        issues = duplicate_names((field.name for field in config.fields), "fields[{}].name")
        for index, field in enumerate(config.fields):
            if field.type == "select" and not field.options:
                issues.append(
                    Issue(
                        level="error",
                        code="invalid_config",
                        message="select 필드에는 options가 있어야 한다",
                        field=f"fields[{index}].options",
                    )
                )
            elif field.default is not None:
                try:
                    _convert(field, field.default)
                except (ValueError, VariableError):
                    issues.append(
                        Issue(
                            level="error",
                            code="invalid_config",
                            message=f"{field.name}의 default가 필드 타입과 맞지 않는다",
                            field=f"fields[{index}].default",
                        )
                    )
        return issues

    async def run(
        self, ctx: NodeContext, config: StartConfig, state: dict[str, Any] | None
    ) -> NodeResult:
        try:
            values = coerce_inputs(config.fields, ctx.run_inputs)
        except InputError as exc:
            raise NodeFailure("invalid_inputs", f"런 입력이 올바르지 않다: {exc}") from None
        return NodeResult(status="succeeded", outputs=values, inputs=dict(ctx.run_inputs))
