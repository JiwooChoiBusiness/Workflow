import ctypes
from typing import Any, cast

import pytest

from app.sandbox import jobobject_win as jw
from app.sandbox.process import ProcessLimits

# 구조체 배치는 x64 Windows 기준이다. 모든 필드를 크기 고정 타입으로 정의했으므로 64비트 Linux에서도
# 같은 크기가 나와야 한다(3.7: sizeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144).
pytestmark = pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="x64 배치 기준")


def offset(struct: type[ctypes.Structure], field: str) -> int:
    return getattr(struct, field).offset


def test_struct_sizes() -> None:
    assert ctypes.sizeof(jw.JOBOBJECT_BASIC_LIMIT_INFORMATION) == 64
    assert ctypes.sizeof(jw.IO_COUNTERS) == 48
    assert ctypes.sizeof(jw.JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144
    assert ctypes.sizeof(jw.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION) == 48


def test_field_offsets() -> None:
    basic = jw.JOBOBJECT_BASIC_LIMIT_INFORMATION
    assert offset(basic, "LimitFlags") == 16
    assert offset(basic, "MinimumWorkingSetSize") == 24
    assert offset(basic, "ActiveProcessLimit") == 40
    assert offset(basic, "Affinity") == 48
    assert offset(basic, "SchedulingClass") == 60

    extended = jw.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    assert offset(extended, "IoInfo") == 64
    assert offset(extended, "ProcessMemoryLimit") == 112
    assert offset(extended, "JobMemoryLimit") == 120
    assert offset(extended, "PeakJobMemoryUsed") == 136

    accounting = jw.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
    assert offset(accounting, "TotalProcesses") == 36
    assert offset(accounting, "ActiveProcesses") == 40


def test_flag_values() -> None:
    assert jw.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x2000
    assert jw.JOB_OBJECT_LIMIT_JOB_MEMORY == 0x200
    assert jw.JOB_OBJECT_LIMIT_ACTIVE_PROCESS == 0x8
    assert jw.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION == 0x400
    assert jw.SANDBOX_LIMIT_FLAGS == 0x2608
    assert jw.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION == 9
    assert jw.JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION == 1
    assert jw.CREATE_NO_WINDOW | jw.CREATE_NEW_PROCESS_GROUP == 0x08000200


def test_limit_information_carries_memory_and_process_caps() -> None:
    info = jw.build_limit_information(ProcessLimits(memory_bytes=1024 * 2**20, max_processes=64))

    assert info.BasicLimitInformation.LimitFlags == jw.SANDBOX_LIMIT_FLAGS
    assert info.BasicLimitInformation.ActiveProcessLimit == 64
    assert info.JobMemoryLimit == 1024 * 2**20
    assert info.ProcessMemoryLimit == 0


class FakeFunction:
    def __init__(self, result: object, calls: list[tuple[str, tuple[object, ...]]], name: str):
        self.result = result
        self.calls = calls
        self.name = name
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> object:
        self.calls.append((self.name, args))
        return self.result


class FakeKernel32:
    """실제 kernel32 대신 반환값과 호출 순서만 흉내 낸다."""

    def __init__(self, **results: object) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        defaults: dict[str, object] = {
            "CreateJobObjectW": 100,
            "SetInformationJobObject": 1,
            "QueryInformationJobObject": 1,
            "AssignProcessToJobObject": 1,
            "TerminateJobObject": 1,
            "OpenProcess": 200,
            "CloseHandle": 1,
        }
        defaults.update(results)
        for name, result in defaults.items():
            setattr(self, name, FakeFunction(result, self.calls, name))


def kernel32(fake: FakeKernel32, last_error: int = 5) -> jw.Kernel32:
    return jw.Kernel32(cast(Any, fake), lambda: last_error)


def test_create_job_failure_reports_winerror() -> None:
    api = kernel32(FakeKernel32(CreateJobObjectW=None), last_error=8)

    with pytest.raises(OSError) as info:
        api.create_job()
    assert "winerror 8" in str(info.value)


def test_assign_closes_process_handle_even_on_failure() -> None:
    fake = FakeKernel32(AssignProcessToJobObject=0)
    api = kernel32(fake)

    with pytest.raises(OSError):
        api.assign_pid(100, 4242)
    names = [name for name, _ in fake.calls]
    assert names == ["OpenProcess", "AssignProcessToJobObject", "CloseHandle"]
    assert fake.calls[0][1][0] == jw.PROCESS_SET_QUOTA | jw.PROCESS_TERMINATE
    assert fake.calls[-1][1] == (200,)


def test_job_object_closes_once_and_stops_reporting() -> None:
    fake = FakeKernel32()
    job = jw.JobObject.create(kernel32(fake), ProcessLimits(memory_bytes=2**20, max_processes=4))

    job.close()
    job.close()
    job.terminate()

    assert [name for name, _ in fake.calls].count("CloseHandle") == 1
    assert "TerminateJobObject" not in [name for name, _ in fake.calls]
    assert job.closed
    assert job.stats().peak_memory_bytes is None


def test_failed_limit_setup_closes_the_job() -> None:
    fake = FakeKernel32(SetInformationJobObject=0)

    with pytest.raises(OSError):
        jw.JobObject.create(kernel32(fake), ProcessLimits(memory_bytes=2**20, max_processes=4))
    assert [name for name, _ in fake.calls] == [
        "CreateJobObjectW",
        "SetInformationJobObject",
        "CloseHandle",
    ]
