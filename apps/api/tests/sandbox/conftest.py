"""샌드박스 테스트 공용 픽스처.

--import-mode=importlib에서는 테스트 모듈이 conftest를 import할 수 없어 도우미도 픽스처로 넘긴다.
"""

import asyncio
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.config import Settings
from app.sandbox.base import ExecExited, ExecOutput, ExecSpec, OutputDecoder, WorkspaceHandle
from app.sandbox.local_workspace import LocalWorkspaceBackend

WINDOWS = sys.platform == "win32"

Collected = tuple[str, str, ExecExited]


def _settings(data_dir: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": data_dir,
        "sandbox_shell": "powershell" if WINDOWS else "bash",
    }
    values.update(overrides)
    return Settings.model_validate(values)


async def _collect(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle, spec: ExecSpec
) -> Collected:
    decoders = {"stdout": OutputDecoder(), "stderr": OutputDecoder()}
    text = {"stdout": "", "stderr": ""}
    exited: ExecExited | None = None
    async for event in backend.exec(handle, spec):
        if isinstance(event, ExecOutput):
            text[event.stream] += decoders[event.stream].feed(event.data)
        elif isinstance(event, ExecExited):
            exited = event
    assert exited is not None
    return (
        text["stdout"] + decoders["stdout"].flush(),
        text["stderr"] + decoders["stderr"].flush(),
        exited,
    )


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        # PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        process = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not process:
            return False
        try:
            # WAIT_TIMEOUT(0x102)이면 아직 살아 있다.
            return kernel32.WaitForSingleObject(process, 0) == 0x102
        finally:
            kernel32.CloseHandle(process)
    # Windows의 os.kill은 신호와 상관없이 프로세스를 죽이므로 POSIX에서만 쓴다.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_gone(pids: list[int], timeout: float = 5.0) -> list[int]:
    deadline = time.monotonic() + timeout
    alive = [pid for pid in pids if _pid_alive(pid)]
    while alive and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        alive = [pid for pid in alive if _pid_alive(pid)]
    return alive


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    return _settings


@pytest.fixture
def collect_exec() -> Callable[
    [LocalWorkspaceBackend, WorkspaceHandle, ExecSpec], Awaitable[Collected]
]:
    return _collect


@pytest.fixture
def pid_alive() -> Callable[[int], bool]:
    return _pid_alive


@pytest.fixture
def wait_gone() -> Callable[..., Awaitable[list[int]]]:
    return _wait_gone


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path.resolve() / "data"


@pytest.fixture
async def backend(data_dir: Path) -> AsyncIterator[LocalWorkspaceBackend]:
    instance = LocalWorkspaceBackend(_settings(data_dir))
    yield instance
    await instance.aclose()


@pytest.fixture
async def handle(backend: LocalWorkspaceBackend) -> WorkspaceHandle:
    return await backend.acquire(uuid4())


@pytest.fixture
def run_exec(
    backend: LocalWorkspaceBackend, handle: WorkspaceHandle
) -> Callable[..., Awaitable[Collected]]:
    async def run(script: str = "", *, kind: str = "shell", **fields: Any) -> Collected:
        spec = ExecSpec.model_validate(
            {"exec_id": f"call_{uuid4().hex[:8]}", "kind": kind, "script": script} | fields
        )
        return await _collect(backend, handle, spec)

    return run
