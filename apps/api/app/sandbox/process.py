import asyncio
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

from app.sandbox.base import SandboxError

# 플랫폼 분기는 이 모듈에만 둔다(8.4). 다른 모듈은 이 값을 인자로 받는다.
IS_WINDOWS = sys.platform == "win32"

# PowerShell 자체가 필요로 하는 변수. 값은 플랫폼 프로세스의 것을 그대로 넘긴다(3.6).
WINDOWS_INHERITED_ENV = (
    "SystemRoot",
    "windir",
    "SystemDrive",
    "ComSpec",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "ProgramData",
    "PSModulePath",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


@dataclass(frozen=True)
class ProcessLimits:
    memory_bytes: int
    max_processes: int


@dataclass(frozen=True)
class ProcessStats:
    peak_memory_bytes: int | None
    active_processes: int | None
    total_processes: int | None


class RunningProcess:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.pid = proc.pid
        self.started_at = time.monotonic()


class ProcessRunner(Protocol):
    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        stdin: bool,
        limits: ProcessLimits,
    ) -> RunningProcess: ...

    async def kill(self, p: RunningProcess) -> None:
        """프로세스 트리 전체를 끝낸다. 이미 끝났으면 남은 자손만 정리한다."""
        ...

    def stats(self, p: RunningProcess) -> ProcessStats: ...

    def close(self, p: RunningProcess) -> None:
        """핸들을 놓는다. 남은 자손이 있으면 함께 끝난다."""
        ...


async def start_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin: bool,
    **platform_kwargs: Any,
) -> asyncio.subprocess.Process:
    if not argv:
        raise SandboxError("sandbox_unavailable", "실행할 명령이 없다")
    try:
        return await asyncio.create_subprocess_exec(
            argv[0],
            *argv[1:],
            cwd=cwd,
            env=dict(env),
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **platform_kwargs,
        )
    except NotImplementedError as exc:
        # Windows의 SelectorEventLoop(uvicorn --reload)는 서브프로세스를 지원하지 않는다.
        raise SandboxError(
            "sandbox_unavailable",
            "이 이벤트 루프는 서브프로세스를 띄울 수 없다. --reload 없이 서버를 띄운다",
        ) from exc
    except OSError as exc:
        raise SandboxError("sandbox_unavailable", f"{argv[0]}을 실행하지 못했다: {exc}") from exc


def make_runner() -> ProcessRunner:
    if sys.platform == "win32":
        import ctypes

        from app.sandbox.jobobject_win import JobObjectRunner, Kernel32

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return JobObjectRunner(Kernel32(kernel32, ctypes.get_last_error))
    import os
    import resource
    import signal

    from app.sandbox.proc_posix import PosixOps, PosixRunner

    return PosixRunner(
        PosixOps(
            killpg=os.killpg,
            getuid=os.getuid,
            getrlimit=resource.getrlimit,
            setrlimit=resource.setrlimit,
            rlimit_as=resource.RLIMIT_AS,
            rlimit_nproc=resource.RLIMIT_NPROC,
            rlim_infinity=resource.RLIM_INFINITY,
            sigterm=signal.SIGTERM,
            sigkill=signal.SIGKILL,
        )
    )


def _lookup(env: Mapping[str, str], name: str, *, windows: bool) -> str | None:
    value = env.get(name)
    if value is not None or not windows:
        return value
    # os.environ은 Windows에서 대소문자를 가리지 않지만 테스트가 넘기는 dict는 그렇지 않다.
    folded = name.casefold()
    for key, candidate in env.items():
        if key.casefold() == folded:
            return candidate
    return None


def _same_dir(a: str, b: str, *, windows: bool) -> bool:
    a = a.strip().strip('"').rstrip("\\/")
    b = b.strip().strip('"').rstrip("\\/")
    if windows:
        return a.replace("/", "\\").casefold() == b.replace("/", "\\").casefold()
    return a == b


def build_env(
    *,
    workspace: Path,
    cwd: Path,
    sandbox_python_dir: Path,
    pip_cache_dir: Path,
    agent_env: Mapping[str, str],
    parent_env: Mapping[str, str],
    windows: bool,
    excluded_path_dirs: Sequence[Path] = (),
) -> dict[str, str]:
    """도구 프로세스 환경(3.6). 부모 환경을 상속하지 않고 필요한 값만 골라 만든다."""
    sep = ";" if windows else ":"
    packages = workspace / ".packages"
    tmp = workspace / ".tmp"
    env: dict[str, str] = {}
    head: list[str] = []
    if windows:
        for name in WINDOWS_INHERITED_ENV:
            value = _lookup(parent_env, name, windows=True)
            if value is not None:
                env[name] = value
        env["TEMP"] = env["TMP"] = str(tmp)
        # Git for Windows의 usr\bin이 PATH 앞쪽에 있으면 find·sort·timeout 같은 GNU 명령이
        # 같은 이름의 Windows 명령을 가린다. 셸이 PowerShell이므로 Windows 명령이 먼저다.
        system_root = env.get("SystemRoot") or env.get("windir")
        if system_root:
            head = [str(PureWindowsPath(system_root, "System32")), system_root]
        head += [str(packages / "bin"), str(packages / "Scripts"), str(sandbox_python_dir)]
    else:
        env["LANG"] = "C.UTF-8"
        env["TMPDIR"] = str(tmp)
        head = [str(packages / "bin"), str(sandbox_python_dir)]

    # 플랫폼 venv(uv run이 PATH 앞에 넣는다)는 도구에 보이지 않게 뺀다(S15).
    # 앞에 이미 넣은 디렉터리도 뒤에서 다시 나오지 않게 뺀다.
    excluded = [*(str(path) for path in excluded_path_dirs), *head]
    parent_path = _lookup(parent_env, "PATH", windows=windows) or ""
    tail = [
        entry
        for entry in parent_path.split(sep)
        if entry.strip() and not any(_same_dir(entry, ex, windows=windows) for ex in excluded)
    ]
    env["PATH"] = sep.join([*head, *tail])
    env.update(
        {
            "SANDBOX_WORKSPACE": str(workspace),
            "SANDBOX_WORKSPACE_CWD": str(cwd),
            "HOME": str(workspace),
            "PYTHONPATH": str(packages),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONSAFEPATH": "1",
            "PIP_CACHE_DIR": str(pip_cache_dir),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "MPLCONFIGDIR": str(workspace / ".cache" / "matplotlib"),
        }
    )

    for name, value in agent_env.items():
        if not name or "=" in name or "\0" in name or "\0" in value:
            raise SandboxError(
                "env_invalid", f"환경변수 이름이나 값이 올바르지 않다: {name[:40]!r}"
            )
        if windows:
            # Windows 환경 블록은 이름의 대소문자를 가리지 않아 같은 이름이 둘이면 안 된다.
            for existing in [key for key in env if key.casefold() == name.casefold()]:
                del env[existing]
        env[name] = value
    return env
