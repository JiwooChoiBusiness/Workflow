"""에이전트 버전(초안·발행) 설정의 구조(설계 5.1 agent_version).

여기서는 값만으로 판단할 수 있는 규칙을 검사한다. 설정·모델 목록·DB에 따라 달라지는 규칙
(메모리·프로세스 상한, model_id, reasoning_effort, 스킬·파일 참조)은 app/services/agents.py가 본다.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.llm.types import ReasoningEffort
from app.schemas.common import ApprovalMode, RunLimits

ToolGroup = Literal["shell", "files", "http", "ask_user"]
TOOL_GROUPS: tuple[ToolGroup, ...] = ("shell", "files", "http", "ask_user")
DEFAULT_MAX_OUTPUT_TOKENS = 16000
MAX_INSTRUCTIONS_CHARS = 100_000
MAX_ENV_VARS = 100
MAX_ENV_VALUE_CHARS = 32 * 1024
# 스킬은 라이브러리 20개 + 임베디드 20개까지라 40개를 넘는 목록은 볼 것도 없다(설계 4.8).
MAX_SKILL_REFERENCES = 40
MAX_FILE_REFERENCES = 200

ModelId = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
]
EnvVarName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]


def _unique[T](values: list[T], what: str) -> list[T]:
    if len(set(values)) != len(values):
        raise ValueError(f"{what}에 같은 값이 두 번 들어 있다")
    return values


class ModelParamsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    # 상한은 모델 능력표(app/llm/catalog.py)의 max_output_tokens다.
    max_output_tokens: int = Field(default=DEFAULT_MAX_OUTPUT_TOKENS, ge=1)
    # 모델마다 받는 값이 다르다(catalog.reasoning_efforts). 비우면 모델 기본값.
    reasoning_effort: ReasoningEffort | None = None


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None이면 GEMINI_MODEL_FALLBACK의 첫 모델부터 쓴다.
    model_id: ModelId | None = None
    params: ModelParamsConfig = Field(default_factory=ModelParamsConfig)


class SandboxConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None이면 전역 값(SANDBOX_MEMORY_LIMIT_MB, SANDBOX_MAX_PROCESSES)을 쓴다.
    # 전역 값보다 클 수 없다.
    memory_limit_mb: int | None = Field(default=None, ge=1)
    max_processes: int | None = Field(default=None, ge=1)


class StoredEnvVar(BaseModel):
    # agent_version.env_vars JSON 항목. 값은 app/security/crypto.py로만 읽고 쓴다.
    name: str
    value_encrypted: str
    secret: bool = False


class EnvVarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: EnvVarName
    # None이면 같은 이름으로 저장된 값을 그대로 둔다. 비밀값은 응답에 없으므로 화면은 보내지 않는다.
    value: Annotated[str, StringConstraints(max_length=MAX_ENV_VALUE_CHARS)] | None = None
    secret: bool = False


class EnvVarOut(BaseModel):
    name: str
    secret: bool
    # 비밀값은 항상 null이다. 복호화하지 못한 값(ENCRYPTION_KEY 변경 등)도 null.
    value: str | None


class AgentVersionConfig(BaseModel):
    """초안과 발행 버전이 같이 갖는 설정. 환경변수는 입력과 출력의 모양이 달라 따로 둔다."""

    model_config = ConfigDict(extra="forbid")

    instructions: str = Field(default="", max_length=MAX_INSTRUCTIONS_CHARS)
    model: ModelConfig = Field(default_factory=ModelConfig)
    tools: list[ToolGroup] = Field(default_factory=lambda: list(TOOL_GROUPS))
    limits: RunLimits = Field(default_factory=RunLimits)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    approval_mode: ApprovalMode = "all_shell"
    skill_version_ids: list[uuid.UUID] = Field(
        default_factory=list[uuid.UUID], max_length=MAX_SKILL_REFERENCES
    )
    file_ids: list[uuid.UUID] = Field(
        default_factory=list[uuid.UUID], max_length=MAX_FILE_REFERENCES
    )

    @field_validator("tools")
    @classmethod
    def _unique_tools(cls, value: list[ToolGroup]) -> list[ToolGroup]:
        return _unique(value, "tools")

    @field_validator("skill_version_ids")
    @classmethod
    def _unique_skills(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        return _unique(value, "skill_version_ids")

    @field_validator("file_ids")
    @classmethod
    def _unique_files(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        return _unique(value, "file_ids")


class DraftUpdate(AgentVersionConfig):
    """PUT .../draft 본문. 초안 전체를 한 번에 바꾼다."""

    # 낙관적 잠금. GET .../draft에서 받은 updated_at을 그대로 보낸다. 다르면 409 draft_conflict.
    updated_at: AwareDatetime
    env_vars: list[EnvVarIn] = Field(default_factory=list[EnvVarIn], max_length=MAX_ENV_VARS)
    # true면 폴백 목록·라이브 목록에 없는 model_id도 받는다. 저장하지 않는 요청 옵션이다.
    allow_unlisted_model: bool = False

    @field_validator("env_vars")
    @classmethod
    def _unique_env_names(cls, value: list[EnvVarIn]) -> list[EnvVarIn]:
        # Windows 환경변수 이름은 대소문자를 가리지 않는다.
        _unique([item.name.upper() for item in value], "env_vars의 name(대소문자 무시)")
        return value


class AgentVersionOut(AgentVersionConfig):
    id: uuid.UUID
    agent_id: uuid.UUID
    # 초안은 null, 발행 버전은 1부터.
    version_number: int | None
    status: Literal["draft", "published"]
    env_vars: list[EnvVarOut]
    snapshot_id: uuid.UUID | None
    published_at: datetime | None
    change_note: str | None
    created_at: datetime
    # 초안 저장(PUT)의 낙관적 잠금 값.
    updated_at: datetime


class AgentVersionSummary(BaseModel):
    id: uuid.UUID
    version_number: int
    published_at: datetime | None
    change_note: str | None
    # agent.published_version_id가 이 버전인지.
    is_current: bool
    created_at: datetime


class AgentVersionList(BaseModel):
    items: list[AgentVersionSummary]
