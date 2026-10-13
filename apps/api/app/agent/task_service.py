"""과업(Agent Task) 실행 인터페이스(설계 4.10, 13.1).

2단계 워크플로우 엔진은 이 모듈만 import한다. 구현은 app/services/tasks.py의 TaskService이고
서비스 API POST /api/v1/agent-tasks는 그 얇은 HTTP 래퍼다. 여기에는 HTTP·FastAPI·DB를 모르는
타입과 과업 지시를 조립하는 순수 함수만 둔다.
"""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.agent.interactions import InteractionReply
from app.schemas.common import ApprovalMode, RunLimits
from app.schemas.events import RunEventOut, UsageTotal
from app.schemas.run import PendingInteractionOut, RunErrorOut
from app.schemas.task import TASK_INPUTS_PATH, SessionFileRef, SessionMode, TaskFile

FILE_ID_KEY = "file_id"
# 렌더링한 지시의 상한. 사용자 메시지 상한(app/schemas/session.py)과 같다.
MAX_INSTRUCTION_CHARS = 100_000
MAX_OUTPUT_SCHEMA_BYTES = 64 * 1024
_SUMMARY_VALUE_CHARS = 500

TaskEndStatus = Literal["succeeded", "failed", "cancelled", "waiting_input"]
# 승인·질문 응답. 콘솔·서비스 API의 /input 본문과 같은 모양이다.
InteractionInput = InteractionReply


class AgentTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    agent_id: UUID
    # None이면 발행 버전.
    agent_version_id: UUID | None = None
    # 변수명 → 값. 파일은 {"file_id": "<session_file id>"}로 준다.
    inputs: dict[str, Any] = Field(default_factory=dict[str, Any])
    # {{변수}} 템플릿(Jinja2 SandboxedEnvironment).
    instruction: str
    # JSON Schema(type: object, additionalProperties: false). 없으면 결과는 text만.
    output_schema: dict[str, Any] | None = None
    # task/files/로 복사할 session_file id.
    files: list[UUID] = Field(default_factory=list[UUID])
    # 준 필드만 버전의 limits를 덮어쓴다.
    limits: RunLimits | None = None
    # None이면 버전의 승인 모드.
    approval_mode: ApprovalMode | None = None
    # ephemeral: 새 세션에서 돌고 끝나면 작업공간을 지운다. reuse: 작업공간을 남긴다.
    session_mode: SessionMode = "ephemeral"
    # reuse일 때 이어 쓸 과업 세션. 비우면 새 세션을 만든다(응답의 session_id로 이어 쓴다).
    session_id: UUID | None = None
    external_user: str | None = None
    # workflow_run_id, node_id 등. run.input.correlation에 남는다.
    correlation: dict[str, Any] = Field(default_factory=dict[str, Any])


class AgentTaskResult(BaseModel):
    run_id: UUID
    session_id: UUID
    status: TaskEndStatus
    # output_schema 검증을 통과한 JSON. 스키마가 없거나 실패했으면 None.
    result: dict[str, Any] | None = None
    # 이 런의 마지막 어시스턴트 글.
    text: str = ""
    files: list[SessionFileRef] = Field(default_factory=list[SessionFileRef])
    usage: UsageTotal = Field(default_factory=UsageTotal)
    error: RunErrorOut | None = None
    # waiting_input이면 승인·질문 내용(2단계 HITL).
    pending_interaction: PendingInteractionOut | None = None


class AgentTaskService(Protocol):
    async def start(self, spec: AgentTaskSpec) -> UUID:
        """과업 런을 큐에 넣고 run_id를 바로 돌려준다."""
        ...

    def events(self, run_id: UUID, after: int | None = None) -> AsyncIterator[RunEventOut]:
        """after 다음부터의 런 이벤트. 런이 끝나거나 입력을 기다리면 끝난다."""
        ...

    async def wait(self, run_id: UUID, timeout: float | None = None) -> AgentTaskResult:
        """끝나거나 입력을 기다릴 때까지 기다린다. 시간이 다 되면 TimeoutError."""
        ...

    async def provide_input(self, run_id: UUID, payload: InteractionInput) -> None: ...

    async def cancel(self, run_id: UUID) -> None: ...


class TaskSpecError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def file_reference(value: object) -> UUID | None:
    """{"file_id": "<uuid>"} 모양이면 그 id."""
    if not isinstance(value, dict):
        return None
    item = cast(dict[str, Any], value)
    if set(item) != {FILE_ID_KEY}:
        return None
    try:
        return UUID(str(item[FILE_ID_KEY]))
    except ValueError:
        raise TaskSpecError(
            "file_not_found", f"올바른 file_id가 아니다: {item[FILE_ID_KEY]!r}"
        ) from None


def check_output_schema(schema: Mapping[str, Any]) -> None:
    if schema.get("type") != "object":
        raise TaskSpecError("output_schema_invalid", "output_schema의 type은 object여야 한다")
    if schema.get("additionalProperties") is not False:
        raise TaskSpecError(
            "output_schema_invalid", "output_schema에는 additionalProperties: false가 있어야 한다"
        )
    if len(json.dumps(schema, ensure_ascii=False).encode("utf-8")) > MAX_OUTPUT_SCHEMA_BYTES:
        raise TaskSpecError(
            "output_schema_invalid", f"output_schema는 {MAX_OUTPUT_SCHEMA_BYTES // 1024}KB 이하다"
        )
    # jsonschema는 과업을 처음 받을 때 불러온다(2.7).
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    from jsonschema.validators import validator_for

    try:
        validator_for(dict(schema), default=Draft202012Validator).check_schema(dict(schema))
    except SchemaError as exc:
        raise TaskSpecError(
            "output_schema_invalid", f"JSON Schema가 아니다: {exc.message}"
        ) from None


def render_instruction(template: str, context: Mapping[str, Any]) -> str:
    """{{변수}}를 채운다. 없는 변수는 오류다(오타를 빈칸으로 넘기지 않는다)."""
    # Jinja2는 과업을 처음 받을 때 불러온다(2.7).
    from jinja2 import StrictUndefined
    from jinja2.sandbox import SandboxedEnvironment

    environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    try:
        rendered = environment.from_string(template).render(dict(context))
    except Exception as exc:
        raise TaskSpecError(
            "instruction_invalid", f"instruction을 채우지 못했다: {type(exc).__name__}: {exc}"
        ) from None
    if not rendered.strip():
        raise TaskSpecError("instruction_invalid", "채운 instruction이 비어 있다")
    if len(rendered) > MAX_INSTRUCTION_CHARS:
        raise TaskSpecError(
            "instruction_invalid", f"채운 instruction이 {MAX_INSTRUCTION_CHARS}자를 넘는다"
        )
    return rendered


def _summary(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= _SUMMARY_VALUE_CHARS:
        return text
    return f"{text[:_SUMMARY_VALUE_CHARS]}... ({len(text)}자 중 앞부분)"


def task_message(instruction: str, inputs: Mapping[str, Any], files: Sequence[TaskFile]) -> str:
    """과업 런의 첫 사용자 메시지. 채운 지시, 입력 값 요약, 입력 파일 경로 순서다."""
    parts = [instruction.strip()]
    if inputs:
        lines = [f"입력 값(전체는 {TASK_INPUTS_PATH}):"]
        lines.extend(f"- {name}: {_summary(value)}" for name, value in inputs.items())
        parts.append("\n".join(lines))
    if files:
        lines = ["입력 파일(작업공간 상대 경로):"]
        lines.extend(f"- {item.path} ({item.size_bytes}바이트, {item.mime})" for item in files)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
