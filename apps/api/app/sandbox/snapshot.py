"""작업공간 zip 스냅샷(3.12). 발행·복원 때만 쓰므로 local_workspace가 지연 import한다."""

import hashlib
import os
import stat
import time
import zipfile
from pathlib import Path

from app.sandbox.base import SandboxError, SnapshotInfo
from app.sandbox.disk import (
    JUNK_DIRS,
    JUNK_SUFFIXES,
    ScanEntry,
    Scanner,
    format_size,
    largest_dirs,
)
from app.sandbox.paths import resolve_in_workspace, split_relative
from app.sandbox.python_runner import REQUIREMENTS_PATH, parse_requirements

# 스냅샷에 넣지 않는 최상위 폴더(3.4). 버전 설정에서 다시 채우거나 재설치로 되살린다. agents는
# 미리보기에서 부른 하위 에이전트의 결과 복사본이라 발행본에 담지 않는다(오케스트레이션 4.2).
SNAPSHOT_EXCLUDED_TOP = frozenset(
    {".packages", ".run", ".tmp", ".cache", "uploads", "task", "agent-files", "skills", "agents"}
)
MAX_SNAPSHOT_FILES = 50_000
MAX_RESTORE_BYTES = 2 * 1024**3
MAX_RESTORE_ENTRIES = 2 * MAX_SNAPSHOT_FILES
COMPRESS_LEVEL = 6
WORKSPACE_JSON = ".platform/workspace.json"
BOOTSTRAP_PATH = ".run/_bootstrap.py"
# 빈 작업공간에 있어도 되는 파일. acquire가 만든다.
SKELETON_FILES = frozenset({WORKSPACE_JSON, BOOTSTRAP_PATH})
_CHUNK = 1024 * 1024


def file_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as file:
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def snapshot_files(root: Path) -> list[ScanEntry]:
    scanner = Scanner(
        root,
        budget_sec=None,
        skip_top=SNAPSHOT_EXCLUDED_TOP,
        skip_dirs=JUNK_DIRS,
        skip_suffixes=JUNK_SUFFIXES,
    )
    # 심볼릭 링크·정션은 따라가지 않고 건너뛴다.
    return [entry for entry in scanner if not entry.is_dir and not entry.is_link]


def create_snapshot(
    root: Path, dest: Path, *, max_bytes: int, max_files: int = MAX_SNAPSHOT_FILES
) -> SnapshotInfo:
    files = snapshot_files(root)
    raw_total = sum(entry.size for entry in files)
    if raw_total > max_bytes or len(files) > max_files:
        top = ", ".join(f"{path} {format_size(size)}" for path, size in largest_dirs(files))
        raise SandboxError(
            "snapshot_too_large",
            f"작업공간이 스냅샷 한도를 넘는다(파일 {len(files)}개, {format_size(raw_total)} / "
            f"한도 {max_files}개, {format_size(max_bytes)}). 큰 폴더: {top or '없음'}",
            status=413,
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    written = 0
    raw_bytes = 0
    try:
        with zipfile.ZipFile(
            partial,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=COMPRESS_LEVEL,
            # 1980년 이전 mtime도 오류 없이 1980년으로 맞춘다.
            strict_timestamps=False,
        ) as archive:
            for entry in files:
                try:
                    archive.write(entry.path, arcname=entry.rel)
                except FileNotFoundError:
                    # 훑은 뒤 지워진 파일
                    continue
                raw_bytes += archive.getinfo(entry.rel).file_size
                written += 1
        os.replace(partial, dest)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    digest, size = file_sha256(dest)
    return SnapshotInfo(raw_bytes=raw_bytes, size_bytes=size, file_count=written, sha256=digest)


def ensure_empty(root: Path) -> None:
    """골격 폴더와 workspace.json, _bootstrap.py만 있어야 한다. 아니면 409."""
    for entry in Scanner(root, budget_sec=None):
        if entry.is_dir:
            continue
        if entry.is_link or entry.rel not in SKELETON_FILES:
            raise SandboxError(
                "workspace_not_empty",
                f"빈 작업공간에만 복원할 수 있다. 이미 있는 파일: {entry.rel}",
                status=409,
            )


def _restore_plan(
    root: Path, infos: list[zipfile.ZipInfo], max_bytes: int
) -> list[tuple[zipfile.ZipInfo, Path]]:
    if len(infos) > MAX_RESTORE_ENTRIES:
        raise SandboxError("snapshot_invalid", f"항목이 {MAX_RESTORE_ENTRIES}개를 넘는다")
    declared = sum(info.file_size for info in infos)
    if declared > max_bytes:
        raise SandboxError(
            "snapshot_too_large",
            f"압축을 풀면 {format_size(declared)}라 한도 {format_size(max_bytes)}를 넘는다",
            status=413,
        )
    plan: list[tuple[zipfile.ZipInfo, Path]] = []
    seen: set[str] = set()
    for info in infos:
        name = info.filename
        if "\\" in name:
            raise SandboxError("snapshot_invalid", f"백슬래시가 든 항목: {name[:80]!r}")
        rel = name.rstrip("/") if info.is_dir() else name
        if any(part in ("", ".", "..") for part in rel.split("/")):
            raise SandboxError("snapshot_invalid", f"상대 경로가 아닌 항목: {name[:80]!r}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise SandboxError("snapshot_invalid", f"심볼릭 링크 항목: {name[:80]!r}")
        try:
            # 절대 경로·드라이브·예약 이름·금지 문자를 Windows 규칙으로 거른다.
            split_relative(rel, os_name="nt")
            target = resolve_in_workspace(root, rel)
        except SandboxError as exc:
            raise SandboxError("snapshot_invalid", f"쓸 수 없는 항목: {exc.message}") from exc
        folded = rel.casefold()
        if folded in seen:
            raise SandboxError("snapshot_invalid", f"같은 이름이 두 번 나온다: {name[:80]!r}")
        seen.add(folded)
        top = folded.split("/", 1)[0]
        # 새 세션의 workspace.json과 실행 스크립트는 덮어쓰지 않는다.
        if folded == WORKSPACE_JSON or top == ".run":
            continue
        plan.append((info, target))
    return plan


def restore_snapshot(
    root: Path,
    src: Path,
    *,
    expected_sha256: str | None = None,
    max_bytes: int = MAX_RESTORE_BYTES,
) -> tuple[int, int, str, list[str]]:
    """(파일 수, 원본 바이트, sha256, 설치 이력)을 돌려준다."""
    ensure_empty(root)
    digest, _ = file_sha256(src)
    if expected_sha256 is not None and digest != expected_sha256.lower():
        raise SandboxError("snapshot_corrupt", "스냅샷 파일의 sha256이 기록과 다르다", status=422)
    created: list[Path] = []
    file_count = 0
    written = 0
    try:
        with zipfile.ZipFile(src) as archive:
            for info, target in _restore_plan(root, archive.infolist(), max_bytes):
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("xb") as sink:
                    created.append(target)
                    while chunk := source.read(_CHUNK):
                        written += len(chunk)
                        if written > max_bytes:
                            raise SandboxError(
                                "snapshot_too_large",
                                f"압축을 풀면 한도 {format_size(max_bytes)}를 넘는다",
                                status=413,
                            )
                        sink.write(chunk)
                # 복원한 파일이 첫 런의 "변경된 파일"로 잡히지 않게 원래 mtime을 되살린다.
                mtime = time.mktime((*info.date_time, 0, 0, -1))
                os.utime(target, (mtime, mtime))
                file_count += 1
    except zipfile.BadZipFile as exc:
        _remove(created)
        raise SandboxError("snapshot_invalid", f"zip 파일이 아니다: {exc}") from exc
    except OSError as exc:
        _remove(created)
        raise SandboxError("snapshot_restore_failed", f"복원하지 못했다: {exc}") from exc
    except BaseException:
        _remove(created)
        raise
    requirements_file = root / REQUIREMENTS_PATH
    requirements = (
        parse_requirements(requirements_file.read_text(encoding="utf-8-sig"))
        if requirements_file.is_file()
        else []
    )
    return file_count, written, digest, requirements


def _remove(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
