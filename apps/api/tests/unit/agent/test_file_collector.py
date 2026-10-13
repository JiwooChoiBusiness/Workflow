"""변경 파일 수집의 순서·한도·이유(설계 3.13)."""

from datetime import UTC, datetime, timedelta

from app.agent.file_collector import (
    REASON_COPY_FAILED,
    REASON_RUN_LIMIT,
    REASON_TOO_LARGE,
    CopiedFile,
    CopyFailed,
    collect_files,
    order_changes,
    window_start,
)
from app.sandbox.base import FileChanges, FileEntry

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def entry(path: str, size: int = 10, age_sec: int = 0) -> FileEntry:
    return FileEntry(path=path, type="file", size=size, mtime=NOW - timedelta(seconds=age_sec))


class Copier:
    def __init__(self, failing: set[str] | None = None) -> None:
        self.copied: list[str] = []
        self.failing = failing or set()

    async def __call__(self, path: str) -> CopiedFile:
        if path in self.failing:
            raise CopyFailed("잠겨 있다")
        self.copied.append(path)
        return CopiedFile(storage_key=f"session-files/s/r/{path}", size=10, sha256="ab" * 32)


def test_outputs_first_then_most_recent() -> None:
    ordered = order_changes(
        [entry("b.txt", age_sec=5), entry("outputs/old.csv", age_sec=50), entry("a.txt", age_sec=1)]
    )

    assert [item.path for item in ordered] == ["outputs/old.csv", "a.txt", "b.txt"]


def test_window_starts_two_seconds_early() -> None:
    assert window_start(NOW) == NOW - timedelta(seconds=2)


async def test_files_are_copied_with_mime_and_key() -> None:
    copier = Copier()

    result = await collect_files(
        FileChanges(entries=[entry("outputs/결과.csv"), entry("notes.md")], truncated=False),
        copier,
    )

    assert copier.copied == ["outputs/결과.csv", "notes.md"]
    first = result.files[0]
    # Windows는 레지스트리의 MIME 표를 쓰므로 csv 값이 플랫폼마다 다르다.
    assert (first.filename, first.downloadable) == ("결과.csv", True)
    assert "/" in first.mime
    assert first.storage_key == "session-files/s/r/outputs/결과.csv"
    assert not result.truncated


async def test_limits_leave_files_listed_but_not_stored() -> None:
    copier = Copier()
    changes = FileChanges(
        entries=[
            entry("outputs/huge.bin", size=60 * 1024 * 1024),
            entry("a.txt"),
            entry("b.txt", age_sec=1),
            entry("c.txt", age_sec=2),
        ],
        truncated=True,
    )

    result = await collect_files(changes, copier, max_files=2)

    reasons = {item.path: item.reason for item in result.files}
    assert reasons == {
        "outputs/huge.bin": REASON_TOO_LARGE,
        "a.txt": None,
        "b.txt": None,
        "c.txt": REASON_RUN_LIMIT,
    }
    assert copier.copied == ["a.txt", "b.txt"]
    assert result.truncated


async def test_total_bytes_limit() -> None:
    copier = Copier()

    result = await collect_files(
        FileChanges(
            entries=[entry("a.txt", size=8), entry("b.txt", size=8, age_sec=1)], truncated=False
        ),
        copier,
        max_run_bytes=15,
    )

    assert [item.reason for item in result.files] == [None, REASON_RUN_LIMIT]


async def test_copy_failure_is_recorded() -> None:
    result = await collect_files(
        FileChanges(entries=[entry("locked.xlsx")], truncated=False), Copier({"locked.xlsx"})
    )

    [item] = result.files
    assert (item.reason, item.downloadable) == (REASON_COPY_FAILED, False)


async def test_directories_are_ignored() -> None:
    changes = FileChanges(
        entries=[FileEntry(path="outputs", type="dir", size=0, mtime=NOW), entry("x.txt")],
        truncated=False,
    )

    result = await collect_files(changes, Copier())

    assert [item.path for item in result.files] == ["x.txt"]
