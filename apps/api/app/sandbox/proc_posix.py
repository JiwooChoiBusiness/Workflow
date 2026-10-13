"""POSIX 폴백(CI 전용). setsid로 프로세스 그룹을 만들고 rlimit으로 메모리·프로세스 수를 막는다.

os.killpg·resource 같은 POSIX 전용 함수는 process.py가 PosixOps로 묶어 넘긴다. 플랫폼 분기를
process.py 한 곳에 두고, 이 모듈을 Windows에서도 가짜 함수로 시험할 수 있게 하려는 것이다.
"""

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.sandbox.process import (
    ProcessLimits,
    ProcessStats,
    RunningProcess,
    start_process,
)

TERM_GRACE_SEC = 2.0
_POLL_SEC = 0.05


@dataclass(frozen=True)
class PosixOps:
    killpg: Callable[[int, int], None]
    getuid: Callable[[], int]
    getrlimit: Callable[[int], tuple[int, int]]
    setrlimit: Callable[[int, tuple[int, int]], None]
    rlimit_as: int
    rlimit_nproc: int
    rlim_infinity: int
    sigterm: int
    sigkill: int


def count_user_tasks(uid: int, proc_dir: Path = Path("/proc")) -> int | None:
    """uid가 실제 사용자인 태스크(스레드 포함) 수. /proc이 없으면 None."""
    if not proc_dir.is_dir():
        return None
    total = 0
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        real_uid: int | None = None
        threads = 1
        try:
            for line in status.splitlines():
                if line.startswith("Uid:"):
                    real_uid = int(line.split()[1])
                elif line.startswith("Threads:"):
                    threads = int(line.split()[1])
        except (IndexError, ValueError):
            continue
        if real_uid == uid:
            total += threads
    return total


def nproc_limit(max_processes: int, current_user_tasks: int | None) -> int | None:
    # RLIMIT_NPROC는 자손 수가 아니라 같은 사용자의 전체 태스크 수를 센다. 그대로 64를 걸면 이미
    # 64개를 넘게 쓰는 사용자(CI 러너)에서 fork가 전부 실패하므로 현재 사용량 위로 얹는다.
    if current_user_tasks is None:
        return None
    return current_user_tasks + max_processes


def make_preexec(ops: PosixOps, memory_bytes: int, nproc: int | None) -> Callable[[], None]:
    def lower(resource: int, value: int) -> None:
        _soft, hard = ops.getrlimit(resource)
        if hard != ops.rlim_infinity:
            value = min(value, hard)
        ops.setrlimit(resource, (value, value))

    def preexec() -> None:
        lower(ops.rlimit_as, memory_bytes)
        if nproc is not None:
            lower(ops.rlimit_nproc, nproc)

    return preexec


def _signal_group(ops: PosixOps, pgid: int, sig: int) -> bool:
    try:
        ops.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


async def kill_group(ops: PosixOps, pgid: int, *, grace_sec: float = TERM_GRACE_SEC) -> None:
    """SIGTERM 후 그룹이 사라지기를 기다리고, grace_sec이 지나면 SIGKILL."""
    if not _signal_group(ops, pgid, ops.sigterm):
        return
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_SEC)
        if not _signal_group(ops, pgid, 0):
            return
    _signal_group(ops, pgid, ops.sigkill)


class PosixRunner:
    def __init__(self, ops: PosixOps) -> None:
        self._ops = ops

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        stdin: bool,
        limits: ProcessLimits,
    ) -> RunningProcess:
        current = await asyncio.to_thread(count_user_tasks, self._ops.getuid())
        platform_kwargs: dict[str, Any] = {
            # setsid. 새 세션의 리더라 pgid가 pid와 같고, 플랫폼 터미널의 Ctrl+C가 전파되지 않는다.
            "start_new_session": True,
            "preexec_fn": make_preexec(
                self._ops, limits.memory_bytes, nproc_limit(limits.max_processes, current)
            ),
        }
        proc = await start_process(argv, cwd=cwd, env=env, stdin=stdin, **platform_kwargs)
        return RunningProcess(proc)

    async def kill(self, p: RunningProcess) -> None:
        await kill_group(self._ops, p.pid)

    def stats(self, p: RunningProcess) -> ProcessStats:
        return ProcessStats(None, None, None)

    def close(self, p: RunningProcess) -> None:
        # 놓을 핸들이 없다. 그룹은 kill이 이미 정리했고, setsid로 그룹을 빠져나간 자손은
        # 잡을 방법이 없어 한계로 둔다(2.6).
        pass
