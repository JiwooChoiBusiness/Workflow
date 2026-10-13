"""Windows Job Object와 PowerShell의 실제 동작(설계 10장 M3의 Windows 검증 절차).

Windows에서만 돈다. 기본 실행에서는 빠지고 `uv run pytest -m windows`로 돌린다.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

import pytest

from app import sysinfo
from app.config import Settings
from app.sandbox.base import (
    ExecExited,
    ExecOutput,
    ExecSpec,
    OutputDecoder,
    SandboxError,
    WorkspaceHandle,
)
from app.sandbox.jobobject_win import SANDBOX_LIMIT_FLAGS, JobObject, Kernel32
from app.sandbox.local_workspace import LocalWorkspaceBackend
from app.sandbox.process import ProcessLimits, make_runner

pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object"),
]

Collected = tuple[str, str, ExecExited]
RunExec = Callable[..., Awaitable[Collected]]

HELPER = """\
import asyncio
import os
import sys
from pathlib import Path

from app.sandbox.process import ProcessLimits, make_runner


async def main() -> None:
    runner = make_runner()
    child = await runner.spawn(
        [sys.argv[1], "-c", "import time; time.sleep(120)"],
        cwd=Path.cwd(),
        env=dict(os.environ),
        stdin=False,
        limits=ProcessLimits(memory_bytes=256 * 2**20, max_processes=8),
    )
    print(child.pid, flush=True)
    await asyncio.sleep(120)


asyncio.run(main())
"""


@pytest.fixture
def kernel32() -> Kernel32:
    assert sys.platform == "win32"
    import ctypes

    return Kernel32(ctypes.WinDLL("kernel32", use_last_error=True), ctypes.get_last_error)


def base_python(settings: Settings) -> str:
    return str(sysinfo.sandbox_python(settings))


async def read_until(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, spec: ExecSpec, marker: str
) -> tuple[str, Callable[[], Awaitable[ExecExited]]]:
    """marker가 나올 때까지 stdout을 읽고, 나머지를 끝까지 읽는 함수를 돌려준다."""
    events = backend.exec(handle, spec).__aiter__()
    decoder = OutputDecoder()
    stdout = ""
    while marker not in stdout:
        event = await asyncio.wait_for(events.__anext__(), timeout=60)
        if isinstance(event, ExecOutput) and event.stream == "stdout":
            stdout += decoder.feed(event.data)
        assert not isinstance(event, ExecExited), stdout

    async def finish() -> ExecExited:
        async for event in events:
            if isinstance(event, ExecExited):
                return event
        raise AssertionError("ExecExited가 오지 않았다")

    return stdout, finish


def test_limits_are_set_on_the_job(kernel32: Kernel32) -> None:
    job = JobObject.create(kernel32, ProcessLimits(memory_bytes=1024 * 2**20, max_processes=64))
    try:
        info = kernel32.query_limits(job.handle)
        assert info.BasicLimitInformation.LimitFlags == SANDBOX_LIMIT_FLAGS
        assert info.BasicLimitInformation.ActiveProcessLimit == 64
        assert info.JobMemoryLimit == 1024 * 2**20
    finally:
        job.close()


async def test_runner_assigns_terminates_and_reports_peak_memory(tmp_path: Path) -> None:
    runner = make_runner()
    python = base_python(Settings.model_validate({"data_dir": tmp_path}))
    code = "import time\nx = bytearray(64 * 2**20)\nprint('ready', flush=True)\ntime.sleep(60)"
    process = await runner.spawn(
        [python, "-c", code],
        cwd=tmp_path,
        env=dict(os.environ),
        stdin=False,
        limits=ProcessLimits(memory_bytes=512 * 2**20, max_processes=8),
    )
    try:
        assert process.proc.stdout is not None
        line = await asyncio.wait_for(process.proc.stdout.readline(), timeout=30)
        assert line.strip() == b"ready"
        stats = runner.stats(process)
        # CREATE_NO_WINDOW로 만든 콘솔의 conhost.exe도 Job 계정에 잡힌다.
        assert stats.active_processes is not None and stats.active_processes >= 1
        assert stats.peak_memory_bytes is not None and stats.peak_memory_bytes >= 64 * 2**20

        started = time.monotonic()
        await runner.kill(process)
        await asyncio.wait_for(process.proc.wait(), timeout=5)
        assert time.monotonic() - started < 1
        assert process.proc.returncode == 1
    finally:
        runner.close(process)


async def test_start_process_grandchild_dies_with_the_call(
    backend: LocalWorkspaceBackend,
    handle: WorkspaceHandle,
    wait_gone: Callable[..., Awaitable[list[int]]],
) -> None:
    # 수동 검증 2번. 바탕화면에 창이 뜨지 않게 -WindowStyle Hidden을 더했다.
    script = (
        "$p = Start-Process powershell -ArgumentList '-c','Start-Sleep 60' "
        '-WindowStyle Hidden -PassThru\nWrite-Output "child=$($p.Id)"\nStart-Sleep 60'
    )
    spec = ExecSpec(exec_id="grandchild", kind="shell", script=script)
    stdout, finish = await read_until(backend, handle, spec, "\n")
    child = int(stdout.split("=")[1])

    killed_at = time.monotonic()
    assert await backend.kill(handle, "grandchild")
    exited = await finish()

    assert exited.killed
    assert await wait_gone([child], 1.0) == []
    assert time.monotonic() - killed_at < 2


async def test_memory_error_leaves_the_platform_alone(run_exec: RunExec) -> None:
    # 수동 검증 3번. Job 상한(1024MB) 때문에 커밋이 바로 실패하므로 실제로 메모리를 채우지 않는다.
    before = sysinfo.rss_bytes()

    stdout, stderr, exited = await run_exec(
        "data = bytearray(2 * 1024**3)\nprint('allocated')", kind="python"
    )

    after = sysinfo.rss_bytes()
    assert exited.code == 1
    assert "MemoryError" in stderr and "allocated" not in stdout
    assert exited.duration_ms < 10_000
    assert before is not None and after is not None
    assert abs(after - before) < 50 * 2**20


async def test_active_process_limit(
    backend: LocalWorkspaceBackend,
    handle: WorkspaceHandle,
    wait_gone: Callable[..., Awaitable[list[int]]],
) -> None:
    # 수동 검증 4번을 상한 8로 줄여 자동화했다. 64개 그대로 한 결과는
    # docs/ops/sandbox-windows-check.md에 있다. cmd /c timeout은 손자(timeout.exe)에서
    # 상한에 걸릴 수도 있어 자식을 만들지 않는 파이썬을 띄운다. 5.1의 -ArgumentList는
    # 배열을 따옴표 없이 공백으로 이으므로 코드를 따옴표로 감싼 한 문자열로 준다.
    script = "\n".join(
        [
            "$ok = 0; $failed = 0",
            "1..20 | ForEach-Object {",
            "  try {",
            "    $p = Start-Process python -ArgumentList '-c \"import time; time.sleep(30)\"' "
            "-WindowStyle Hidden -PassThru -ErrorAction Stop",
            '    Write-Output "pid $($p.Id)"; $ok++',
            "  } catch { $failed++ }",
            "}",
            'Write-Output "ok=$ok failed=$failed"',
            "Start-Sleep 60",
        ]
    )
    spec = ExecSpec(exec_id="many", kind="shell", script=script, max_processes=8)
    stdout, finish = await read_until(backend, handle, spec, "failed=")
    summary = stdout[stdout.index("ok=") :].split()
    started = int(summary[0].split("=")[1])
    failed = int(summary[1].split("=")[1])
    pids = [int(line.split()[1]) for line in stdout.splitlines() if line.startswith("pid ")]

    assert failed > 0
    assert started < 8
    assert await backend.kill(handle, "many")
    exited = await finish()
    assert exited.killed
    assert await wait_gone(pids, 2.0) == []


async def test_korean_output_and_file_encoding(handle: WorkspaceHandle, run_exec: RunExec) -> None:
    # 수동 검증 5번.
    stdout, stderr, exited = await run_exec("Write-Output '한글'; python -c \"print('한글')\"")
    assert (stdout, exited.code) == ("한글\n한글\n", 0), stderr

    stdout, _, _ = await run_exec("'x' > a.txt; Get-Content a.txt -Encoding utf8")
    assert stdout == "x\n"
    # 5.1의 'utf8'은 BOM을 붙인다. UTF-16이 아니어야 한다.
    assert (handle.root / "a.txt").read_bytes() == b"\xef\xbb\xbfx\r\n"

    stdout, _, _ = await run_exec("Set-Content -Path b.txt -Value '한글'; Get-Content b.txt -Raw")
    assert stdout.strip() == "한글"
    assert (handle.root / "b.txt").read_bytes().startswith(b"\xef\xbb\xbf")


async def test_junction_made_by_the_shell_cannot_be_read_through(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, run_exec: RunExec
) -> None:
    # 수동 검증 6번.
    _, stderr, exited = await run_exec(
        r"New-Item -ItemType Junction -Path j -Target C:\Windows | Out-Null"
    )
    assert exited.code == 0, stderr
    try:
        assert (handle.root / "j" / "win.ini").exists()
        for path in ("j/win.ini", r"j\System32\drivers\etc\hosts"):
            with pytest.raises(SandboxError) as info:
                await backend.read_file(handle, path)
            assert info.value.code == "path_outside_workspace"
        with pytest.raises(SandboxError):
            await backend.write_file(handle, "j/evil.txt", b"x")
        listing = {entry.path: entry.type for entry in await backend.list_files(handle)}
        assert listing["j"] == "link"
    finally:
        # 정션만 지운다. 대상(C:\Windows)은 건드리지 않는다.
        (handle.root / "j").rmdir()


async def test_kill_on_job_close_when_the_platform_process_dies(
    tmp_path: Path,
    pid_alive: Callable[[int], bool],
    wait_gone: Callable[..., Awaitable[list[int]]],
) -> None:
    # 수동 검증 8번. 작업 관리자의 강제 종료 대신 테스트용 부모 프로세스를 taskkill /F로 끝낸다.
    helper = tmp_path / "helper.py"
    helper.write_text(HELPER, encoding="utf-8")
    python = base_python(Settings.model_validate({"data_dir": tmp_path}))
    parent = subprocess.Popen(
        [sys.executable, str(helper), python],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
    )
    child: int | None = None
    try:
        assert parent.stdout is not None
        line = await asyncio.wait_for(asyncio.to_thread(parent.stdout.readline), timeout=60)
        child = int(line)
        assert pid_alive(child)

        taskkill = shutil.which("taskkill")
        assert taskkill is not None
        subprocess.run([taskkill, "/F", "/PID", str(parent.pid)], capture_output=True, check=True)
        parent.wait(timeout=10)

        assert await wait_gone([child], 5.0) == []
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        for stream in (parent.stdout, parent.stderr):
            if stream is not None:
                stream.close()


async def test_platform_already_inside_a_job_can_still_assign(
    data_dir: Path, collect_exec: Callable[..., Awaitable[Collected]]
) -> None:
    # 이 테스트 자체가 터미널·IDE가 만든 Job 안에서 돌 수 있다.
    # 중첩 Job(Windows 8+)이 되면 통과한다.
    backend = LocalWorkspaceBackend(Settings.model_validate({"data_dir": data_dir}))
    try:
        handle = await backend.acquire(uuid4())
        stdout, _, exited = await collect_exec(
            backend, handle, ExecSpec(exec_id="nested", kind="shell", script="Write-Output ok")
        )
        assert (stdout, exited.code) == ("ok\n", 0)
        assert exited.peak_memory_bytes is not None and exited.peak_memory_bytes > 0
    finally:
        await backend.aclose()


async def test_encoded_mode_runs_without_a_script_file(
    data_dir: Path, collect_exec: Callable[..., Awaitable[Collected]]
) -> None:
    # 그룹 정책이 -File 실행을 막을 때 쓰는 폴백(3.6). 같은 프리앰블과 에필로그가 돈다.
    settings = Settings.model_validate(
        {"data_dir": data_dir, "sandbox_shell_mode": "encoded", "keep_run_scripts": True}
    )
    backend = LocalWorkspaceBackend(settings)
    try:
        handle = await backend.acquire(uuid4())
        stdout, _, exited = await collect_exec(
            backend,
            handle,
            ExecSpec(exec_id="enc", kind="shell", script="Write-Output '한글'; cmd /c exit 5"),
        )
        assert (stdout, exited.code) == ("한글\n", 5)
        assert sorted(p.name for p in (handle.root / ".run").iterdir()) == ["_bootstrap.py"]
        with pytest.raises(SandboxError) as info:
            await collect_exec(
                backend, handle, ExecSpec(exec_id="long", kind="shell", script="#" * 8001)
            )
        assert info.value.code == "command_too_long"
    finally:
        await backend.aclose()
