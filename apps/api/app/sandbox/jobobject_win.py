"""Windows Job Object(ctypes). process.py가 Windows에서만 지연 import한다.

kernel32 DLL은 process.py가 만들어 넘긴다. 이 모듈은 ctypes.WinDLL 같은 Windows 전용 API를
직접 부르지 않으므로 다른 OS에서도 import되고, 구조체 크기 테스트가 모든 OS에서 돈다.
"""

import asyncio
import ctypes
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.sandbox.base import SandboxError
from app.sandbox.process import (
    ProcessLimits,
    ProcessStats,
    RunningProcess,
    start_process,
)

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
SANDBOX_LIMIT_FLAGS = (
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    | JOB_OBJECT_LIMIT_JOB_MEMORY
    | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
)

JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

# wintypes.DWORD는 비Windows에서 c_ulong(8바이트)이라 구조체 배치가 달라진다. 크기 고정 타입을 쓴다.
DWORD = ctypes.c_uint32
LARGE_INTEGER = ctypes.c_int64
ULONGLONG = ctypes.c_uint64
SIZE_T = ctypes.c_size_t
ULONG_PTR = ctypes.c_size_t
HANDLE = ctypes.c_void_p
BOOL = ctypes.c_int32


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    PerProcessUserTimeLimit: int
    PerJobUserTimeLimit: int
    LimitFlags: int
    MinimumWorkingSetSize: int
    MaximumWorkingSetSize: int
    ActiveProcessLimit: int
    Affinity: int
    PriorityClass: int
    SchedulingClass: int
    _fields_ = [
        ("PerProcessUserTimeLimit", LARGE_INTEGER),
        ("PerJobUserTimeLimit", LARGE_INTEGER),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", SIZE_T),
        ("MaximumWorkingSetSize", SIZE_T),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ULONG_PTR),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ULONGLONG),
        ("WriteOperationCount", ULONGLONG),
        ("OtherOperationCount", ULONGLONG),
        ("ReadTransferCount", ULONGLONG),
        ("WriteTransferCount", ULONGLONG),
        ("OtherTransferCount", ULONGLONG),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    BasicLimitInformation: JOBOBJECT_BASIC_LIMIT_INFORMATION
    IoInfo: IO_COUNTERS
    ProcessMemoryLimit: int
    JobMemoryLimit: int
    PeakProcessMemoryUsed: int
    PeakJobMemoryUsed: int
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", SIZE_T),
        ("JobMemoryLimit", SIZE_T),
        ("PeakProcessMemoryUsed", SIZE_T),
        ("PeakJobMemoryUsed", SIZE_T),
    ]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    TotalUserTime: int
    TotalKernelTime: int
    ThisPeriodTotalUserTime: int
    ThisPeriodTotalKernelTime: int
    TotalPageFaultCount: int
    TotalProcesses: int
    ActiveProcesses: int
    TotalTerminatedProcesses: int
    _fields_ = [
        ("TotalUserTime", LARGE_INTEGER),
        ("TotalKernelTime", LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", LARGE_INTEGER),
        ("TotalPageFaultCount", DWORD),
        ("TotalProcesses", DWORD),
        ("ActiveProcesses", DWORD),
        ("TotalTerminatedProcesses", DWORD),
    ]


def build_limit_information(limits: ProcessLimits) -> JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = SANDBOX_LIMIT_FLAGS
    info.BasicLimitInformation.ActiveProcessLimit = limits.max_processes
    # Job 안 모든 프로세스의 커밋 메모리 합계 상한. 넘으면 할당이 실패할 뿐 죽이지는 않는다.
    info.JobMemoryLimit = limits.memory_bytes
    return info


def _winerror(code: int, what: str) -> OSError:
    return OSError(0, f"{what} 실패(winerror {code})", None, code)


class Kernel32:
    """Job Object에 필요한 kernel32 함수 묶음. dll은 WinDLL("kernel32", use_last_error=True)."""

    def __init__(self, dll: ctypes.CDLL, get_last_error: Callable[[], int]) -> None:
        self._dll = dll
        self._last_error = get_last_error
        dll.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        dll.CreateJobObjectW.restype = HANDLE
        dll.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD]
        dll.SetInformationJobObject.restype = BOOL
        dll.QueryInformationJobObject.argtypes = [
            HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            DWORD,
            ctypes.POINTER(DWORD),
        ]
        dll.QueryInformationJobObject.restype = BOOL
        dll.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
        dll.AssignProcessToJobObject.restype = BOOL
        dll.TerminateJobObject.argtypes = [HANDLE, ctypes.c_uint32]
        dll.TerminateJobObject.restype = BOOL
        dll.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
        dll.OpenProcess.restype = HANDLE
        dll.CloseHandle.argtypes = [HANDLE]
        dll.CloseHandle.restype = BOOL

    def create_job(self) -> int:
        handle: int | None = self._dll.CreateJobObjectW(None, None)
        if not handle:
            raise _winerror(self._last_error(), "CreateJobObjectW")
        return handle

    def set_limits(self, job: int, info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION) -> None:
        ok: int = self._dll.SetInformationJobObject(
            job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            raise _winerror(self._last_error(), "SetInformationJobObject")

    def query_limits(self, job: int) -> JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        self._query(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, info)
        return info

    def query_accounting(self, job: int) -> JOBOBJECT_BASIC_ACCOUNTING_INFORMATION:
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        self._query(job, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION, info)
        return info

    def _query(self, job: int, info_class: int, info: ctypes.Structure) -> None:
        ok: int = self._dll.QueryInformationJobObject(
            job, info_class, ctypes.byref(info), ctypes.sizeof(info), None
        )
        if not ok:
            raise _winerror(self._last_error(), "QueryInformationJobObject")

    def assign_pid(self, job: int, pid: int) -> None:
        process: int | None = self._dll.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid
        )
        if not process:
            raise _winerror(self._last_error(), "OpenProcess")
        try:
            ok: int = self._dll.AssignProcessToJobObject(job, process)
            if not ok:
                raise _winerror(self._last_error(), "AssignProcessToJobObject")
        finally:
            self._dll.CloseHandle(process)

    def terminate(self, job: int, exit_code: int = 1) -> bool:
        return bool(self._dll.TerminateJobObject(job, exit_code))

    def close(self, handle: int) -> None:
        self._dll.CloseHandle(handle)


class JobObject:
    def __init__(self, api: Kernel32, handle: int) -> None:
        self._api = api
        self._handle: int | None = handle

    @classmethod
    def create(cls, api: Kernel32, limits: ProcessLimits) -> "JobObject":
        job = cls(api, api.create_job())
        try:
            api.set_limits(job.handle, build_limit_information(limits))
        except OSError:
            job.close()
            raise
        return job

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise ValueError("이미 닫힌 Job Object")
        return self._handle

    @property
    def closed(self) -> bool:
        return self._handle is None

    def assign(self, pid: int) -> None:
        self._api.assign_pid(self.handle, pid)

    def terminate(self) -> None:
        if self._handle is not None:
            self._api.terminate(self._handle)

    def stats(self) -> ProcessStats:
        if self._handle is None:
            return ProcessStats(None, None, None)
        try:
            limits = self._api.query_limits(self._handle)
            accounting = self._api.query_accounting(self._handle)
        except OSError:
            return ProcessStats(None, None, None)
        return ProcessStats(
            peak_memory_bytes=limits.PeakJobMemoryUsed,
            active_processes=accounting.ActiveProcesses,
            total_processes=accounting.TotalProcesses,
        )

    def close(self) -> None:
        # KILL_ON_JOB_CLOSE라서 마지막 핸들이 닫히면 남은 프로세스가 모두 끝난다.
        if self._handle is not None:
            handle, self._handle = self._handle, None
            self._api.close(handle)


class JobProcess(RunningProcess):
    def __init__(self, proc: asyncio.subprocess.Process, job: JobObject) -> None:
        super().__init__(proc)
        self.job = job


def _job(p: RunningProcess) -> JobObject:
    if not isinstance(p, JobProcess):
        raise TypeError("JobObjectRunner가 띄운 프로세스가 아니다")
    return p.job


class JobObjectRunner:
    def __init__(self, api: Kernel32) -> None:
        self._api = api

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        stdin: bool,
        limits: ProcessLimits,
    ) -> RunningProcess:
        try:
            job = JobObject.create(self._api, limits)
        except OSError as exc:
            raise SandboxError("sandbox_unavailable", f"Job Object를 만들지 못했다: {exc}") from exc
        platform_kwargs: dict[str, Any] = {
            # 콘솔 창을 띄우지 않고, 플랫폼 콘솔의 Ctrl+C가 도구로 전파되지 않게 한다.
            "creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        }
        try:
            proc = await start_process(argv, cwd=cwd, env=env, stdin=stdin, **platform_kwargs)
        except BaseException:
            job.close()
            raise
        try:
            # 생성과 할당 사이의 틈은 받아들인다(3.7). 할당 뒤에 생긴 자손은 모두 Job에 들어간다.
            job.assign(proc.pid)
        except OSError as exc:
            # 격리 없이 실행하지 않는다.
            proc.kill()
            await proc.wait()
            job.close()
            raise SandboxError(
                "sandbox_unavailable", f"프로세스를 Job Object에 넣지 못했다: {exc}"
            ) from exc
        return JobProcess(proc, job)

    async def kill(self, p: RunningProcess) -> None:
        _job(p).terminate()

    def stats(self, p: RunningProcess) -> ProcessStats:
        return _job(p).stats()

    def close(self, p: RunningProcess) -> None:
        _job(p).close()
