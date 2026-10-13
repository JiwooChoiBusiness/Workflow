import codecs
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel


class SandboxError(Exception):
    """도구 오류로 모델에 돌려줄 수 있는 실패. code는 고정 식별자, message는 설명이다."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class WorkspaceHandle(BaseModel):
    session_id: UUID
    root: Path  # 작업공간 절대 경로 (resolve 완료)
    backend: Literal["local", "e2b"]


class ExecSpec(BaseModel):
    exec_id: str  # tool_call_id
    kind: Literal["shell", "python", "pip"]
    script: str = ""  # shell: 명령 텍스트, python: 코드
    packages: list[str] = []  # pip 전용
    cwd: str = "."  # 작업공간 상대 경로
    env: dict[str, str] = {}  # 에이전트 환경변수(복호화됨)
    timeout_sec: int = 120
    stdin: str | None = None
    # 에이전트 버전 sandbox 설정. 전역 상한보다 크면 전역 값을 쓴다(3.7).
    memory_limit_mb: int | None = None
    max_processes: int | None = None


class ExecStarted(BaseModel):
    exec_id: str
    pid: int


class ExecOutput(BaseModel):
    exec_id: str
    stream: Literal["stdout", "stderr"]
    data: bytes


class ExecExited(BaseModel):
    exec_id: str
    code: int | None
    timed_out: bool
    killed: bool
    duration_ms: int
    peak_memory_bytes: int | None


ExecEvent = ExecStarted | ExecOutput | ExecExited

EntryType = Literal["file", "dir", "link"]


class FileContent(BaseModel):
    content: str
    encoding: Literal["utf-8", "base64"]
    size: int
    truncated: bool
    next_offset: int | None


class FileEntry(BaseModel):
    path: str  # 작업공간 상대 경로, 구분자 '/'
    type: EntryType
    size: int
    mtime: datetime


class FileStat(BaseModel):
    type: EntryType
    size: int
    mtime: datetime


class FileChanges(BaseModel):
    entries: list[FileEntry]
    # 시간 예산이나 max_entries에 걸려 다 훑지 못했다(run.completed.changes_truncated).
    truncated: bool


class HttpSpec(BaseModel):
    exec_id: str
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    url: str
    headers: dict[str, str] = {}
    body: str | None = None
    timeout_sec: int = 60


class HttpResult(BaseModel):
    status: int
    url: str  # 리다이렉트를 따라간 마지막 URL
    headers: dict[str, str]
    content_type: str | None
    body: str | None  # 텍스트 응답의 앞부분(최대 64KB). 바이너리와 HEAD는 None
    body_bytes: int
    truncated: bool
    saved_path: str | None  # 본문을 저장한 작업공간 상대 경로(downloads/...)
    redirects: list[str]


class SnapshotInfo(BaseModel):
    raw_bytes: int
    size_bytes: int
    file_count: int
    sha256: str


class RestoreInfo(BaseModel):
    file_count: int
    raw_bytes: int
    sha256: str
    requirements: list[str]  # 복원된 .platform/requirements.txt. 비어 있지 않으면 재설치한다


class RunningExec(BaseModel):
    exec_id: str
    kind: Literal["shell", "python", "pip"]
    pid: int
    started_at: datetime
    elapsed_ms: int
    peak_memory_bytes: int | None
    active_processes: int | None


class WorkspaceStats(BaseModel):
    disk_bytes: int
    disk_estimated: bool
    active_processes: int
    last_exec_at: datetime | None
    running: list[RunningExec] = []


class SandboxBackend(Protocol):
    name: str

    async def acquire(self, session_id: UUID) -> WorkspaceHandle: ...

    async def release(self, h: WorkspaceHandle, *, delete: bool) -> None: ...

    def exec(self, h: WorkspaceHandle, spec: ExecSpec) -> AsyncIterator[ExecEvent]: ...

    async def kill(self, h: WorkspaceHandle, exec_id: str) -> bool: ...

    async def read_file(
        self, h: WorkspaceHandle, path: str, offset: int = 0, limit: int = 65536
    ) -> FileContent: ...

    async def write_file(
        self,
        h: WorkspaceHandle,
        path: str,
        data: bytes,
        *,
        append: bool = False,
        mkdirs: bool = True,
    ) -> int: ...

    async def list_files(
        self,
        h: WorkspaceHandle,
        path: str = ".",
        *,
        recursive: bool = False,
        max_entries: int = 500,
        include_hidden: bool = False,
    ) -> list[FileEntry]: ...

    async def stat(self, h: WorkspaceHandle, path: str) -> FileStat: ...

    async def changes(
        self, h: WorkspaceHandle, since: datetime, *, max_entries: int = 1000
    ) -> FileChanges: ...

    async def http_request(self, h: WorkspaceHandle, spec: HttpSpec) -> HttpResult: ...

    async def snapshot(self, h: WorkspaceHandle, dest: Path) -> SnapshotInfo: ...

    async def restore(
        self, h: WorkspaceHandle, src: Path, *, expected_sha256: str | None = None
    ) -> RestoreInfo: ...

    async def stats(self, h: WorkspaceHandle) -> WorkspaceStats: ...

    # 하우스키핑이 매시 부른다. 잠겨 있어 이름만 바꿔 둔 작업공간을 다시 지우고 지운 개수를 준다.
    async def purge_deleted(self) -> int: ...

    # 서버 종료 때 부른다. 남은 도구 프로세스를 끝내고 HTTP 클라이언트를 닫는다.
    async def aclose(self) -> None: ...


class OutputDecoder:
    """ExecOutput 바이트 조각을 텍스트로 바꾼다.

    조각 경계에서 잘린 UTF-8 문자와 CR LF를 다음 조각까지 들고 있다가 이어 붙인다.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending_cr = False

    def feed(self, data: bytes) -> str:
        return self._normalize(self._decoder.decode(data), final=False)

    def flush(self) -> str:
        return self._normalize(self._decoder.decode(b"", final=True), final=True)

    def _normalize(self, text: str, *, final: bool) -> str:
        if self._pending_cr:
            text = "\r" + text
            self._pending_cr = False
        if not final and text.endswith("\r"):
            text = text[:-1]
            self._pending_cr = True
        return text.replace("\r\n", "\n")
