"""POSIX 폴백을 가짜 OS 함수로 시험한다.

실제 setsid·rlimit 실행은 Linux CI에서 test_local_backend가 본다.
"""

from pathlib import Path

import pytest

from app.sandbox import proc_posix
from app.sandbox.proc_posix import PosixOps, count_user_tasks, kill_group, make_preexec, nproc_limit

RLIMIT_AS = 9
RLIMIT_NPROC = 6
INFINITY = -1
SIGTERM = 15
SIGKILL = 9


class FakeOs:
    def __init__(self, *, alive_polls: int = 0, hard_limit: int = INFINITY, gone: bool = False):
        self.signals: list[tuple[int, int]] = []
        self.limits: list[tuple[int, tuple[int, int]]] = []
        self.alive_polls = alive_polls
        self.hard_limit = hard_limit
        self.gone = gone

    def killpg(self, pgid: int, sig: int) -> None:
        self.signals.append((pgid, sig))
        if self.gone:
            raise ProcessLookupError
        if sig == 0:
            if self.alive_polls <= 0:
                raise ProcessLookupError
            self.alive_polls -= 1

    def getrlimit(self, resource: int) -> tuple[int, int]:
        return (INFINITY, self.hard_limit)

    def setrlimit(self, resource: int, limits: tuple[int, int]) -> None:
        self.limits.append((resource, limits))

    def ops(self) -> PosixOps:
        return PosixOps(
            killpg=self.killpg,
            getuid=lambda: 1000,
            getrlimit=self.getrlimit,
            setrlimit=self.setrlimit,
            rlimit_as=RLIMIT_AS,
            rlimit_nproc=RLIMIT_NPROC,
            rlim_infinity=INFINITY,
            sigterm=SIGTERM,
            sigkill=SIGKILL,
        )


def test_preexec_sets_memory_and_process_limits() -> None:
    fake = FakeOs()

    make_preexec(fake.ops(), 1024 * 2**20, 164)()

    assert fake.limits == [
        (RLIMIT_AS, (1024 * 2**20, 1024 * 2**20)),
        (RLIMIT_NPROC, (164, 164)),
    ]


def test_preexec_never_raises_above_the_hard_limit() -> None:
    fake = FakeOs(hard_limit=100)

    make_preexec(fake.ops(), 1024, 500)()

    assert fake.limits == [(RLIMIT_AS, (100, 100)), (RLIMIT_NPROC, (100, 100))]


def test_preexec_skips_nproc_when_unknown() -> None:
    fake = FakeOs()

    make_preexec(fake.ops(), 2048, None)()

    assert fake.limits == [(RLIMIT_AS, (2048, 2048))]


def test_nproc_limit_is_added_on_top_of_current_usage() -> None:
    # RLIMIT_NPROC는 사용자 전체 태스크 수라 그대로 64를 걸면 CI 러너에서 fork가 막힌다.
    assert nproc_limit(64, 120) == 184
    assert nproc_limit(64, None) is None


async def test_kill_group_stops_after_sigterm_when_group_is_gone() -> None:
    fake = FakeOs(alive_polls=2)

    await kill_group(fake.ops(), 4321, grace_sec=1.0)

    assert fake.signals == [(4321, SIGTERM), (4321, 0), (4321, 0), (4321, 0)]


async def test_kill_group_escalates_to_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc_posix, "_POLL_SEC", 0.01)
    fake = FakeOs(alive_polls=10_000)

    await kill_group(fake.ops(), 4321, grace_sec=0.05)

    assert fake.signals[0] == (4321, SIGTERM)
    assert fake.signals[-1] == (4321, SIGKILL)


async def test_kill_group_returns_when_already_gone() -> None:
    fake = FakeOs(gone=True)

    await kill_group(fake.ops(), 4321)

    assert fake.signals == [(4321, SIGTERM)]


def test_count_user_tasks_reads_proc_status(tmp_path: Path) -> None:
    def status(pid: str, uid: int, threads: int) -> None:
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "status").write_text(
            f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nThreads:\t{threads}\n", encoding="ascii"
        )

    status("1", 0, 1)
    status("200", 1000, 3)
    status("201", 1000, 20)
    (tmp_path / "self").mkdir()
    (tmp_path / "300").mkdir()

    assert count_user_tasks(1000, tmp_path) == 23
    assert count_user_tasks(1000, tmp_path / "missing") is None
