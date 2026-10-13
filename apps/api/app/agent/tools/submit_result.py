"""과업 모드의 결과 제출 도구(설계 4.4, 4.10).

과업의 출력 스키마를 모델에는 이 도구의 입력 스키마로 보여 주고, 실제 검증은 execute가 전체 스키마로
한다. 루프의 공통 입력 검사(validate_tool_input)는 객체인지만 본다. 스키마에 맞지 않으면 오류 결과로
고쳐 내게 하고, MAX_SUBMIT_ATTEMPTS번 모두 맞지 않으면 런을 output_invalid로 끝낸다. 맞으면 그 값이
run.output.result가 되고 런이 끝난다(terminal).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from app.agent.tools.base import Tool, ToolContext, ToolResult, model_schema
from app.llm.types import ToolSpec

if TYPE_CHECKING:
    from jsonschema.protocols import Validator

SUBMIT_RESULT = "submit_result"
MAX_SUBMIT_ATTEMPTS = 3
OUTPUT_INVALID = "output_invalid"
_MAX_REPORTED = 5


def schema_validator(schema: Mapping[str, Any]) -> "Validator":
    """스키마의 $schema에 맞는 검증기. 없으면 2020-12."""
    # jsonschema는 과업을 처음 받을 때 불러온다(2.7).
    from jsonschema import Draft202012Validator
    from jsonschema.validators import validator_for

    cls = validator_for(dict(schema), default=Draft202012Validator)
    return cls(dict(schema))


def schema_problems(validator: "Validator", data: Any) -> list[str]:
    problems: list[str] = []
    for error in validator.iter_errors(data):
        location = "/".join(str(part) for part in error.absolute_path) or "(결과)"
        problems.append(f"{location}: {error.message}")
        if len(problems) >= _MAX_REPORTED:
            break
    return sorted(problems)


class SubmitResultTool(Tool):
    name = SUBMIT_RESULT
    group = "system"
    terminal = True
    description = (
        "과업 결과를 제출한다. 작업을 모두 마친 뒤 마지막에 한 번 부른다. 입력은 과업의 출력 "
        "스키마를 그대로 따른다. 스키마에 맞지 않으면 오류가 돌아오므로 고쳐서 다시 부른다. "
        "제출이 받아들여지면 과업이 끝난다."
    )
    # 루프의 공통 검사용. 출력 스키마 검증은 execute가 한다.
    input_schema = {"type": "object"}

    def __init__(
        self, output_schema: Mapping[str, Any], *, max_attempts: int = MAX_SUBMIT_ATTEMPTS
    ) -> None:
        self.output_schema = dict(output_schema)
        self.max_attempts = max_attempts
        self.failures = 0
        self._validator: Validator | None = None

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=model_schema(self.output_schema),
        )

    def validator(self) -> "Validator":
        if self._validator is None:
            self._validator = schema_validator(self.output_schema)
        return self._validator

    async def execute(self, ctx: ToolContext, data: dict[str, Any]) -> ToolResult:
        problems = schema_problems(self.validator(), data)
        if not problems:
            result = ToolResult.ok("결과를 받았다. 과업을 마친다.")
            result.output = data
            return result
        self.failures += 1
        detail = "; ".join(problems)
        left = self.max_attempts - self.failures
        if left <= 0:
            result = ToolResult.error(
                f"INVALID_OUTPUT: {detail}. 출력 스키마에 맞는 결과를 {self.max_attempts}번 안에 "
                "내지 못해 과업을 실패로 끝낸다.",
                code=OUTPUT_INVALID,
            )
            result.fatal = True
            return result
        return ToolResult.error(
            f"INVALID_OUTPUT: {detail}. 출력 스키마에 맞게 고쳐 다시 제출한다(남은 기회 {left}번).",
            code=OUTPUT_INVALID,
        )
