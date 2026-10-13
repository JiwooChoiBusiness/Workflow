import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.common import ApprovalMode, ModelPrice, RunLimits

WorkspaceName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]


class WorkspaceSettings(BaseModel):
    # workspace.settings JSON 열의 구조.
    model_config = ConfigDict(extra="forbid")

    default_limits: RunLimits = Field(default_factory=RunLimits)
    approval_mode_default: ApprovalMode = "all_shell"
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)
    run_event_retention_days: int = Field(default=90, ge=1, le=3650)


class WorkspaceSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class WorkspaceOut(WorkspaceSummary):
    settings: WorkspaceSettings
    created_at: datetime
    updated_at: datetime


class WorkspacePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: WorkspaceName | None = None
    # 설정은 통째로 바꾼다. 화면은 GET으로 받은 전체 값을 고쳐서 보낸다.
    settings: WorkspaceSettings | None = None
