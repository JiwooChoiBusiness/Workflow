"""변경 파일 수집의 순서·한도·이유(설계 3.13)."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.agent.file_collector import (
    MTIME_TOLERANCE,
    REASON_COPY_FAILED,
    REASON_RUN_LIMIT,
    REASON_TOO_LARGE,
    CopiedFile,
    CopyFailed,
    changes_since,
    collect_files,
    order_changes,
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


def test_baseline_is_the_moment_the_workspace_was_ready() -> None:
    # 파일 시스템 시각이 한 틱 늦게 찍히는 만큼만 앞당긴다. 몇 초 앞의 파일은 이 런의 것이 아니다.
    assert changes_since(NOW) == NOW - MTIME_TOLERANCE
    assert MTIME_TOLERANCE < timedelta(seconds=1)
    assert changes_since(NOW, NOW - timedelta(minutes=5)) == NOW - MTIME_TOLERANCE


def test_baseline_never_reaches_back_before_the_previous_run_ended() -> None:
    # 앞 런이 끝난(작업공간을 초기화한) 시각과 같은 틱에 쓴 파일도 다시 잡지 않는다.
    floor = NOW - timedelta(milliseconds=5)
    since = changes_since(NOW, floor)

    assert floor < since <= NOW


def test_mtime_lag_stays_inside_the_tolerance(tmp_path: Path) -> None:
    # 런이 작업공간을 받자마자 쓴 파일의 mtime이 그 시각보다 허용 오차 넘게 이르게 찍히면 변경 파일
    # 목록에서 빠진다. 쓰기 직전 시계와 찍힌 mtime의 차이를 이 PC·CI의 파일 시스템에서 잰다.
    lags: list[float] = []
    for index in range(200):
        path = tmp_path / f"f{index}.txt"
        before = time.time()
        path.write_bytes(b"x")
        lags.append(before - path.stat().st_mtime)

    worst = max(lags)
    assert worst < MTIME_TOLERANCE.total_seconds(), (
        f"mtime이 쓰기 직전 시각보다 최대 {worst * 1000:.1f}ms 이르다"
    )


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
