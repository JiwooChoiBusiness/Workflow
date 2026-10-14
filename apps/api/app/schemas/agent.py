import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.agent_version import AgentVersionOut, AgentWarning

# 웹앱 주소(/apps/<slug>)에 쓰인다. 스킬 이름과 같은 규칙.
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,64}$")]
AgentName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
# 이모지나 아이콘 이름. 화면이 해석한다.
Icon = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: AgentName
    # 비우면 이름에서 만든다. 이름이 한글뿐이면 agent, agent-2처럼 붙인다.
    slug: Slug | None = None
    icon: Icon | None = None
    description: Description = ""


class AgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: AgentName | None = None
    slug: Slug | None = None
    # null을 보내면 아이콘을 지운다. 보내지 않으면 그대로 둔다.
    icon: Icon | None = None
    description: Description | None = None
    webapp_enabled: bool | None = None


class AgentOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    icon: str | None
    description: str
    webapp_enabled: bool
    draft_version_id: uuid.UUID | None
    published_version_id: uuid.UUID | None
    published_version_number: int | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentList(BaseModel):
    items: list[AgentOut]


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_note: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None
    ) = None


class SnapshotOut(BaseModel):
    """발행 스냅샷(3.12). 새 웹앱·API 세션의 작업공간이 이 zip에서 시작한다."""

    id: uuid.UUID
    file_count: int
    # zip 크기와 압축 전 합계.
    size_bytes: int
    raw_bytes: int
    # 미리보기 작업공간이 없어 직전 발행 버전의 스냅샷을 이어받았다.
    inherited: bool
    # 스냅샷을 뜬 미리보기 세션. 이어받았으면 원래 스냅샷의 세션.
    session_id: uuid.UUID | None
    created_at: datetime


class PublishOut(BaseModel):
    agent: AgentOut
    version: AgentVersionOut
    # 미리보기 작업공간도, 이어받을 스냅샷도 없으면 null.
    snapshot: SnapshotOut | None = None
    # 발행은 막지 않는 하위 에이전트 문제(설명 없음, 사슬 깊이 초과).
    warnings: list[AgentWarning] = Field(default_factory=list[AgentWarning])
