"""E2B 백엔드 자리(3.15). 같은 프로토콜을 맞춰 두기만 하고 동작은 구현하지 않는다.

구현할 때 매핑: acquire→Sandbox.create/connect, exec→commands.run(background)+stdout 콜백,
read/write/list→files.*, snapshot/restore→파일 다운로드·업로드 zip, stats→sandbox.get_metrics.
"""

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from uuid import UUID

from app.config import Settings
from app.sandbox.base import (
    ExecEvent,
    ExecSpec,
    FileChanges,
    FileContent,
    FileEntry,
    FileStat,
    HttpResult,
    HttpSpec,
    RestoreInfo,
    SnapshotInfo,
    WorkspaceHandle,
    WorkspaceStats,
)

NOT_IMPLEMENTED = "E2B backend is not implemented yet"


class E2BBackend:
    name = "e2b"

    def __init__(self, settings: Settings) -> None:
        if settings.e2b_api_key is None or not settings.e2b_api_key.get_secret_value():
            raise ValueError("E2B_API_KEY가 비어 있다")
        self.api_key = settings.e2b_api_key
        self.template = settings.e2b_template

    async def acquire(self, session_id: UUID) -> WorkspaceHandle:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def release(self, h: WorkspaceHandle, *, delete: bool) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def exec(self, h: WorkspaceHandle, spec: ExecSpec) -> AsyncIterator[ExecEvent]:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def kill(self, h: WorkspaceHandle, exec_id: str) -> bool:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def read_file(
        self, h: WorkspaceHandle, path: str, offset: int = 0, limit: int = 65536
    ) -> FileContent:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def write_file(
        self,
        h: WorkspaceHandle,
        path: str,
        data: bytes,
        *,
        append: bool = False,
        mkdirs: bool = True,
    ) -> int:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def list_files(
        self,
        h: WorkspaceHandle,
        path: str = ".",
        *,
        recursive: bool = False,
        max_entries: int = 500,
        include_hidden: bool = False,
    ) -> list[FileEntry]:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def stat(self, h: WorkspaceHandle, path: str) -> FileStat:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def changes(
        self, h: WorkspaceHandle, since: datetime, *, max_entries: int = 1000
    ) -> FileChanges:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def http_request(self, h: WorkspaceHandle, spec: HttpSpec) -> HttpResult:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def snapshot(self, h: WorkspaceHandle, dest: Path) -> SnapshotInfo:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def restore(
        self, h: WorkspaceHandle, src: Path, *, expected_sha256: str | None = None
    ) -> RestoreInfo:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def stats(self, h: WorkspaceHandle) -> WorkspaceStats:
        raise NotImplementedError(NOT_IMPLEMENTED)

    async def purge_deleted(self) -> int:
        # 이 PC에 남는 작업공간 폴더가 없어 지울 것도 없다.
        return 0

    async def aclose(self) -> None:
        return None
