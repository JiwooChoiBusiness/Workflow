"""platform backup(설계 2.10, M10.1 (1)).

결과 폴더는 DATA_DIR과 같은 구조라 그 폴더를 DATA_DIR로 지정하면 복구가 끝난다. 별도 복구 명령은
없다. cli.py는 인자와 출력만 맡고 여기는 표준 라이브러리로만 복사한다.
"""

import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app import sysinfo
from app.config import REPO_ROOT, Settings

DEFAULT_ROOT = REPO_ROOT / "backups"
DEFAULT_KEEP = 5
MANIFEST_NAME = "manifest.json"
DB_NAME = "app.db"
ALLOWLIST_NAME = "allowlist.txt"
PART_SUFFIX = ".part"
# app/sandbox/local_workspace.py의 DELETED_PREFIX. 샌드박스 모듈을 끌어오지 않으려고 따로 둔다.
DELETED_PREFIX = ".deleted-"
STAMP_FORMAT = "%Y%m%d-%H%M%S"
_STAMP = re.compile(r"\d{8}-\d{6}")
# 서버가 체크포인트 중이면 잠깐 기다린다. 서버 쪽 busy_timeout(5초)보다 넉넉하게 둔다.
BUSY_TIMEOUT_SEC = 30.0
QUICK_CHECK_MAX_CHARS = 500


class BackupError(Exception):
    pass


@dataclass
class _Tally:
    files: int = 0
    bytes: int = 0
    links: int = 0
    vanished: int = 0

    def add(self, path: str | Path) -> None:
        self.files += 1
        self.bytes += os.stat(path).st_size


@dataclass(frozen=True)
class BackupResult:
    path: Path
    manifest: dict[str, Any]
    # 링크·정션은 따라가지 않는다. 작업공간에서 만든 정션이 PC의 다른 폴더를 가리킬 수 있다.
    links_skipped: int
    # 서버가 떠 있으면 복사하는 사이에 지워지는 파일이 있다(보존 정리, 세션 삭제).
    vanished: int
    pruned: list[Path]
    prune_failed: list[Path]

    @property
    def healthy(self) -> bool:
        return self.manifest["quick_check"] == "ok"


def server_warning(settings: Settings) -> str | None:
    if sysinfo.check_port(settings.server_port).ok:
        return None
    return (
        f"127.0.0.1:{settings.server_port}에 서버가 떠 있다. 실행 중인 런이 있으면 storage와 DB가"
        " 어긋날 수 있다"
    )


def create_backup(
    settings: Settings,
    *,
    out: Path | None = None,
    with_workspaces: bool = False,
    keep: int = DEFAULT_KEEP,
    now: datetime | None = None,
) -> BackupResult:
    if keep < 1:
        raise BackupError("--keep은 1 이상이어야 한다")
    source = settings.data_dir.resolve()
    database = source / DB_NAME
    if not database.is_file():
        raise BackupError(f"{database}가 없다. DATA_DIR이 맞는지 확인한다")
    created = (now or datetime.now()).astimezone()
    target = (DEFAULT_ROOT / created.strftime(STAMP_FORMAT) if out is None else out).resolve()
    trees = {"storage": _skip_partial_writes}
    if with_workspaces:
        trees["workspaces"] = _skip_deleted_workspaces
    for name in trees:
        if target.is_relative_to(source / name):
            raise BackupError(f"백업 폴더가 복사할 {source / name} 안에 있다. 다른 곳을 고른다")
    part = target.with_name(target.name + PART_SUFFIX)
    for path in (target, part):
        if os.path.lexists(path):
            raise BackupError(
                f"{path}가 이미 있다. 덮어쓰지 않는다"
                + (". 끊긴 백업의 흔적이면 지우고 다시 한다" if path == part else "")
            )

    part.parent.mkdir(parents=True, exist_ok=True)
    part.mkdir()
    tally = _Tally()
    try:
        quick_check, revision = _copy_database(database, part / DB_NAME)
        tally.add(part / DB_NAME)
        if (source / ALLOWLIST_NAME).is_file():
            shutil.copy2(source / ALLOWLIST_NAME, part / ALLOWLIST_NAME)
            tally.add(part / ALLOWLIST_NAME)
        for name, skip in trees.items():
            _copy_tree(source / name, part / name, tally, skip)
        manifest: dict[str, Any] = {
            "created_at": created.isoformat(timespec="seconds"),
            "source_data_dir": str(settings.data_dir),
            "alembic_revision": revision,
            "with_workspaces": with_workspaces,
            "files": tally.files,
            "bytes": tally.bytes,
            "quick_check": quick_check,
            "encryption_key_fingerprint": sysinfo.encryption_key_fingerprint(settings),
        }
        (part / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.rename(sysinfo.long_path(part), sysinfo.long_path(target))
    except BaseException:
        _remove_quietly(part)
        raise

    pruned: list[Path] = []
    prune_failed: list[Path] = []
    # 복사본이 깨졌으면 예전 백업을 남겨 둔다.
    if out is None and quick_check == "ok":
        pruned, prune_failed = prune(DEFAULT_ROOT, keep, protect=target)
    return BackupResult(
        path=target,
        manifest=manifest,
        links_skipped=tally.links,
        vanished=tally.vanished,
        pruned=pruned,
        prune_failed=prune_failed,
    )


def prune(root: Path, keep: int, *, protect: Path | None = None) -> tuple[list[Path], list[Path]]:
    """root에서 타임스탬프 이름에 manifest.json이 있는 폴더만 세어 최근 keep개를 남긴다."""
    backups = sorted(
        path
        for path in root.iterdir()
        if _STAMP.fullmatch(path.name) and (path / MANIFEST_NAME).is_file()
    )
    removed: list[Path] = []
    failed: list[Path] = []
    for path in backups[:-keep]:
        # 시계가 뒤로 갔으면 방금 만든 백업 이름이 더 앞설 수 있다.
        if path.resolve() == protect:
            continue
        try:
            _remove(path)
        except OSError:
            failed.append(path)
        else:
            removed.append(path)
    return removed, failed


def _copy_database(source: Path, dest: Path) -> tuple[str, str | None]:
    # WAL에서는 읽기가 서버의 쓰기를 막지 않는다. pages=-1이면 읽기 트랜잭션 하나에서 끝나므로
    # 도중에 서버가 쓴 페이지 때문에 처음부터 다시 복사하는 일도 없고 WAL 내용까지 합쳐진다.
    reader = sqlite3.connect(source, timeout=BUSY_TIMEOUT_SEC)
    try:
        writer = sqlite3.connect(dest)
        try:
            reader.backup(writer, pages=-1)
            rows = writer.execute("PRAGMA quick_check").fetchall()
            revision = _alembic_revision(writer)
            # 복사본도 WAL 모드라 열면 -wal·-shm이 생긴다. 비우고 닫아야 app.db 하나로 남는다.
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            writer.close()
    finally:
        reader.close()
    messages = [str(row[0]) for row in rows]
    if messages == ["ok"]:
        return "ok", revision
    return "; ".join(messages)[:QUICK_CHECK_MAX_CHARS], revision


def _alembic_revision(conn: sqlite3.Connection) -> str | None:
    try:
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.OperationalError:
        return None
    return ",".join(sorted(str(row[0]) for row in rows)) or None


def _skip_partial_writes(name: str, top: bool) -> bool:
    # 저장소가 쓰는 중인 임시 파일(<이름>.<uuid>.part). 끝난 파일은 다음 백업에 들어간다.
    return name.endswith(PART_SUFFIX)


def _skip_deleted_workspaces(name: str, top: bool) -> bool:
    return top and name.startswith(DELETED_PREFIX)


def _copy_tree(source: Path, dest: Path, tally: _Tally, skip: Callable[[str, bool], bool]) -> None:
    root = sysinfo.long_path(source)
    if not os.path.isdir(root):
        return
    # 재귀 대신 스택을 쓴다. 모델이 만든 아주 깊은 폴더에서도 RecursionError가 나지 않는다.
    stack = [(root, sysinfo.long_path(dest))]
    while stack:
        src, dst = stack.pop()
        try:
            with os.scandir(src) as entries:
                children = list(entries)
        except FileNotFoundError:
            tally.vanished += 1
            continue
        os.makedirs(dst, exist_ok=True)
        for entry in children:
            if skip(entry.name, src == root):
                continue
            if entry.is_symlink() or entry.is_junction():
                tally.links += 1
                continue
            target = os.path.join(dst, entry.name)
            if entry.is_dir(follow_symlinks=False):
                stack.append((entry.path, target))
                continue
            try:
                shutil.copy2(entry.path, target)
            except FileNotFoundError:
                if os.path.lexists(entry.path):
                    raise
                tally.vanished += 1
                continue
            tally.add(target)


def _clear_readonly(func: Callable[..., Any], path: str, exc: BaseException) -> None:
    # 작업공간에서 복사한 읽기 전용 파일(.git 객체 등)은 속성을 풀어야 지울 수 있다.
    if isinstance(exc, PermissionError) and os.path.lexists(path):
        os.chmod(path, stat.S_IWRITE)
        func(path)
        return
    raise exc


def _remove(path: Path) -> None:
    shutil.rmtree(sysinfo.long_path(path), onexc=_clear_readonly)


def _remove_quietly(path: Path) -> None:
    try:
        _remove(path)
    except OSError:
        pass
