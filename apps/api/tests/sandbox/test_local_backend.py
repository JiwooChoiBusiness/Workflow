"""실제 프로세스를 띄우는 로컬 백엔드 테스트.

Windows(PowerShell + Job Object)와 Linux(bash + setsid) 양쪽에서 돈다. 셸 명령만 플랫폼별로 다르다.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.config import Settings
from app.sandbox import E2B_NOT_IMPLEMENTED, local_workspace, make_backend
from app.sandbox.base import (
    ExecExited,
    ExecOutput,
    ExecSpec,
    ExecStarted,
    OutputDecoder,
    SandboxBackend,
    SandboxError,
    WorkspaceHandle,
)
from app.sandbox.e2b_stub import E2BBackend
from app.sandbox.local_workspace import (
    SKELETON_DIRS,
    LocalWorkspaceBackend,
    purge_deleted,
    remove_workspace_dir,
)
from app.sandbox.process import ProcessLimits, ProcessStats, RunningProcess, make_runner
from app.sandbox.provisioning import AgentFileSource, SkillSource, WorkspaceProvisioner

WINDOWS = sys.platform == "win32"
Collected = tuple[str, str, ExecExited]
RunExec = Callable[..., Awaitable[Collected]]
Collect = Callable[[LocalWorkspaceBackend, WorkspaceHandle, ExecSpec], Awaitable[Collected]]


def sh(windows: str, posix: str) -> str:
    return windows if WINDOWS else posix


# 손자 프로세스를 하나 띄우고 그 pid를 출력한다.
SPAWN_SLEEPER = sh(
    "$p = Start-Process powershell -ArgumentList '-NoProfile','-Command','Start-Sleep 60' "
    "-WindowStyle Hidden -PassThru; Write-Output $p.Id",
    "sleep 60 &\necho $!",
)


def make_dir_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


async def test_acquire_builds_the_skeleton_once(backend: LocalWorkspaceBackend) -> None:
    session = uuid4()
    handle = await backend.acquire(session)
    root = handle.root

    assert root == (backend.workspaces_dir / str(session)).resolve()
    for name in SKELETON_DIRS:
        assert (root / name).is_dir(), name
    info = json.loads((root / ".platform" / "workspace.json").read_text(encoding="utf-8"))
    assert info["session_id"] == str(session)
    assert (root / ".run" / "_bootstrap.py").is_file()

    again = await backend.acquire(session)
    assert again == handle
    assert json.loads((root / ".platform" / "workspace.json").read_text(encoding="utf-8")) == info


async def test_korean_output_is_utf8(run_exec: RunExec) -> None:
    script = sh(
        "Write-Output '한글'; python -c \"print('파이썬 한글')\"",
        "echo 한글; python3 -c \"print('파이썬 한글')\"",
    )

    stdout, stderr, exited = await run_exec(script)

    assert exited.code == 0, stderr
    assert stdout == "한글\n파이썬 한글\n"


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("exit 3", 3),
        (sh("cmd /c exit 7", "sh -c 'exit 7'"), 7),
        (sh("Get-Item -LiteralPath missing.txt", "false"), 1),
        (sh("Write-Output ok", "echo ok"), 0),
    ],
)
async def test_exit_codes(run_exec: RunExec, script: str, expected: int) -> None:
    _, _, exited = await run_exec(script)

    assert exited.code == expected
    assert not exited.timed_out and not exited.killed


async def test_stderr_is_a_separate_stream(run_exec: RunExec) -> None:
    stdout, stderr, _ = await run_exec(sh("[Console]::Error.WriteLine('경고')", "echo 경고 >&2"))

    assert stdout == ""
    assert stderr == "경고\n"


async def test_timeout_kills_the_command(run_exec: RunExec) -> None:
    started = time.monotonic()

    _, _, exited = await run_exec(sh("Start-Sleep 30", "sleep 30"), timeout_sec=2)

    assert exited.timed_out
    assert exited.code is None
    assert time.monotonic() - started < 15


async def test_kill_ends_the_whole_process_tree(
    backend: LocalWorkspaceBackend,
    handle: WorkspaceHandle,
    wait_gone: Callable[..., Awaitable[list[int]]],
) -> None:
    spec = ExecSpec(
        exec_id="kill_me",
        kind="shell",
        script=f"{SPAWN_SLEEPER}\n{sh('Start-Sleep 60', 'sleep 60')}",
    )
    decoder = OutputDecoder()
    stdout = ""
    pids: list[int] = []
    exited: ExecExited | None = None
    killed_at = 0.0
    async for event in backend.exec(handle, spec):
        if isinstance(event, ExecStarted):
            pids.append(event.pid)
        elif isinstance(event, ExecOutput) and event.stream == "stdout":
            stdout += decoder.feed(event.data)
            if len(pids) == 1 and "\n" in stdout:
                pids.append(int(stdout.split()[0]))
                killed_at = time.monotonic()
                assert await backend.kill(handle, "kill_me")
        elif isinstance(event, ExecExited):
            exited = event

    assert exited is not None and exited.killed and exited.code is None
    # A11(c): 런을 멈추면 손자 프로세스까지 1초 안에 사라진다(POSIX 폴백은 SIGTERM 유예가 있다).
    assert await wait_gone(pids, 1.0 if WINDOWS else 3.0) == []
    assert time.monotonic() - killed_at < 5
    assert not await backend.kill(handle, "kill_me")


async def test_background_children_end_with_the_tool_call(
    run_exec: RunExec, wait_gone: Callable[..., Awaitable[list[int]]]
) -> None:
    started = time.monotonic()

    stdout, stderr, exited = await run_exec(SPAWN_SLEEPER)

    assert exited.code == 0, stderr
    # 손자가 파이프를 쥐고 있어도 본 프로세스가 끝나면 호출이 끝난다.
    assert time.monotonic() - started < 20
    assert await wait_gone([int(stdout.split()[0])], 3.0) == []


async def test_memory_limit_turns_big_allocation_into_memory_error(run_exec: RunExec) -> None:
    stdout, stderr, exited = await run_exec(
        "data = bytearray(2 * 1024**3)\nprint('allocated')", kind="python", timeout_sec=60
    )

    assert exited.code == 1
    assert "MemoryError" in stderr
    assert "allocated" not in stdout


async def test_run_python_isolation(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, run_exec: RunExec
) -> None:
    await backend.write_file(handle, ".packages/sandbox_probe_mod.py", b"VALUE = 'from packages'\n")
    await backend.write_file(handle, "json.py", b"raise SystemExit('workspace json.py was used')\n")
    code = "\n".join(
        [
            "import json, os, sys, sandbox_probe_mod",
            "print(sandbox_probe_mod.VALUE)",
            "print(json.__file__)",
            "print(os.getcwd())",
            "print(sys.flags.isolated, sys.prefix == sys.base_prefix)",
            "print(__name__)",
        ]
    )

    stdout, stderr, exited = await run_exec(code, kind="python")

    assert exited.code == 0, stderr
    lines = stdout.splitlines()
    assert lines[0] == "from packages"
    assert not Path(lines[1]).resolve().is_relative_to(handle.root)
    assert Path(lines[2]).resolve() == handle.root
    # 플랫폼 venv가 아닌 기반 인터프리터를 격리 모드로 쓴다(S15).
    assert lines[3] == "1 True"
    assert lines[4] == "__main__"


async def test_run_python_exit_status_and_cwd(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, run_exec: RunExec
) -> None:
    await backend.write_file(handle, "sub/data.txt", "자료".encode())

    stdout, _, exited = await run_exec(
        "import sys\nprint(open('data.txt', encoding='utf-8').read())\nsys.exit(4)",
        kind="python",
        cwd="sub",
    )

    assert exited.code == 4
    assert stdout == "자료\n"


async def test_environment_is_minimal(run_exec: RunExec, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEYS", "AIza-not-a-real-key")
    monkeypatch.setenv("SANDBOX_TEST_LEAK", "leak")
    script = sh("Get-ChildItem env: | ForEach-Object { $_.Name }", "env | cut -d= -f1")

    stdout, _, exited = await run_exec(script, env={"AGENT_SETTING": "x"})

    assert exited.code == 0
    names = {line.strip().upper() for line in stdout.splitlines() if line.strip()}
    assert "GEMINI_API_KEYS" not in names
    assert "SANDBOX_TEST_LEAK" not in names
    assert "VIRTUAL_ENV" not in names
    assert {"AGENT_SETTING", "SANDBOX_WORKSPACE", "SANDBOX_WORKSPACE_CWD", "PYTHONPATH"} <= names


async def test_platform_venv_is_not_on_the_tool_path(run_exec: RunExec) -> None:
    stdout, _, _ = await run_exec(sh("$env:PATH", 'echo "$PATH"'))

    entries = [entry for entry in stdout.strip().split(os.pathsep) if entry]
    venv = Path(sys.prefix).resolve()
    if sys.prefix != sys.base_prefix:
        assert not any(Path(entry).resolve().is_relative_to(venv) for entry in entries)
    if WINDOWS:
        system_root = Path(os.environ["SystemRoot"])
        assert [Path(entry) for entry in entries[:2]] == [system_root / "System32", system_root]
        entries = entries[2:]
    assert Path(entries[0]).name == "bin"


@pytest.mark.skipif(not WINDOWS, reason="Windows 명령과 Git의 GNU 명령이 겹치는 문제")
async def test_windows_commands_win_over_same_named_gnu_tools(run_exec: RunExec) -> None:
    stdout, _, exited = await run_exec("(Get-Command sort.exe, find.exe).Source")

    assert exited.code == 0
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    assert [Path(line.strip()).parent for line in stdout.splitlines() if line.strip()] == [
        system32,
        system32,
    ]


async def test_cwd_must_be_an_existing_directory_inside(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, run_exec: RunExec
) -> None:
    (handle.root / "sub").mkdir()
    stdout, _, _ = await run_exec(sh("(Get-Location).Path", "pwd"), cwd="sub")
    assert Path(stdout.strip()).resolve() == handle.root / "sub"

    for cwd, code in (("missing", "not_found"), ("..", "path_outside_workspace")):
        with pytest.raises(SandboxError) as info:
            await run_exec("exit 0", cwd=cwd)
        assert info.value.code == code


async def test_kill_before_the_process_is_up(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle
) -> None:
    events = backend.exec(
        handle, ExecSpec(exec_id="early", kind="shell", script=sh("Start-Sleep 30", "sleep 30"))
    )
    first = asyncio.ensure_future(anext(events))
    # 제너레이터가 exec_id 자리를 잡고 프로세스를 띄우는 중일 때 멈춤 요청이 온다.
    await asyncio.sleep(0)
    started = time.monotonic()

    assert await backend.kill(handle, "early")
    assert isinstance(await first, ExecStarted)
    remaining = [event async for event in events]

    exited = remaining[-1]
    assert isinstance(exited, ExecExited) and exited.killed
    assert time.monotonic() - started < 10


async def test_duplicate_exec_id_is_rejected(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, collect_exec: Collect
) -> None:
    long_running = ExecSpec(exec_id="same", kind="shell", script=sh("Start-Sleep 30", "sleep 30"))
    events = backend.exec(handle, long_running)
    started = await anext(events)
    assert isinstance(started, ExecStarted)
    try:
        with pytest.raises(SandboxError) as info:
            await collect_exec(
                backend, handle, ExecSpec(exec_id="same", kind="shell", script="exit 0")
            )
        assert info.value.code == "exec_conflict"
        stats = await backend.stats(handle)
        assert [running.exec_id for running in stats.running] == ["same"]
        assert stats.running[0].pid == started.pid
    finally:
        assert await backend.kill(handle, "same")
        async for _ in events:
            pass


async def test_stdin_is_passed_as_utf8(run_exec: RunExec) -> None:
    # PowerShell 5.1의 $input은 시작 시점의 콘솔 코드 페이지(CP949)로 읽어 한글이 깨진다.
    # 프리앰블이 인코딩을 바꾼 뒤 다시 만들어지는 [Console]::In은 UTF-8로 읽는다.
    script = sh(
        '$text = [Console]::In.ReadToEnd(); Write-Output "got: $($text.Trim())"',
        'read line; echo "got: $line"',
    )

    stdout, _, _ = await run_exec(script, stdin="한글 입력\n")
    python_out, _, _ = await run_exec(
        "import sys\nprint('got:', sys.stdin.read().strip())", kind="python", stdin="한글 입력\n"
    )

    assert stdout == "got: 한글 입력\n"
    assert python_out == "got: 한글 입력\n"


async def test_run_scripts_are_removed_after_the_call(
    handle: WorkspaceHandle, run_exec: RunExec
) -> None:
    await run_exec("exit 0")
    await run_exec("print(1)", kind="python")

    assert sorted(path.name for path in (handle.root / ".run").iterdir()) == ["_bootstrap.py"]


async def test_run_scripts_can_be_kept_for_inspection(
    data_dir: Path, make_settings: Callable[..., Settings], collect_exec: Collect
) -> None:
    backend = LocalWorkspaceBackend(make_settings(data_dir, keep_run_scripts=True))
    try:
        handle = await backend.acquire(uuid4())
        await collect_exec(
            backend, handle, ExecSpec(exec_id="kept", kind="shell", script="echo '유지'")
        )
        script = handle.root / ".run" / ("kept.ps1" if WINDOWS else "kept.sh")
        data = script.read_bytes()
        if WINDOWS:
            # 5.1은 BOM이 없으면 시스템 코드 페이지로 읽는다(수동 검증 7번).
            assert data.startswith(b"\xef\xbb\xbf")
        assert "echo '유지'" in data.decode("utf-8-sig")
    finally:
        await backend.aclose()


async def test_exec_id_with_unsafe_characters_gets_a_safe_script_name(
    data_dir: Path, make_settings: Callable[..., Settings], collect_exec: Collect
) -> None:
    backend = LocalWorkspaceBackend(make_settings(data_dir, keep_run_scripts=True))
    try:
        handle = await backend.acquire(uuid4())
        stdout, _, _ = await collect_exec(
            backend,
            handle,
            ExecSpec(exec_id="../call:1", kind="shell", script="echo ok"),
        )
        assert stdout.strip() == "ok"
        names = [p.name for p in (handle.root / ".run").iterdir() if p.name != "_bootstrap.py"]
        assert len(names) == 1 and names[0].startswith("x")
    finally:
        await backend.aclose()


@pytest.mark.parametrize("exec_id", ["_bootstrap", "CON", "nul"])
async def test_exec_ids_never_overwrite_platform_files(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, collect_exec: Collect, exec_id: str
) -> None:
    stdout, _, exited = await collect_exec(
        backend, handle, ExecSpec(exec_id=exec_id, kind="python", script="print('ok')")
    )

    assert (stdout.strip(), exited.code) == ("ok", 0)
    bootstrap = (handle.root / ".run" / "_bootstrap.py").read_text(encoding="utf-8")
    assert "runpy.run_path" in bootstrap


async def test_invalid_pip_spec_fails_before_anything_runs(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle
) -> None:
    events: list[object] = []
    with pytest.raises(SandboxError) as info:
        async for event in backend.exec(
            handle, ExecSpec(exec_id="pip1", kind="pip", packages=["requests", "--index-url=x"])
        ):
            events.append(event)

    assert info.value.code == "pip_spec_invalid"
    assert events == []


def build_wheel(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    wheel = directory / "sandboxpkg-0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("sandboxpkg/__init__.py", "VALUE = 42\n")
        archive.writestr(
            "sandboxpkg-0.1.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: sandboxpkg\nVersion: 0.1\n",
        )
        archive.writestr(
            "sandboxpkg-0.1.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            "sandboxpkg-0.1.dist-info/RECORD",
            "sandboxpkg/__init__.py,,\nsandboxpkg-0.1.dist-info/METADATA,,\n"
            "sandboxpkg-0.1.dist-info/WHEEL,,\nsandboxpkg-0.1.dist-info/RECORD,,\n",
        )
    return wheel


def sandbox_pip_available(settings: Settings) -> bool:
    from app import sysinfo

    return sysinfo.check_pip(settings).ok


async def test_install_packages_offline_from_a_local_wheel(
    backend: LocalWorkspaceBackend,
    handle: WorkspaceHandle,
    run_exec: RunExec,
    make_settings: Callable[..., Settings],
    tmp_path: Path,
) -> None:
    if not sandbox_pip_available(make_settings(tmp_path)):
        pytest.skip("샌드박스 파이썬에 pip이 없다")
    wheels = tmp_path / "wheels"
    build_wheel(wheels)
    # 네트워크 없이 로컬 휠만 쓰도록 에이전트 환경변수로 pip을 묶는다.
    offline = {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(wheels)}

    stdout, stderr, exited = await run_exec(
        kind="pip", packages=["sandboxpkg==0.1"], env=offline, timeout_sec=300
    )

    assert exited.code == 0, stdout + stderr
    assert (handle.root / ".packages" / "sandboxpkg" / "__init__.py").is_file()
    requirements = (handle.root / ".platform" / "requirements.txt").read_text(encoding="utf-8")
    assert requirements == "sandboxpkg==0.1\n"
    stdout, _, _ = await run_exec("import sandboxpkg\nprint(sandboxpkg.VALUE)", kind="python")
    assert stdout == "42\n"

    _, _, failed = await run_exec(
        kind="pip", packages=["sandboxpkg==9.9"], env=offline, timeout_sec=300
    )
    assert failed.code != 0
    # 실패한 설치는 이력에 남기지 않는다.
    assert (handle.root / ".platform" / "requirements.txt").read_text(
        encoding="utf-8"
    ) == requirements


async def test_write_read_round_trip(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle
) -> None:
    data = "a,b\n한글,2\n".encode()

    assert await backend.write_file(handle, "outputs/report.csv", data) == len(data)
    content = await backend.read_file(handle, "outputs/report.csv")

    assert content.encoding == "utf-8"
    assert content.content == "a,b\n한글,2\n"
    assert content.size == len(data)
    assert not content.truncated and content.next_offset is None

    await backend.write_file(handle, "outputs/report.csv", "끝\n".encode(), append=True)
    appended = await backend.read_file(handle, "outputs/report.csv")
    assert appended.content.endswith("끝\n")


async def test_read_pages_never_split_a_character(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle
) -> None:
    text = "가" * 30_000 + "끝"
    await backend.write_file(handle, "big.txt", text.encode())

    pieces: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = await backend.read_file(handle, "big.txt", offset=offset, limit=65536)
        assert "\ufffd" not in page.content
        pieces.append(page.content)
        offset = page.next_offset
    assert "".join(pieces) == text
    assert len(pieces) == 2


async def test_binary_files_come_back_as_base64(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle
) -> None:
    import base64

    data = bytes(range(256)) * 4
    await backend.write_file(handle, "outputs/blob.bin", data)

    content = await backend.read_file(handle, "outputs/blob.bin", limit=100)

    assert content.encoding == "base64"
    assert base64.b64decode(content.content) == data[:100]
    assert content.truncated and content.next_offset == 100


@pytest.mark.parametrize(
    ("operation", "path", "code"),
    [
        ("write", ".platform/requirements.txt", "path_forbidden"),
        ("write", ".run/x.ps1", "path_forbidden"),
        ("write", "../outside.txt", "path_outside_workspace"),
        ("read", "../../app.db", "path_outside_workspace"),
        ("read", "missing.txt", "not_found"),
        ("read", "outputs", "is_a_directory"),
        ("write", "outputs", "is_a_directory"),
        ("read", "C:/Windows/win.ini", "path_invalid"),
    ],
)
async def test_file_tool_errors(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, operation: str, path: str, code: str
) -> None:
    with pytest.raises(SandboxError) as info:
        if operation == "write":
            await backend.write_file(handle, path, b"x")
        else:
            await backend.read_file(handle, path)
    assert info.value.code == code


async def test_write_without_mkdirs_needs_the_parent(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle
) -> None:
    with pytest.raises(SandboxError) as info:
        await backend.write_file(handle, "new/dir/a.txt", b"x", mkdirs=False)
    assert info.value.code == "not_found"
    assert await backend.write_file(handle, "new/dir/a.txt", b"x") == 1


async def test_list_and_stat(backend: LocalWorkspaceBackend, handle: WorkspaceHandle) -> None:
    await backend.write_file(handle, "outputs/a.txt", b"12345")
    await backend.write_file(handle, "outputs/nested/b.txt", b"1")
    await backend.write_file(handle, ".hidden.txt", b"1")

    top = {entry.path: entry.type for entry in await backend.list_files(handle)}
    assert top == {"downloads": "dir", "outputs": "dir"}
    hidden = {entry.path for entry in await backend.list_files(handle, include_hidden=True)}
    assert {".platform", ".run", ".hidden.txt"} <= hidden
    nested = await backend.list_files(handle, "outputs", recursive=True)
    assert [entry.path for entry in nested] == [
        "outputs/a.txt",
        "outputs/nested",
        "outputs/nested/b.txt",
    ]
    assert nested[0].size == 5
    assert len(await backend.list_files(handle, "outputs", recursive=True, max_entries=2)) == 2

    file_stat = await backend.stat(handle, "outputs/a.txt")
    assert (file_stat.type, file_stat.size) == ("file", 5)
    assert (await backend.stat(handle, "outputs")).type == "dir"
    with pytest.raises(SandboxError) as info:
        await backend.stat(handle, "nope")
    assert info.value.code == "not_found"
    with pytest.raises(SandboxError) as info:
        await backend.list_files(handle, "outputs/a.txt")
    assert info.value.code == "not_a_directory"


async def test_changes_skip_platform_and_input_folders(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle
) -> None:
    await backend.write_file(handle, "old.txt", b"old")
    old = handle.root / "old.txt"
    past = time.time() - 3600
    os.utime(old, (past, past))
    since = datetime.now(UTC) - timedelta(seconds=2)
    for path in (
        "outputs/result.csv",
        "notes.md",
        "deep/a/b/c.txt",
        ".packages/pkg/mod.py",
        "skills/s/SKILL.md",
        "agent-files/ref.pdf",
        "uploads/m1/in.txt",
        "task/inputs.json",
        ".cache/x",
        "src/__pycache__/m.cpython-312.pyc",
        "src/m.pyc",
        ".git/HEAD",
        "node_modules/x/index.js",
    ):
        await backend.write_file(handle, path, b"x")

    changes = await backend.changes(handle, since)

    assert sorted(entry.path for entry in changes.entries) == [
        "deep/a/b/c.txt",
        "notes.md",
        "outputs/result.csv",
    ]
    assert not changes.truncated
    limited = await backend.changes(handle, since, max_entries=2)
    assert len(limited.entries) == 2 and limited.truncated


async def test_links_are_listed_but_not_followed(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("남아 있어야 한다", encoding="utf-8")
    make_dir_link(handle.root / "link", outside)

    listing = {entry.path: entry.type for entry in await backend.list_files(handle, recursive=True)}
    assert listing["link"] == "link"
    assert "link/keep.txt" not in listing
    with pytest.raises(SandboxError) as info:
        await backend.read_file(handle, "link/keep.txt")
    assert info.value.code == "path_outside_workspace"
    assert (await backend.changes(handle, datetime.now(UTC) - timedelta(days=1))).entries == []

    # 작업공간을 지워도 링크가 가리키는 바깥 파일은 남는다.
    await backend.release(handle, delete=True)
    assert not handle.root.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "남아 있어야 한다"


async def test_release_delete_and_missing_workspace(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle
) -> None:
    await backend.write_file(handle, "outputs/a.txt", b"x")
    os.chmod(handle.root / "outputs" / "a.txt", 0o444)

    await backend.release(handle, delete=True)

    assert not handle.root.exists()
    with pytest.raises(SandboxError) as info:
        await backend.read_file(handle, "outputs/a.txt")
    assert info.value.code == "workspace_missing"
    await backend.release(handle, delete=True)


def test_locked_workspace_is_renamed_and_purged_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspaces" / "s1"
    (root / "x").mkdir(parents=True)
    real_rmtree = shutil.rmtree
    calls: list[str] = []

    def locked(path: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(str(path))
        raise PermissionError("locked")

    monkeypatch.setattr(shutil, "rmtree", locked)
    renamed = remove_workspace_dir(root, sleep=lambda _: None)

    assert len(calls) == 4
    assert renamed is not None and renamed.name.startswith(".deleted-")
    assert not root.exists() and renamed.exists()
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    assert purge_deleted(tmp_path / "workspaces") == 1
    assert not renamed.exists()


async def test_backend_purge_deleted_clears_renamed_workspaces(
    backend: LocalWorkspaceBackend,
) -> None:
    kept = await backend.acquire(uuid4())
    leftover = backend.workspaces_dir / f"{local_workspace.DELETED_PREFIX}1-old"
    (leftover / "x").mkdir(parents=True)

    assert await backend.purge_deleted() == 1
    assert not leftover.exists()
    assert kept.root.is_dir()
    assert await backend.purge_deleted() == 0


async def test_stats_reports_disk_and_last_exec(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, run_exec: RunExec
) -> None:
    before = await backend.stats(handle)
    assert before.last_exec_at is None and before.running == []
    await backend.write_file(handle, "outputs/a.bin", b"x" * 10_000)
    await run_exec("exit 0")

    after = await backend.stats(handle)

    assert after.disk_bytes >= 10_000 + before.disk_bytes
    assert not after.disk_estimated
    assert after.last_exec_at is not None
    assert after.active_processes == 0


async def test_handles_from_elsewhere_are_rejected(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, tmp_path: Path
) -> None:
    forged = handle.model_copy(update={"root": tmp_path})

    with pytest.raises(SandboxError) as info:
        await backend.read_file(forged, "x.txt")
    assert info.value.code == "workspace_invalid"


async def test_provisioner_fills_agent_files_and_skills(
    backend: LocalWorkspaceBackend, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    (store / "f1").write_bytes("참고 자료".encode())
    (store / "empty").write_bytes(b"")
    package = store / "skill.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("csv-tools/SKILL.md", "---\nname: csv-tools\n---\n본문\n")
        archive.writestr("csv-tools/scripts/run.py", "print('hi')\n")

    class Content:
        async def agent_files(self) -> list[AgentFileSource]:
            return [
                AgentFileSource(filename="참고/자료.txt", path=store / "f1"),
                AgentFileSource(filename="empty.txt", path=store / "empty"),
            ]

        async def skills(self) -> list[SkillSource]:
            return [SkillSource(slug="csv-tools", package=package)]

    provisioner = WorkspaceProvisioner(backend)
    session = uuid4()
    first = await provisioner.ensure(session, instance_created_at=None, content=Content())
    root = first.handle.root

    assert (first.state, first.agent_files, first.skills) == ("created", 2, 1)
    assert first.created_at is not None
    assert (root / "agent-files" / "참고" / "자료.txt").read_bytes() == "참고 자료".encode()
    assert (root / "agent-files" / "empty.txt").read_bytes() == b""
    assert (root / "skills" / "csv-tools" / "SKILL.md").is_file()
    assert (root / "skills" / "csv-tools" / "scripts" / "run.py").is_file()

    second = await provisioner.ensure(
        session, instance_created_at=first.created_at, content=Content()
    )
    assert second.state == "reused" and second.events == []
    # DB가 시간대 없는 UTC로 돌려줘도 같은 폴더로 본다.
    naive = first.created_at.replace(tzinfo=None)
    assert (await provisioner.ensure(session, instance_created_at=naive)).state == "reused"

    shutil.rmtree(root)
    emitted: list[str] = []

    async def emit(event: Any) -> None:
        emitted.append(event.type)

    third = await provisioner.ensure(
        session, instance_created_at=first.created_at, content=Content(), emit=emit
    )
    assert third.state == "recreated"
    assert emitted == ["workspace.recreated"]
    assert (root / "skills" / "csv-tools" / "SKILL.md").is_file()


async def test_skill_package_without_skill_md_is_rejected(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, tmp_path: Path
) -> None:
    package = tmp_path / "bad.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("a/readme.md", "x")
        archive.writestr("b/other.md", "x")

    with pytest.raises(SandboxError) as info:
        await WorkspaceProvisioner(backend).sync_skills(
            handle, [SkillSource(slug="bad", package=package)]
        )
    assert info.value.code == "skill_invalid"


def test_make_backend_selects_by_setting(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    local = make_backend(make_settings(tmp_path))
    assert isinstance(local, LocalWorkspaceBackend)

    with pytest.raises(NotImplementedError, match=E2B_NOT_IMPLEMENTED):
        make_backend(make_settings(tmp_path, sandbox_backend="e2b", e2b_api_key="e2b-test"))


async def test_e2b_stub_matches_the_protocol(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        E2BBackend(make_settings(tmp_path))
    stub: SandboxBackend = E2BBackend(make_settings(tmp_path, e2b_api_key="e2b-test"))
    local: SandboxBackend = LocalWorkspaceBackend(make_settings(tmp_path))

    assert (stub.name, local.name) == ("e2b", "local")
    assert await stub.purge_deleted() == 0
    await stub.aclose()
    with pytest.raises(NotImplementedError, match=E2B_NOT_IMPLEMENTED):
        await stub.acquire(uuid4())
    with pytest.raises(NotImplementedError):
        stub.exec(
            WorkspaceHandle(session_id=uuid4(), root=tmp_path, backend="e2b"),
            ExecSpec(exec_id="x", kind="shell"),
        )


def test_sandbox_python_used_by_tests_exists(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    from app import sysinfo

    python = sysinfo.sandbox_python(make_settings(tmp_path))
    assert python.is_file()
    result = subprocess.run(
        [str(python), "-I", "-c", "import sys; print(sys.prefix == sys.base_prefix)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "True"


async def test_concurrent_reads_do_not_block_a_running_command(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle
) -> None:
    await backend.write_file(handle, "a.txt", b"a")
    spec = ExecSpec(exec_id="slow", kind="shell", script=sh("Start-Sleep 2; exit 0", "sleep 2"))

    async def consume() -> ExecExited | None:
        last: ExecExited | None = None
        async for event in backend.exec(handle, spec):
            if isinstance(event, ExecExited):
                last = event
        return last

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)
    started = time.monotonic()
    assert (await backend.read_file(handle, "a.txt")).content == "a"
    assert time.monotonic() - started < 1
    exited = await task
    assert exited is not None and exited.code == 0


def test_make_runner_matches_the_platform() -> None:
    expected = "JobObjectRunner" if WINDOWS else "PosixRunner"

    assert type(make_runner()).__name__ == expected


class CapturingRunner:
    """플랫폼 러너 대신 인자만 기록하고 무해한 프로세스를 띄운다."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path, dict[str, str], ProcessLimits]] = []

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        stdin: bool,
        limits: ProcessLimits,
    ) -> RunningProcess:
        self.calls.append((list(argv), cwd, dict(env), limits))
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "print('ran')",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return RunningProcess(proc)

    async def kill(self, p: RunningProcess) -> None:
        return None

    def stats(self, p: RunningProcess) -> ProcessStats:
        return ProcessStats(None, None, None)

    def close(self, p: RunningProcess) -> None:
        return None


async def test_posix_fallback_wiring_without_posix(
    data_dir: Path,
    make_settings: Callable[..., Settings],
    collect_exec: Collect,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 플랫폼 분기 값을 바꿔 Linux 폴백의 스크립트·인자·환경 조립을 어느 OS에서나 확인한다.
    # setsid·rlimit의 실제 효과는 Linux CI에서 위의 테스트들이 본다.
    monkeypatch.setattr(local_workspace, "IS_WINDOWS", False)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = CapturingRunner()
    backend = LocalWorkspaceBackend(
        make_settings(data_dir, sandbox_shell="powershell", keep_run_scripts=True), runner=runner
    )
    handle = await backend.acquire(uuid4())

    stdout, _, exited = await collect_exec(
        backend,
        handle,
        ExecSpec(exec_id="p1", kind="shell", script="echo 한글", memory_limit_mb=256),
    )

    argv, cwd, env, limits = runner.calls[0]
    script = handle.root / ".run" / "p1.sh"
    assert argv == ["/usr/bin/bash", "--noprofile", "--norc", str(script)]
    assert script.read_text(encoding="utf-8").splitlines() == [
        "set -o pipefail",
        'cd "$SANDBOX_WORKSPACE_CWD" || exit 1',
        "echo 한글",
    ]
    assert cwd == handle.root
    assert env["LANG"] == "C.UTF-8"
    assert env["TMPDIR"] == str(handle.root / ".tmp")
    assert "SystemRoot" not in env and "TEMP" not in env
    assert limits == ProcessLimits(memory_bytes=256 * 2**20, max_processes=64)
    assert (stdout.strip(), exited.code) == ("ran", 0)
    await backend.aclose()
