"""파일 저장소(설계 2.8). 키는 data/storage 아래 상대 경로이고 구분자는 '/'다.

DB의 storage_key 열에는 이 키를 그대로 저장한다. 키를 만드는 규칙은 아래 함수들 한 곳에 둔다.
"""

from collections.abc import AsyncIterable, AsyncIterator
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel

CHUNK_SIZE = 1024 * 1024
MAX_KEY_LENGTH = 400
_SAFE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")


class StorageError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class StorageLimitExceeded(StorageError):
    def __init__(self, limit: int) -> None:
        super().__init__("too_large", f"{limit}바이트를 넘는다")
        self.limit = limit


class StoredObject(BaseModel):
    key: str
    size_bytes: int
    sha256: str


class StorageBackend(Protocol):
    async def write_stream(
        self, key: str, chunks: AsyncIterable[bytes], *, max_bytes: int | None = None
    ) -> StoredObject:
        """임시 파일에 받은 뒤 이름을 바꿔 넣는다. max_bytes를 넘으면 StorageLimitExceeded."""
        ...

    async def write_file(
        self, key: str, source: Path, *, max_bytes: int | None = None
    ) -> StoredObject: ...

    def read_stream(self, key: str, *, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]: ...

    async def size(self, key: str) -> int | None:
        """없으면 None."""
        ...

    async def delete(self, key: str) -> bool: ...

    def local_path(self, key: str) -> Path:
        """키의 실제 파일 경로. zip을 열거나 작업공간으로 복사할 때 쓴다.

        1단계 저장소는 로컬 폴더뿐이다. 원격 저장소를 붙이면 내려받은 캐시 경로를 돌려준다.
        """
        ...


def check_key(key: str) -> tuple[str, ...]:
    """키를 구성 요소로 나눈다. 저장소 폴더 밖을 가리킬 수 있는 키는 받지 않는다."""
    if not key or len(key) > MAX_KEY_LENGTH:
        raise StorageError("key_invalid", f"키 길이가 맞지 않다: {key[:80]!r}")
    if "\\" in key or ":" in key or any(ord(char) < 0x20 for char in key):
        raise StorageError("key_invalid", f"쓸 수 없는 문자가 있다: {key[:80]!r}")
    parts = tuple(key.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise StorageError("key_invalid", f"상대 경로가 아니다: {key[:80]!r}")
    return parts


def agent_file_key(workspace_id: UUID, file_id: UUID) -> str:
    return f"agent-files/{workspace_id}/{file_id}"


def skill_package_key(workspace_id: UUID, skill_version_id: UUID) -> str:
    return f"skills/{workspace_id}/{skill_version_id}.zip"


def upload_key(session_id: UUID, file_id: UUID) -> str:
    return f"uploads/{session_id}/{file_id}"


def session_file_key(run_id: UUID, rel_path: str) -> str:
    # rel_path는 작업공간 상대 경로(이미 경로 검사를 거친 값)다. 세션 id는 키에 넣지 않는다.
    # uuid가 하나 더 붙으면 storage 경로가 37자 길어져 깊은 작업공간 경로가 Windows 260자 한계에
    # 닿는다(12.2 M6 이후 확정). 런 id로 세션이 정해지고, 예전 키(session-files/<세션>/<런>/…)로
    # 저장된 행은 그 키 그대로 읽는다.
    return f"session-files/{run_id}/{rel_path}"


def staged_file_key(run_id: UUID, call_id: str, rel_path: str) -> str:
    # call_agent로 하위 에이전트에 넘기려고 복사한 부모 작업공간 파일(오케스트레이션 4.1). 런 종료
    # 수집의 session_file_key와 겹치면 부모가 그 파일을 고친 뒤의 수집이 덮어써 스테이징 행의
    # sha256이 실제와 어긋나므로 호출별 폴더로 나눈다. call_id는 키에 쓸 수 있게 고친 도구 호출
    # id다.
    return f"session-files/{run_id}/.calls/{call_id}/{rel_path}"


def snapshot_key(workspace_id: UUID, snapshot_id: UUID) -> str:
    return f"snapshots/{workspace_id}/{snapshot_id}.zip"


def safe_tool_call_id(tool_call_id: str) -> str:
    """storage 키에 쓰는 도구 호출 id. 프로바이더가 주는 id에 '/' 같은 글자가 섞여도 된다."""
    if 0 < len(tool_call_id) <= 64 and set(tool_call_id) <= _SAFE_ID_CHARS:
        return tool_call_id.lstrip(".") or "call"
    return "call-" + uuid5(NAMESPACE_URL, tool_call_id).hex


def tool_output_key(run_id: UUID, tool_call_id: str) -> str:
    return f"tool-outputs/{run_id}/{tool_call_id}.txt"


def wf_file_key(workspace_id: UUID, file_id: UUID) -> str:
    # 워크플로우 업로드·출력 파일(2단계 4.1 wf_file).
    return f"wf-files/{workspace_id}/{file_id}"
