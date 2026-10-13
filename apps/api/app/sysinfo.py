import asyncio
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

# 작업공간 안의 깊은 패키지 경로가 260자 제한에 걸리지 않으려면 데이터 폴더가 짧아야 한다.
DATA_DIR_MAX_LENGTH = 80
# 그룹 정책이 이 값을 강제하면 -ExecutionPolicy Bypass를 줘도 -File 실행이 막힌다.
_BLOCKING_POLICIES = {"AllSigned", "Restricted"}
SUBPROCESS_CHECK_TIMEOUT_SEC = 10.0


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)
    # restype을 지정하지 않으면 의사 핸들 -1이 32비트 int로 잘린다.
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessMemoryCounters),
        wintypes.DWORD,
    ]
    _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def rss_bytes() -> int | None:
    if sys.platform == "win32":
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process = _kernel32.GetCurrentProcess()
        if not _psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return None
        return counters.WorkingSetSize
    try:
        with open("/proc/self/statm", "rb") as statm:
            resident_pages = int(statm.read().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return resident_pages * os.sysconf("SC_PAGE_SIZE")


def long_paths_enabled() -> bool | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
    except OSError:
        return False
    return value == 1


def default_sandbox_python() -> Path:
    # 플랫폼 venv의 python을 쓰면 서버 의존성이 도구 프로세스에 노출되므로 기반 인터프리터를 쓴다.
    if sys.platform == "win32":
        return Path(sys.base_prefix) / "python.exe"
    return Path(sys.base_prefix) / "bin" / "python3"


def sandbox_python(settings: Settings) -> Path:
    return settings.sandbox_python or default_sandbox_python()


def check_shell(settings: Settings) -> Check:
    path = shutil.which(settings.sandbox_shell)
    if path is None:
        return Check(
            "셸", False, f"{settings.sandbox_shell}을 찾지 못했다. shell 도구를 쓸 수 없다"
        )
    return Check("셸", True, path)


def check_python(settings: Settings) -> Check:
    python = sandbox_python(settings)
    if python.is_file():
        return Check("샌드박스 파이썬", True, str(python))
    return Check("샌드박스 파이썬", False, f"{python} 없음. SANDBOX_PYTHON을 확인한다")


def check_pip(settings: Settings) -> Check:
    python = sandbox_python(settings)
    if not python.is_file():
        return Check("pip", False, "샌드박스 파이썬이 없어 확인하지 못했다")
    try:
        # -I가 PYTHON* 환경변수를 무시하므로 출력 인코딩은 -X utf8로 고정한다.
        result = subprocess.run(
            [str(python), "-I", "-X", "utf8", "-m", "pip", "--version"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("pip", False, f"실행하지 못했다: {exc}")
    output = (result.stdout if result.returncode == 0 else result.stderr).strip()
    detail = output.splitlines()[-1] if output else f"종료 코드 {result.returncode}"
    return Check("pip", result.returncode == 0, detail)


def check_data_dir(settings: Settings) -> Check:
    length = len(str(settings.data_dir))
    detail = f"{settings.data_dir} ({length}자)"
    if length > DATA_DIR_MAX_LENGTH:
        return Check(
            "데이터 폴더",
            False,
            f"{detail}. {DATA_DIR_MAX_LENGTH}자를 넘으면 패키지 설치가 경로 길이 제한에"
            " 걸릴 수 있다. DATA_DIR을 C:\\wf-data 같은 짧은 경로로 둔다",
        )
    return Check("데이터 폴더", True, detail)


def check_long_paths(settings: Settings) -> Check:
    enabled = long_paths_enabled()
    if enabled is None:
        return Check("긴 경로 정책", True, "Windows가 아니라 해당 없음")
    if enabled:
        return Check("긴 경로 정책", True, "켜짐")
    if len(str(settings.data_dir)) <= DATA_DIR_MAX_LENGTH:
        return Check("긴 경로 정책", True, "꺼짐. 데이터 폴더가 짧아 괜찮다")
    return Check("긴 경로 정책", False, "꺼짐. 데이터 폴더를 짧은 경로로 옮긴다")


def check_execution_policy(settings: Settings) -> Check:
    name = "실행 정책"
    if settings.sandbox_shell == "bash":
        return Check(name, True, "PowerShell을 쓰지 않아 해당 없음")
    shell = shutil.which(settings.sandbox_shell)
    if shell is None:
        return Check(name, False, "PowerShell이 없어 확인하지 못했다")
    script = (
        "'MachinePolicy','UserPolicy' | ForEach-Object { \"$_=$(Get-ExecutionPolicy -Scope $_)\" }"
    )
    try:
        result = subprocess.run(
            [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(name, False, f"확인하지 못했다: {exc}")
    policies = dict(line.split("=", 1) for line in result.stdout.split() if "=" in line)
    if result.returncode != 0 or not policies:
        return Check(name, False, f"확인하지 못했다: {result.stderr.strip()[:200]}")
    detail = ", ".join(f"{scope}={policy}" for scope, policy in policies.items())
    blocked = _BLOCKING_POLICIES.intersection(policies.values())
    if blocked and settings.sandbox_shell_mode != "encoded":
        return Check(
            name, False, f"{detail}. 스크립트 실행이 막힌다. SANDBOX_SHELL_MODE=encoded로 바꾼다"
        )
    return Check(name, True, detail)


def check_port(port: int) -> Check:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError:
        return Check("포트", True, f"127.0.0.1:{port} 비어 있음")
    return Check(
        "포트", False, f"127.0.0.1:{port} 사용 중. 서버가 이미 떠 있거나 다른 프로그램이 쓴다"
    )


async def check_subprocess() -> Check:
    """지금 이벤트 루프에서 asyncio 서브프로세스(도구 실행 방식)가 뜨는지 본다.

    Windows의 SelectorEventLoop는 서브프로세스를 못 띄운다. 서버 안에서는 서버의 루프로
    불러야 의미가 있다.
    """
    name = "서브프로세스"
    loop = type(asyncio.get_running_loop()).__name__
    argv = ("cmd", "/c", "echo", "ok") if sys.platform == "win32" else ("sh", "-c", "echo ok")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except NotImplementedError:
        return Check(name, False, f"{loop}는 서브프로세스를 띄울 수 없어 도구 실행이 모두 실패한다")
    except OSError as exc:
        return Check(name, False, f"{loop}에서 {argv[0]}을 실행하지 못했다: {exc}")
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), SUBPROCESS_CHECK_TIMEOUT_SEC)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return Check(name, False, f"{loop}에서 {' '.join(argv)}이 끝나지 않았다")
    if proc.returncode != 0 or stdout.strip() != b"ok":
        return Check(name, False, f"{loop}에서 종료 코드 {proc.returncode}, 출력 {stdout[:40]!r}")
    return Check(name, True, f"{loop}에서 {' '.join(argv)} 실행됨")


def startup_checks(settings: Settings) -> list[Check]:
    return [
        check_shell(settings),
        check_python(settings),
        check_pip(settings),
        check_data_dir(settings),
        check_long_paths(settings),
    ]


def doctor_checks(settings: Settings) -> list[Check]:
    return [
        *startup_checks(settings),
        check_execution_policy(settings),
        check_port(settings.server_port),
        asyncio.run(check_subprocess()),
    ]
