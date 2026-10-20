import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

# 에이전트 키는 chat·tasks, 워크플로우 키는 workflow, 챗플로우 키는 chat만 받는다(2단계 4.1).
ApiKeyScope = Literal["chat", "tasks", "workflow"]


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    # 비우면 대상의 기본값(에이전트 chat·tasks, 워크플로우 workflow, 챗플로우 chat).
    scopes: Annotated[list[ApiKeyScope], Field(min_length=1)] | None = None
    # 비우면 만료 없음.
    expires_at: AwareDatetime | None = None


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    # 키가 묶인 대상. 에이전트 키는 agent_id, 워크플로우 키는 workflow_id만 있다.
    agent_id: uuid.UUID | None
    workflow_id: uuid.UUID | None = None
    name: str
    # 목록에서 키를 알아보는 앞부분(sk-wf-로 시작하는 12자). 원문은 생성 응답에만 있다.
    key_prefix: str
    scopes: list[str]
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    # 키 원문. 이 응답 말고는 어디에서도 다시 볼 수 없다.
    key: str


class ApiKeyList(BaseModel):
    items: list[ApiKeyOut]
