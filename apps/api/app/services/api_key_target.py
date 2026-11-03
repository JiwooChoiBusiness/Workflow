"""API 키의 대상(2단계 설계 4.1, 6.3, 12장, 3단계 설계 8.5).

키는 에이전트, 워크플로우, 지식 베이스 중 하나에 묶인다(api_key.agent_id·workflow_id·
knowledge_base_id 중 정확히 하나). 1단계 코드는 agent_id가 늘 있다고 보고 쓰였으므로 행에서 대상을
읽고, 대상별 권한을 정하고, 대상이 맞지 않는 호출을 거절하는 일을 이 모듈 한 곳에 모은다. 지식
베이스 키의 서비스 API(datasets)는 3단계 M23에서 붙는다.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from app.db.models import ApiKey
from app.db.models.enums import WorkflowKind
from app.services.errors import ServiceError

KeyTargetKind = Literal["agent", "workflow", "knowledge"]
# 서비스 API가 고르는 대상. 워크플로우 키는 워크플로우의 kind로 workflow와 chatflow로 갈린다.
ApiTarget = Literal["agent", "workflow", "chatflow", "knowledge"]

_ALLOWED_SCOPES: dict[ApiTarget, frozenset[str]] = {
    "agent": frozenset({"chat", "tasks"}),
    "workflow": frozenset({"workflow"}),
    "chatflow": frozenset({"chat"}),
    "knowledge": frozenset({"knowledge"}),
}
_TARGET_NAMES: dict[ApiTarget, str] = {
    "agent": "에이전트",
    "workflow": "워크플로우",
    "chatflow": "챗플로우",
    "knowledge": "지식 베이스",
}
_TARGET_COLUMNS: dict[KeyTargetKind, str] = {
    "agent": "agent_id",
    "workflow": "workflow_id",
    "knowledge": "knowledge_base_id",
}


@dataclass(frozen=True)
class KeyTarget:
    kind: KeyTargetKind
    id: uuid.UUID

    @classmethod
    def agent(cls, agent_id: uuid.UUID) -> "KeyTarget":
        return cls("agent", agent_id)

    @classmethod
    def workflow(cls, workflow_id: uuid.UUID) -> "KeyTarget":
        return cls("workflow", workflow_id)

    @classmethod
    def knowledge(cls, knowledge_base_id: uuid.UUID) -> "KeyTarget":
        return cls("knowledge", knowledge_base_id)


def key_target(key: ApiKey) -> KeyTarget:
    """행이 가리키는 대상. CHECK가 셋 중 하나만 채워지게 막는다."""
    if key.knowledge_base_id is not None:
        return KeyTarget.knowledge(key.knowledge_base_id)
    if key.workflow_id is not None:
        return KeyTarget.workflow(key.workflow_id)
    if key.agent_id is None:
        raise ValueError(f"대상이 없는 API 키다: {key.id}")
    return KeyTarget.agent(key.agent_id)


def target_columns(target: KeyTarget) -> dict[str, uuid.UUID | None]:
    """새 행에 넣을 대상 열."""
    columns: dict[str, uuid.UUID | None] = dict.fromkeys(_TARGET_COLUMNS.values())
    columns[_TARGET_COLUMNS[target.kind]] = target.id
    return columns


def api_target(target: KeyTarget, flow_kind: str | None = None) -> ApiTarget:
    if target.kind == "agent":
        return "agent"
    if target.kind == "knowledge":
        return "knowledge"
    return "chatflow" if flow_kind == WorkflowKind.CHATFLOW else "workflow"


def default_scopes(target: KeyTarget, *, chatflow: bool = False) -> list[str]:
    """대상별 기본 권한(4.1). 에이전트는 chat·tasks, 워크플로우는 workflow, 챗플로우는 chat,
    지식 베이스는 knowledge다.
    """
    if target.kind == "agent":
        return ["chat", "tasks"]
    if target.kind == "knowledge":
        return ["knowledge"]
    return ["chat"] if chatflow else ["workflow"]


def allowed_scopes(target: ApiTarget) -> frozenset[str]:
    return _ALLOWED_SCOPES[target]


def wrong_target(actual: ApiTarget, *expected: ApiTarget) -> ServiceError:
    """대상에 맞지 않는 호출(6.3). 경로가 받는 대상과 키의 대상을 details에 싣는다."""
    names = "·".join(_TARGET_NAMES[item] for item in expected)
    return ServiceError(
        400,
        "wrong_target",
        f"이 경로는 {names} API 키로 부른다. 이 키는 {_TARGET_NAMES[actual]}에 묶여 있다",
        details={"key_target": actual, "expected": list(expected)},
    )
