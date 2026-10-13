"""작업공간 확보와 초기화(3.3). 작업공간을 만들고 채우는 일은 모두 여기를 거친다(13.2).

DB를 모르므로 workspace_instance 행의 정보는 호출하는 쪽이 넘긴다. 행이 없으면
instance_created_at=None, 있으면 지난번 ensure가 돌려준 created_at(폴더의 생성 시각)을 넘긴다.
폴더의 생성 시각이 그 값과 다르면 폴더가 밖에서 지워졌다가 새로 생긴 것이다.
첨부 파일·스킬 패키지가 storage 어디에 있는지는 ProvisionContent로 받는다(M5가 구현).
"""

import asyncio
import json
import re
import stat
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel

from app.sandbox.base import (
    ExecExited,
    ExecOutput,
    ExecSpec,
    OutputDecoder,
    RestoreInfo,
    SandboxBackend,
    SandboxError,
    WorkspaceHandle,
)
from app.sandbox.paths import split_relative
from app.sandbox.python_runner import PIP_TIMEOUT_SEC

if TYPE_CHECKING:
    import zipfile

COPY_CHUNK = 1024 * 1024
SKILL_MAX_BYTES = 2 * 1024**3
# workspace.json과 DB가 같은 시각을 마이크로초까지 저장하므로 직렬화 오차만 허용한다.
_SAME_INSTANCE_TOLERANCE = timedelta(milliseconds=1)
_SKILL_SLUG = re.compile(r"[a-z0-9-]{1,64}")
_WARNING_TAIL = 2000


class ProvisionEvent(BaseModel):
    type: Literal[
        "workspace.restoring",
        "workspace.restored",
        "workspace.restore_warning",
        "workspace.recreated",
    ]
    data: dict[str, Any] = {}


class SnapshotSource(BaseModel):
    snapshot_id: UUID
    path: Path
    sha256: str | None = None


class AgentFileSource(BaseModel):
    filename: str  # agent-files/ 아래 상대 경로
    path: Path  # storage 원본


class SkillSource(BaseModel):
    slug: str
    package: Path  # skill_version zip


class ProvisionContent(Protocol):
    """에이전트 버전의 첨부 파일과 스킬 패키지. storage를 아는 쪽이 구현한다."""

    async def agent_files(self) -> list[AgentFileSource]: ...

    async def skills(self) -> list[SkillSource]: ...


class ProvisionResult(BaseModel):
    handle: WorkspaceHandle
    state: Literal["created", "reused", "recreated"]
    # 폴더의 생성 시각(.platform/workspace.json). 저장해 두었다가 다음 ensure에 넘긴다.
    created_at: datetime | None
    restored_snapshot_id: UUID | None = None
    packages_reinstalled: int = 0
    agent_files: int = 0
    skills: int = 0
    events: list[ProvisionEvent] = []


EmitFn = Callable[[ProvisionEvent], Awaitable[None]]


def _validate_relative(rel: str, what: str) -> str:
    if "\\" in rel or any(part in ("", ".", "..") for part in rel.split("/")):
        raise SandboxError("path_invalid", f"{what} 경로가 올바르지 않다: {rel[:80]!r}")
    split_relative(rel, os_name="nt")
    return rel


def _as_utc(value: datetime) -> datetime:
    # DB가 시간대 없는 UTC 값을 돌려줘도 비교할 수 있게 맞춘다.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _skill_layout(names: list[str]) -> str:
    """SKILL.md가 루트에 있으면 '', 최상위 폴더 하나 안에 있으면 '<폴더>/'."""
    if "SKILL.md" in names:
        return ""
    tops = {name.split("/", 1)[0] for name in names}
    if len(tops) == 1:
        top = next(iter(tops))
        if f"{top}/SKILL.md" in names:
            return f"{top}/"
    raise SandboxError("skill_invalid", "스킬 패키지에 SKILL.md가 없다")


class WorkspaceProvisioner:
    def __init__(self, backend: SandboxBackend, *, pip_timeout_sec: int = PIP_TIMEOUT_SEC) -> None:
        self._backend = backend
        self._pip_timeout = pip_timeout_sec

    async def ensure(
        self,
        session_id: UUID,
        *,
        instance_created_at: datetime | None,
        snapshot: SnapshotSource | None = None,
        content: ProvisionContent | None = None,
        emit: EmitFn | None = None,
    ) -> ProvisionResult:
        handle = await self._backend.acquire(session_id)
        created_at = await self._folder_created_at(handle)
        known = instance_created_at is not None
        if (
            instance_created_at is not None
            and created_at is not None
            and abs(_as_utc(created_at) - _as_utc(instance_created_at)) <= _SAME_INSTANCE_TOLERANCE
        ):
            return ProvisionResult(handle=handle, state="reused", created_at=created_at)

        result = ProvisionResult(
            handle=handle, state="recreated" if known else "created", created_at=created_at
        )
        restored: RestoreInfo | None = None
        if known:
            # 폴더가 밖에서 지워졌다. 스냅샷은 다시 풀지 않는다(3.3).
            # 첨부와 스킬은 버전 설정에서 다시 채운다.
            await self._emit(result, emit, "workspace.recreated", {})
        elif snapshot is not None:
            await self._emit(
                result, emit, "workspace.restoring", {"snapshot_id": str(snapshot.snapshot_id)}
            )
            try:
                restored = await self._backend.restore(
                    handle, snapshot.path, expected_sha256=snapshot.sha256
                )
                result.restored_snapshot_id = snapshot.snapshot_id
            except SandboxError as exc:
                await self._emit(
                    result,
                    emit,
                    "workspace.restore_warning",
                    {"snapshot_id": str(snapshot.snapshot_id), "message": exc.message},
                )

        if content is not None:
            result.agent_files = await self.sync_agent_files(handle, await content.agent_files())
            result.skills = await self.sync_skills(handle, await content.skills())

        if restored is not None and snapshot is not None:
            data: dict[str, Any] = {"snapshot_id": str(snapshot.snapshot_id), "packages": 0}
            if restored.requirements:
                ok, message = await self.reinstall_packages(handle, restored.requirements)
                if not ok:
                    data["message"] = message
                    await self._emit(result, emit, "workspace.restore_warning", data)
                    return result
                result.packages_reinstalled = len(restored.requirements)
                data["packages"] = result.packages_reinstalled
            await self._emit(result, emit, "workspace.restored", data)
        return result

    async def reset(
        self,
        handle: WorkspaceHandle,
        *,
        snapshot: SnapshotSource | None = None,
        content: ProvisionContent | None = None,
        emit: EmitFn | None = None,
    ) -> ProvisionResult:
        """콘솔의 "작업공간 초기화". 지우고 처음 만들 때와 같은 절차를 밟는다."""
        await self._backend.release(handle, delete=True)
        return await self.ensure(
            handle.session_id,
            instance_created_at=None,
            snapshot=snapshot,
            content=content,
            emit=emit,
        )

    async def _folder_created_at(self, handle: WorkspaceHandle) -> datetime | None:
        try:
            info = await self._backend.read_file(handle, ".platform/workspace.json")
            return datetime.fromisoformat(json.loads(info.content)["created_at"])
        except (SandboxError, ValueError, KeyError, TypeError):
            return None

    async def _emit(
        self,
        result: ProvisionResult,
        emit: EmitFn | None,
        event_type: Literal[
            "workspace.restoring",
            "workspace.restored",
            "workspace.restore_warning",
            "workspace.recreated",
        ],
        data: dict[str, Any],
    ) -> None:
        event = ProvisionEvent(type=event_type, data=data)
        result.events.append(event)
        if emit is not None:
            await emit(event)

    async def _copy_stream(self, handle: WorkspaceHandle, rel: str, reader: IO[bytes]) -> int:
        written = 0
        first = True
        while True:
            chunk = await asyncio.to_thread(reader.read, COPY_CHUNK)
            # 빈 파일도 만들어야 하므로 첫 조각은 비어 있어도 쓴다.
            if chunk or first:
                written += await self._backend.write_file(handle, rel, chunk, append=not first)
            first = False
            if len(chunk) < COPY_CHUNK:
                return written

    async def copy_file_in(self, handle: WorkspaceHandle, rel: str, source: Path) -> int:
        """storage의 파일을 작업공간 rel로 복사한다. uploads·task 입력에도 쓴다."""
        with source.open("rb") as reader:
            return await self._copy_stream(handle, rel, reader)

    async def sync_agent_files(self, handle: WorkspaceHandle, files: list[AgentFileSource]) -> int:
        for item in files:
            rel = _validate_relative(item.filename.replace("\\", "/"), "첨부 파일")
            await self.copy_file_in(handle, f"agent-files/{rel}", item.path)
        return len(files)

    async def sync_skills(self, handle: WorkspaceHandle, skills: list[SkillSource]) -> int:
        for skill in skills:
            if not _SKILL_SLUG.fullmatch(skill.slug):
                raise SandboxError("skill_invalid", f"스킬 slug가 올바르지 않다: {skill.slug!r}")
            # zipfile은 스킬을 풀 때만 쓰므로 기동 메모리에 올리지 않는다(2.7).
            import zipfile

            archive = await asyncio.to_thread(zipfile.ZipFile, skill.package)
            try:
                await self._extract_skill(handle, skill.slug, archive)
            finally:
                archive.close()
        return len(skills)

    async def _extract_skill(
        self, handle: WorkspaceHandle, slug: str, archive: "zipfile.ZipFile"
    ) -> None:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if sum(info.file_size for info in infos) > SKILL_MAX_BYTES:
            raise SandboxError("skill_invalid", "스킬 패키지를 풀면 너무 크다")
        prefix = _skill_layout([info.filename for info in infos])
        for info in infos:
            if stat.S_ISLNK(info.external_attr >> 16):
                raise SandboxError("skill_invalid", f"심볼릭 링크 항목: {info.filename[:80]!r}")
            if not info.filename.startswith(prefix):
                continue
            rel = _validate_relative(info.filename[len(prefix) :], "스킬 항목")
            with archive.open(info) as source:
                await self._copy_stream(handle, f"skills/{slug}/{rel}", source)

    async def reinstall_packages(
        self, handle: WorkspaceHandle, requirements: list[str]
    ) -> tuple[bool, str]:
        """스냅샷의 설치 이력을 다시 설치한다.

        플랫폼이 발행 상태를 재현하는 일이라 승인을 받지 않는다(3.8).
        """
        spec = ExecSpec(
            exec_id=f"restore-{uuid4().hex[:12]}",
            kind="pip",
            packages=requirements,
            timeout_sec=self._pip_timeout,
        )
        decoder = OutputDecoder()
        tail: deque[str] = deque()
        tail_size = 0
        exited: ExecExited | None = None
        try:
            async for event in self._backend.exec(handle, spec):
                if isinstance(event, ExecOutput) and event.stream == "stderr":
                    text = decoder.feed(event.data)
                    tail.append(text)
                    tail_size += len(text)
                    while tail_size > _WARNING_TAIL and len(tail) > 1:
                        tail_size -= len(tail.popleft())
                elif isinstance(event, ExecExited):
                    exited = event
        except SandboxError as exc:
            return False, f"패키지를 다시 설치하지 못했다: {exc.message}"
        if exited is None or exited.code != 0:
            detail = "".join(tail)[-_WARNING_TAIL:].strip()
            reason = "시간 초과" if exited is not None and exited.timed_out else "설치 실패"
            return False, f"패키지 재설치 {reason}. {detail}".strip()
        return True, ""
