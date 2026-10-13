import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SkillPackageFile(BaseModel):
    # 스킬 루트(SKILL.md가 있는 폴더) 기준 상대 경로. 작업공간에서는 skills/<slug>/<path>다.
    path: str
    size: int


class SkillManifest(BaseModel):
    # skill_version.manifest JSON 열의 구조.
    name: str
    description: str
    version: str | None = None
    files: list[SkillPackageFile]


class SkillVersionSummary(BaseModel):
    id: uuid.UUID
    version_number: int
    size_bytes: int
    sha256: str
    created_at: datetime


class SkillVersionOut(SkillVersionSummary):
    skill_id: uuid.UUID
    manifest: SkillManifest
    skill_md: str


class SkillOut(BaseModel):
    id: uuid.UUID
    scope: Literal["library", "embedded"]
    # 임베디드 스킬만 값이 있다.
    agent_id: uuid.UUID | None
    slug: str
    name: str
    description: str
    current_version_id: uuid.UUID | None
    current_version_number: int | None
    created_at: datetime
    updated_at: datetime


class SkillDetailOut(SkillOut):
    versions: list[SkillVersionSummary]
    current_version: SkillVersionOut | None


class SkillList(BaseModel):
    items: list[SkillOut]
