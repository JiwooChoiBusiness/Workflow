"""API 키의 대상(2단계 설계 4.1, 6.3, 12장).

키는 에이전트나 워크플로우 하나에 묶인다(api_key.agent_id·workflow_id 중 정확히 하나). 1단계
코드는 agent_id가 늘 있다고 보고 쓰였으므로 행에서 대상을 읽고, 대상별 권한을 정하고, 대상이 맞지
않는 호출을 거절하는 일을 이 모듈 한 곳에 모은다.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from app.db.models import ApiKey
from app.db.models.enums import WorkflowKind
from app.services.errors import ServiceError

KeyTargetKind = Literal["agent", "workflow"]
# 서비스 API가 고르는 대상. 워크플로우 키는 워크플로우의 kind로 workflow와 chatflow로 갈린다.
ApiTarget = Literal["agent", "workflow", "chatflow"]

_ALLOWED_SCOPES: dict[ApiTarget, frozenset[str]] = {
    "agent": frozenset({"chat", "tasks"}),
    "workflow": frozenset({"workflow"}),
    "chatflow": frozenset({"chat"}),
}
_TARGET_NAMES: dict[ApiTarget, str] = {
    "agent": "에이전트",
    "workflow": "워크플로우",
    "chatflow": "챗플로우",
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


def key_target(key: ApiKey) -> KeyTarget:
    """행이 가리키는 대상. CHECK가 둘 중 하나만 채워지게 막는다."""
    if key.workflow_id is not None:
        return KeyTarget.workflow(key.workflow_id)
    if key.agent_id is None:
        raise ValueError(f"대상이 없는 API 키다: {key.id}")
    return KeyTarget.agent(key.agent_id)


def target_columns(target: KeyTarget) -> dict[str, uuid.UUID | None]:
    """새 행에 넣을 대상 열."""
    if target.kind == "agent":
        return {"agent_id": target.id, "workflow_id": None}
    return {"agent_id": None, "workflow_id": target.id}


def api_target(target: KeyTarget, flow_kind: str | None = None) -> ApiTarget:
    if target.kind == "agent":
        return "agent"
    return "chatflow" if flow_kind == WorkflowKind.CHATFLOW else "workflow"


def default_scopes(target: KeyTarget, *, chatflow: bool = False) -> list[str]:
    """대상별 기본 권한(4.1). 에이전트는 chat·tasks, 워크플로우는 workflow, 챗플로우는 chat이다."""
    if target.kind == "agent":
        return ["chat", "tasks"]
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
