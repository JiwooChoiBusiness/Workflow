"""ProvisionContent 구현(3.3). storage 키를 로컬 경로로 바꿔 WorkspaceProvisioner에 넘긴다.

app.sandbox는 DB를 모르므로(13.2) 어떤 파일·스킬이 버전에 들어 있는지는 부르는 쪽
(app/services/agents.py의 load_runtime_version)이 정해서 키 목록으로 준다.
"""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from app.sandbox.base import SandboxError
from app.sandbox.provisioning import AgentFileSource, SkillSource
from app.storage.base import StorageBackend, StorageError


class AgentFileRef(BaseModel):
    filename: str  # agent-files/ 아래 상대 경로
    storage_key: str


class SkillPackageRef(BaseModel):
    slug: str  # skills/<slug>/
    storage_key: str


class StorageProvisionContent:
    def __init__(
        self,
        storage: StorageBackend,
        *,
        files: Sequence[AgentFileRef] = (),
        skills: Sequence[SkillPackageRef] = (),
    ) -> None:
        self._storage = storage
        self._files = list(files)
        self._skills = list(skills)

    def _path(self, key: str, what: str) -> Path:
        try:
            path = self._storage.local_path(key)
        except StorageError as exc:
            raise SandboxError(
                "storage_missing", f"{what}의 저장 위치가 올바르지 않다: {exc}"
            ) from exc
        # 원본이 없으면 복사 도중 OSError가 아니라 원인이 분명한 오류를 낸다.
        if not path.is_file():
            raise SandboxError("storage_missing", f"{what} 원본이 저장소에 없다: {key}")
        return path

    async def agent_files(self) -> list[AgentFileSource]:
        return [
            AgentFileSource(filename=item.filename, path=self._path(item.storage_key, "첨부 파일"))
            for item in self._files
        ]

    async def skills(self) -> list[SkillSource]:
        return [
            SkillSource(slug=item.slug, package=self._path(item.storage_key, "스킬 패키지"))
            for item in self._skills
        ]
