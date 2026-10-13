import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

ApiKeyScope = Literal["chat", "tasks"]


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    scopes: list[ApiKeyScope] = Field(default_factory=lambda: ["chat", "tasks"], min_length=1)
    # 비우면 만료 없음.
    expires_at: AwareDatetime | None = None


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
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
