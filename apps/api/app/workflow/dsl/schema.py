# pyright: strict
"""우리 DSL 형식(2단계 설계 5.1).

YAML 파일 하나에 워크플로우 하나를 담는다. graph는 wf_version.graph와 같은 모양이라 저장 형식과
DSL 사이에 변환 코드가 없다. 다른 점은 둘이다. agent 노드 config에 agent_slug를 더 쓰고
(다른 PC에서는 agent_id가 뜻이 없다), 비밀 환경 변수는 include_secrets로 내보낼 때만 값을 쓴다.

형식이 바뀌면 DSL_VERSION을 올리고 import_.py의 DSL_UPGRADES에 변환기를 둔다.
"""

from collections.abc import Mapping
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from app.schemas.agent import Description, Icon
from app.schemas.agent_version import MAX_ENV_VALUE_CHARS, MAX_ENV_VARS, EnvVarName
from app.schemas.workflow import (
    MAX_CONVERSATION_VARS,
    ConversationVar,
    Graph,
    WorkflowKind,
    WorkflowName,
    WorkflowSettings,
)

DSL_VERSION = 1
MAX_DSL_BYTES = 2 * 1024 * 1024
# 내보낼 때 agent 노드 config에 더하는 키. 가져올 때 이 slug로 에이전트를 찾고 키는 지운다.
AGENT_SLUG_KEY = "agent_slug"
MAX_ERROR_DETAILS = 50


class DslError(Exception):
    """가져오기를 거부하는 문제. 서비스가 422로 바꾼다."""

    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class DslEnvVar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: EnvVarName
    # 비밀값은 include_secrets로 내보냈을 때만 있다. 없으면 빈 값으로 들어오고 화면이 값을 받는다.
    value: Annotated[str, StringConstraints(max_length=MAX_ENV_VALUE_CHARS)] | None = None
    secret: bool = False


class DslDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dsl_version: int = Field(ge=1)
    kind: WorkflowKind
    name: WorkflowName
    icon: Icon | None = None
    description: Description = ""
    env_vars: list[DslEnvVar] = Field(default_factory=list[DslEnvVar], max_length=MAX_ENV_VARS)
    conversation_vars: list[ConversationVar] = Field(
        default_factory=list[ConversationVar], max_length=MAX_CONVERSATION_VARS
    )
    settings: WorkflowSettings = Field(default_factory=WorkflowSettings)
    graph: Graph = Field(default_factory=Graph)

    @field_validator("env_vars")
    @classmethod
    def _unique_env(cls, value: list[DslEnvVar]) -> list[DslEnvVar]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("env_vars에 같은 이름이 두 번 들어 있다")
        return value

    @field_validator("conversation_vars")
    @classmethod
    def _unique_conversation(cls, value: list[ConversationVar]) -> list[ConversationVar]:
        names = [item.name for item in value]
        if len(set(names)) != len(names):
            raise ValueError("conversation_vars에 같은 이름이 두 번 들어 있다")
        return value


def validate_document(data: Mapping[str, Any]) -> DslDocument:
    """형식이 맞지 않으면 DslError(invalid_dsl). details는 {loc, msg, type} 목록이다."""
    try:
        return DslDocument.model_validate(dict(data))
    except ValidationError as exc:
        details = [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()[:MAX_ERROR_DETAILS]
        ]
        raise DslError("invalid_dsl", "DSL 내용이 형식에 맞지 않는다", details=details) from None
