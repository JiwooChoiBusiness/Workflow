import os
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

SCAN_BUDGET_SEC = 3.0
_DEADLINE_CHECK_EVERY = 256

# 3.4 표에서 변경 수집 대상이 아닌 최상위 폴더.
CHANGE_EXCLUDED_TOP = frozenset(
    {"agent-files", "skills", "uploads", "task", ".packages", ".platform", ".run", ".tmp", ".cache"}
)
# 어디에 있든 결과물로 보지 않는 폴더와 파일. 스냅샷 제외 목록(3.12)과 같다.
JUNK_DIRS = frozenset({"__pycache__", ".venv", "node_modules", ".git"})
JUNK_SUFFIXES = (".pyc",)


@dataclass(frozen=True)
class ScanEntry:
    rel: str  # 작업공간 상대 경로, 구분자 '/'
    path: Path
    is_dir: bool
    is_link: bool
    size: int
    mtime: float


@dataclass
class Scanner:
    """os.scandir 재귀 순회. 심볼릭 링크와 정션은 항목으로만 내고 따라 들어가지 않는다.

    Windows에서 정션은 is_symlink()가 False이고 is_dir(follow_symlinks=False)가 True라서
    is_junction()을 따로 봐야 작업공간 밖으로 걸어 나가지 않는다.
    """

    root: Path
    budget_sec: float | None = SCAN_BUDGET_SEC
    skip_top: frozenset[str] = frozenset()
    skip_dirs: frozenset[str] = frozenset()
    skip_suffixes: tuple[str, ...] = ()
    include_hidden: bool = True
    recursive: bool = True
    truncated: bool = field(default=False, init=False)

    def __iter__(self) -> Iterator[ScanEntry]:
        deadline = None if self.budget_sec is None else time.monotonic() + self.budget_sec
        stack: list[tuple[Path, str]] = [(self.root, "")]
        seen = 0
        while stack:
            directory, prefix = stack.pop()
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError:
                continue
            subdirs: list[tuple[Path, str]] = []
            for entry in entries:
                seen += 1
                if (
                    deadline is not None
                    and seen % _DEADLINE_CHECK_EVERY == 0
                    and time.monotonic() > deadline
                ):
                    self.truncated = True
                    return
                name = entry.name
                folded = name.casefold()
                if not self.include_hidden and name.startswith("."):
                    continue
                try:
                    is_link = entry.is_symlink() or entry.is_junction()
                    is_dir = not is_link and entry.is_dir(follow_symlinks=False)
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                rel = f"{prefix}{name}"
                if is_dir:
                    if folded in self.skip_dirs or (not prefix and folded in self.skip_top):
                        continue
                    yield ScanEntry(rel, Path(entry.path), True, False, 0, info.st_mtime)
                    if self.recursive:
                        subdirs.append((Path(entry.path), f"{rel}/"))
                    continue
                if not prefix and folded in self.skip_top:
                    continue
                if self.skip_suffixes and folded.endswith(self.skip_suffixes):
                    continue
                yield ScanEntry(rel, Path(entry.path), False, is_link, info.st_size, info.st_mtime)
            # 이름순으로 내보내려고 뒤집어 쌓는다.
            stack.extend(reversed(subdirs))


@dataclass(frozen=True)
class DiskUsage:
    total_bytes: int
    estimated: bool
    file_count: int


def measure(root: Path, *, budget_sec: float = SCAN_BUDGET_SEC) -> DiskUsage:
    scanner = Scanner(root, budget_sec=budget_sec)
    total = 0
    count = 0
    for entry in scanner:
        if not entry.is_dir and not entry.is_link:
            total += entry.size
            count += 1
    return DiskUsage(total, scanner.truncated, count)


def scan_changes(
    root: Path,
    since_ts: float,
    *,
    max_entries: int,
    budget_sec: float = SCAN_BUDGET_SEC,
) -> tuple[list[ScanEntry], bool]:
    scanner = Scanner(
        root,
        budget_sec=budget_sec,
        skip_top=CHANGE_EXCLUDED_TOP,
        skip_dirs=JUNK_DIRS,
        skip_suffixes=JUNK_SUFFIXES,
    )
    found: list[ScanEntry] = []
    for entry in scanner:
        if entry.is_dir or entry.is_link or entry.mtime < since_ts:
            continue
        if len(found) >= max_entries:
            return found, True
        found.append(entry)
    return found, scanner.truncated


def format_size(size: int) -> str:
    """메시지에 넣는 크기(1024 단위). 한도를 아주 작게 잡아도 0MB처럼 보이지 않게 단위를 고른다."""
    if size < 1024:
        return f"{size}B"
    value, unit = size / 1024, "KB"
    for bigger in ("MB", "GB"):
        if value < 1024:
            break
        value, unit = value / 1024, bigger
    return f"{value:.1f}".removesuffix(".0") + unit


def largest_dirs(
    files: Iterable[ScanEntry], *, top: int = 10, depth: int = 2
) -> list[tuple[str, int]]:
    """파일 크기를 depth 단계까지의 상위 폴더별로 더해 큰 순서로 돌려준다."""
    totals: Counter[str] = Counter()
    for entry in files:
        if entry.is_dir:
            continue
        parts = entry.rel.split("/")[:-1]
        for level in range(1, min(depth, len(parts)) + 1):
            totals["/".join(parts[:level])] += entry.size
    return totals.most_common(top)
