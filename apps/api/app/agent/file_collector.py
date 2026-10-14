"""런이 바꾼 파일 수집(설계 3.13).

백엔드 changes()가 준 목록에서 한도 안의 파일만 storage로 복사한다. 복사 방법과 session_file 행은
부르는 쪽(런 실행기)이 정하고 여기서는 순서·한도·이유만 정한다. 다운로드는 항상 storage 복사본에서
하므로 작업공간 폴더가 지워져도 된다(A5).
"""

import mimetypes
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta

import structlog
from pydantic import BaseModel

from app.sandbox.base import FileChanges, FileEntry

log = structlog.get_logger("app.agent")

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_RUN_BYTES = 200 * 1024 * 1024
MAX_RUN_FILES = 200
MAX_SCAN_ENTRIES = 1000
# 파일 시스템은 mtime을 시스템 시계의 한 틱(Windows 1~16ms, Linux 1~10ms) 늦게 찍을 수 있다.
MTIME_TOLERANCE = timedelta(milliseconds=50)
# 바닥 시각은 DB에 마이크로초까지만 남고 mtime 비교는 float라서, 그 시각과 같은 틱에 쓴 파일이
# 바닥보다 늦어 보이지 않게 조금 띄운다.
_FLOOR_STEP = timedelta(milliseconds=1)
OUTPUTS_PREFIX = "outputs/"

REASON_TOO_LARGE = "too_large"
REASON_RUN_LIMIT = "run_limit"
REASON_COPY_FAILED = "copy_failed"


class CopyFailed(Exception):
    """복사하지 못한 파일. 목록에는 남기고 다운로드만 막는다."""


class AlreadyCollected(Exception):
    """이 런이 같은 경로·같은 내용으로 이미 storage에 남긴 파일(call_agent 스테이징).

    목록에서 빼고 새 행을 만들지 않는다. 같은 파일이 세션 파일 목록에 두 번 보이지 않게 한다
    (오케스트레이션 4.1의 3번).
    """


class CopiedFile(BaseModel):
    storage_key: str
    size: int
    sha256: str


class CollectedFile(BaseModel):
    path: str
    filename: str
    size: int
    mime: str
    storage_key: str | None = None
    sha256: str | None = None
    reason: str | None = None

    @property
    def downloadable(self) -> bool:
        return self.storage_key is not None


class CollectionResult(BaseModel):
    files: list[CollectedFile]
    truncated: bool


CopyFn = Callable[[str], Awaitable[CopiedFile]]


def changes_since(ready_at: datetime, floor: datetime | None = None) -> datetime:
    """이 런의 변경으로 볼 mtime 기준선(3.13).

    ready_at은 런이 작업공간을 넘겨받은 시각이다(스냅샷 복원·첨부 복사가 끝난 뒤). 그 전에
    플랫폼이 쓴 파일은 mtime이 새로 찍혔어도 런의 결과가 아니다. floor는 이 런 앞에서 플랫폼이
    작업공간을 마지막으로 만진 시각(앞 런이 끝난 때, 작업공간 초기화)이다. 그때 이미 있던 파일은
    앞 런이 알렸거나 원래 있던 것이라 허용 오차 안에 들어도 다시 잡지 않는다.
    """
    since = ready_at - MTIME_TOLERANCE
    if floor is not None:
        since = max(since, floor + _FLOOR_STEP)
    return since


def guess_mime(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def order_changes(entries: Sequence[FileEntry]) -> list[FileEntry]:
    """outputs/의 파일을 먼저, 그다음 최근에 바뀐 순서. 한도에 걸리면 뒤쪽이 빠진다."""
    by_path = sorted(entries, key=lambda entry: entry.path)
    by_recent = sorted(by_path, key=lambda entry: entry.mtime, reverse=True)
    return sorted(by_recent, key=lambda entry: not entry.path.startswith(OUTPUTS_PREFIX))


async def collect_files(
    changes: FileChanges,
    copy: CopyFn,
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_run_bytes: int = MAX_RUN_BYTES,
    max_files: int = MAX_RUN_FILES,
) -> CollectionResult:
    collected: list[CollectedFile] = []
    total = 0
    copied = 0
    for entry in order_changes([entry for entry in changes.entries if entry.type == "file"]):
        filename = entry.path.rsplit("/", 1)[-1]
        item = CollectedFile(
            path=entry.path, filename=filename, size=entry.size, mime=guess_mime(filename)
        )
        if entry.size > max_file_bytes:
            item.reason = REASON_TOO_LARGE
        elif copied >= max_files or total + entry.size > max_run_bytes:
            item.reason = REASON_RUN_LIMIT
        else:
            try:
                stored = await copy(entry.path)
            except AlreadyCollected:
                continue
            except CopyFailed as exc:
                log.warning("agent.file_copy_failed", path=entry.path, error=str(exc))
                item.reason = REASON_COPY_FAILED
            else:
                # 스캔 뒤 복사 사이에 파일이 바뀔 수 있으므로 실제로 복사한 크기를 쓴다.
                item.size = stored.size
                item.storage_key = stored.storage_key
                item.sha256 = stored.sha256
                total += stored.size
                copied += 1
        collected.append(item)
    return CollectionResult(files=collected, truncated=changes.truncated)
