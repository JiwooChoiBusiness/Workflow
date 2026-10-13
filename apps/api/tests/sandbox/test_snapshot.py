import hashlib
import json
import os
import stat
import sys
import time
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from app.sandbox.base import (
    ExecEvent,
    ExecExited,
    ExecOutput,
    ExecSpec,
    ExecStarted,
    SandboxError,
    WorkspaceHandle,
)
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.sandbox.provisioning import ProvisionEvent, SnapshotSource, WorkspaceProvisioner
from app.sandbox.snapshot import create_snapshot, restore_snapshot

INCLUDED = {
    "notes.md": b"# notes\n",
    "outputs/report.csv": "a,b\n한글,1\n".encode(),
    "data/raw/input.json": b"{}",
    ".platform/requirements.txt": b"requests==2.31.0\nnumpy\n",
    "src/app.py": b"print('x')\n",
}
EXCLUDED = [
    ".packages/requests/__init__.py",
    ".run/call_1.ps1",
    ".tmp/t.txt",
    ".cache/matplotlib/x",
    "uploads/m1/in.txt",
    "task/inputs.json",
    "agent-files/ref.pdf",
    "skills/s/SKILL.md",
    "src/__pycache__/app.cpython-312.pyc",
    "src/old.pyc",
    ".venv/pyvenv.cfg",
    "node_modules/x/index.js",
    ".git/HEAD",
]


def make_dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


async def fill(backend: LocalWorkspaceBackend, handle: WorkspaceHandle) -> None:
    for path, data in INCLUDED.items():
        target = handle.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for path in EXCLUDED:
        target = handle.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"excluded")


async def test_snapshot_keeps_work_and_skips_rebuildable_folders(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, tmp_path: Path
) -> None:
    await fill(backend, handle)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    make_dir_link(handle.root / "link", outside)
    dest = tmp_path / "snapshots" / "ws1" / "snap1.zip"

    info = await backend.snapshot(handle, dest)

    with zipfile.ZipFile(dest) as archive:
        names = set(archive.namelist())
        assert archive.read("outputs/report.csv") == INCLUDED["outputs/report.csv"]
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in archive.infolist())
    assert names == {*INCLUDED, ".platform/workspace.json"}
    assert not (dest.parent / "snap1.zip.part").exists()
    assert info.file_count == len(names)
    assert info.raw_bytes == sum((handle.root / name).stat().st_size for name in names)
    assert info.size_bytes == dest.stat().st_size
    assert info.sha256 == hashlib.sha256(dest.read_bytes()).hexdigest()


def test_snapshot_limits_name_the_biggest_folders(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "big" / "inner").mkdir(parents=True)
    (root / "big" / "inner" / "a.bin").write_bytes(b"x" * 5000)
    (root / "small.txt").write_bytes(b"x")

    with pytest.raises(SandboxError) as by_size:
        create_snapshot(root, tmp_path / "s.zip", max_bytes=1000)
    assert by_size.value.code == "snapshot_too_large"
    assert "big" in by_size.value.message and "big/inner" in by_size.value.message
    with pytest.raises(SandboxError) as by_count:
        create_snapshot(root, tmp_path / "s.zip", max_bytes=10**9, max_files=1)
    assert by_count.value.code == "snapshot_too_large"
    assert not (tmp_path / "s.zip").exists()


async def test_restore_into_a_new_session(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, tmp_path: Path
) -> None:
    await fill(backend, handle)
    report = handle.root / "outputs" / "report.csv"
    past = time.time() - 86400
    os.utime(report, (past, past))
    dest = tmp_path / "snap.zip"
    info = await backend.snapshot(handle, dest)

    target = await backend.acquire(uuid4())
    restored = await backend.restore(target, dest, expected_sha256=info.sha256)

    for path, data in INCLUDED.items():
        assert (target.root / path).read_bytes() == data, path
    assert restored.requirements == ["requests==2.31.0", "numpy"]
    assert restored.file_count == len(INCLUDED)
    assert restored.sha256 == info.sha256
    # 첫 런의 변경 파일로 잡히지 않게 원래 mtime을 되살린다(zip은 2초 단위).
    assert abs((target.root / "outputs" / "report.csv").stat().st_mtime - past) <= 2
    workspace = json.loads((target.root / ".platform" / "workspace.json").read_text("utf-8"))
    assert workspace["session_id"] == str(target.session_id)
    assert workspace["restored_from_snapshot"] == "snap"


async def test_restore_needs_an_empty_workspace(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, tmp_path: Path
) -> None:
    await fill(backend, handle)
    dest = tmp_path / "snap.zip"
    await backend.snapshot(handle, dest)

    with pytest.raises(SandboxError) as info:
        await backend.restore(handle, dest)
    assert (info.value.code, info.value.status) == ("workspace_not_empty", 409)


async def test_restore_checks_the_recorded_hash(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, tmp_path: Path
) -> None:
    await fill(backend, handle)
    dest = tmp_path / "snap.zip"
    await backend.snapshot(handle, dest)
    target = await backend.acquire(uuid4())

    with pytest.raises(SandboxError) as info:
        await backend.restore(target, dest, expected_sha256="0" * 64)
    assert info.value.code == "snapshot_corrupt"


def write_zip(path: Path, entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return path


def symlink_entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


@pytest.mark.parametrize(
    "entries",
    [
        [("../evil.txt", b"x")],
        [("a/../../evil.txt", b"x")],
        [("/abs.txt", b"x")],
        [("C:/x.txt", b"x")],
        [("a\\..\\..\\evil.txt", b"x")],
        [("CON.txt", b"x")],
        [("dir/NUL", b"x")],
        [("file.txt:stream", b"x")],
        [("dir./x", b"x")],
        [(symlink_entry("link"), b"/etc/passwd")],
        [("a.txt", b"1"), ("A.TXT", b"2")],
    ],
)
async def test_restore_rejects_zip_slip_entries(
    backend: LocalWorkspaceBackend,
    handle: WorkspaceHandle,
    tmp_path: Path,
    entries: list[tuple[str | zipfile.ZipInfo, bytes]],
) -> None:
    good: list[tuple[str | zipfile.ZipInfo, bytes]] = [("ok/first.txt", b"ok")]
    archive = write_zip(tmp_path / "bad.zip", good + entries)

    with pytest.raises(SandboxError) as info:
        await backend.restore(handle, archive)

    assert info.value.code == "snapshot_invalid"
    # 항목을 모두 검사한 뒤에야 쓰므로 정상 항목도 풀리지 않는다.
    assert not (handle.root / "ok").exists()
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "data" / "evil.txt").exists()


def test_restore_stops_zip_bombs(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", b"\0" * 2_000_000)

    with pytest.raises(SandboxError) as info:
        restore_snapshot(root, archive, max_bytes=1_000_000)
    assert info.value.code == "snapshot_too_large"
    assert list(root.iterdir()) == []


async def test_restore_rejects_files_that_are_not_zips(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, tmp_path: Path
) -> None:
    fake = tmp_path / "fake.zip"
    fake.write_bytes(b"not a zip")

    with pytest.raises(SandboxError) as info:
        await backend.restore(handle, fake)
    assert info.value.code == "snapshot_invalid"


async def test_restore_ignores_run_scripts_and_foreign_workspace_json(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, tmp_path: Path
) -> None:
    archive = write_zip(
        tmp_path / "s.zip",
        [
            (".run/evil.ps1", b"Remove-Item -Recurse C:\\"),
            (".platform/workspace.json", b'{"session_id": "other"}'),
            ("kept.txt", b"kept"),
        ],
    )

    info = await backend.restore(handle, archive)

    assert info.file_count == 1
    assert not (handle.root / ".run" / "evil.ps1").exists()
    workspace = json.loads((handle.root / ".platform" / "workspace.json").read_text("utf-8"))
    assert workspace["session_id"] == str(handle.session_id)


class RecordingProvisioner(WorkspaceProvisioner):
    def __init__(self, backend: LocalWorkspaceBackend, *, reinstall_ok: bool) -> None:
        super().__init__(backend)
        self.reinstall_ok = reinstall_ok
        self.reinstalled: list[list[str]] = []

    async def reinstall_packages(
        self, handle: WorkspaceHandle, requirements: list[str]
    ) -> tuple[bool, str]:
        # 실제 pip은 네트워크를 쓰므로 호출만 기록한다.
        self.reinstalled.append(requirements)
        return (True, "") if self.reinstall_ok else (False, "패키지 재설치 설치 실패. 오류")


@pytest.mark.parametrize("reinstall_ok", [True, False])
async def test_provisioner_restores_the_published_snapshot(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, tmp_path: Path, reinstall_ok: bool
) -> None:
    await fill(backend, handle)
    dest = tmp_path / "snap.zip"
    info = await backend.snapshot(handle, dest)
    snapshot = SnapshotSource(snapshot_id=uuid4(), path=dest, sha256=info.sha256)
    provisioner = RecordingProvisioner(backend, reinstall_ok=reinstall_ok)
    events: list[ProvisionEvent] = []

    async def emit(event: ProvisionEvent) -> None:
        events.append(event)

    result = await provisioner.ensure(
        uuid4(), instance_created_at=None, snapshot=snapshot, emit=emit
    )

    assert result.state == "created"
    assert result.restored_snapshot_id == snapshot.snapshot_id
    assert (result.handle.root / "outputs" / "report.csv").is_file()
    assert provisioner.reinstalled == [["requests==2.31.0", "numpy"]]
    types = [event.type for event in events]
    if reinstall_ok:
        assert types == ["workspace.restoring", "workspace.restored"]
        assert events[-1].data == {"snapshot_id": str(snapshot.snapshot_id), "packages": 2}
        assert result.packages_reinstalled == 2
    else:
        assert types == ["workspace.restoring", "workspace.restore_warning"]
        assert "재설치" in events[-1].data["message"]
    assert result.events == events


async def test_provisioner_reports_a_broken_snapshot_and_keeps_going(
    backend: LocalWorkspaceBackend, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    snapshot = SnapshotSource(snapshot_id=uuid4(), path=broken)

    result = await WorkspaceProvisioner(backend).ensure(
        uuid4(), instance_created_at=None, snapshot=snapshot
    )

    assert result.state == "created"
    assert result.restored_snapshot_id is None
    assert [event.type for event in result.events] == [
        "workspace.restoring",
        "workspace.restore_warning",
    ]


class ScriptedBackend:
    """reinstall_packages가 exec 결과를 어떻게 해석하는지만 본다."""

    def __init__(self, events: list[ExecEvent]) -> None:
        self.events = events
        self.specs: list[ExecSpec] = []

    async def exec(self, h: WorkspaceHandle, spec: ExecSpec) -> AsyncIterator[ExecEvent]:
        self.specs.append(spec)
        for event in self.events:
            yield event


@pytest.mark.parametrize(
    ("code", "timed_out", "ok"), [(0, False, True), (1, False, False), (None, True, False)]
)
async def test_reinstall_reads_the_pip_result(
    tmp_path: Path, code: int | None, timed_out: bool, ok: bool
) -> None:
    fake = ScriptedBackend(
        [
            ExecStarted(exec_id="r", pid=1),
            ExecOutput(exec_id="r", stream="stderr", data="오류: 찾을 수 없다\n".encode()),
            ExecExited(
                exec_id="r",
                code=code,
                timed_out=timed_out,
                killed=False,
                duration_ms=10,
                peak_memory_bytes=None,
            ),
        ]
    )
    handle = WorkspaceHandle(session_id=uuid4(), root=tmp_path, backend="local")
    provisioner = WorkspaceProvisioner(fake, pip_timeout_sec=600)  # type: ignore[arg-type]

    result, message = await provisioner.reinstall_packages(handle, ["requests"])

    assert result is ok
    assert fake.specs[0].kind == "pip" and fake.specs[0].timeout_sec == 600
    assert fake.specs[0].packages == ["requests"]
    if not ok:
        assert "오류: 찾을 수 없다" in message
        assert ("시간 초과" in message) is timed_out
