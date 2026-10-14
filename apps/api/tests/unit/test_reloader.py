"""serve --reload의 감시 프로세스(설계 M10.1 (3)). 서버 대신 가짜 자식으로 재시작 절차를 본다."""

import io
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from watchfiles import Change

from app.runner import reloader


class FakeChild:
    def __init__(self, name: str, *, exit_code: int | None = None) -> None:
        self.name = name
        self.exit_code = exit_code
        self.polls = 0
        self.signals: list[int] = []
        self.killed = False
        self.wait_timeouts: list[float | None] = []
        self.ignore_signal = False
        self.signal_error: OSError | None = None

    def poll(self) -> int | None:
        self.polls += 1
        return self.exit_code

    def send_signal(self, sig: int) -> None:
        if self.signal_error is not None:
            raise self.signal_error
        self.signals.append(sig)
        if not self.ignore_signal:
            self.exit_code = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.exit_code is None:
            raise subprocess.TimeoutExpired("server", timeout or 0)
        return self.exit_code

    def kill(self) -> None:
        self.killed = True
        self.exit_code = 1


class Recorder:
    def __init__(self, children: Sequence[FakeChild] = ()) -> None:
        self.prepared = list(children)
        self.started: list[FakeChild] = []
        self.stopped: list[FakeChild] = []
        self.commands: list[list[str]] = []

    def start(self, command: Sequence[str]) -> FakeChild:
        self.commands.append(list(command))
        child = self.prepared.pop(0) if self.prepared else FakeChild(f"c{len(self.started)}")
        self.started.append(child)
        return child

    def stop(self, child: Any) -> bool:
        self.stopped.append(child)
        return True


def wait_until(condition: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "시간 안에 조건이 맞지 않았다"
        time.sleep(0.05)


def test_python_file_change_restarts_the_child(tmp_path: Path) -> None:
    watched = tmp_path / "app"
    (watched / "pkg").mkdir(parents=True)
    recorder = Recorder()
    stop_event = threading.Event()
    out = io.StringIO()
    thread = threading.Thread(
        target=reloader.run,
        args=(["serve"],),
        kwargs={
            "dirs": [watched],
            "start": recorder.start,
            "stop": recorder.stop,
            "stop_event": stop_event,
            "out": out,
        },
    )
    thread.start()
    try:
        # 자식 상태를 한 번 봤다면 감시가 이미 돌고 있다.
        wait_until(lambda: bool(recorder.started) and recorder.started[0].polls >= 1)
        (watched / "notes.txt").write_text("not code", encoding="utf-8")
        (watched / "pkg" / "__pycache__").mkdir()
        (watched / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0")
        polls = recorder.started[0].polls
        # 감시가 두 번 더 깨어날 동안(디바운스보다 길다) 재시작이 없어야 한다.
        wait_until(lambda: recorder.started[0].polls >= polls + 2)
        assert len(recorder.started) == 1

        (watched / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        wait_until(lambda: len(recorder.started) == 2)
    finally:
        stop_event.set()
        thread.join(timeout=15)

    assert not thread.is_alive()
    first, second = recorder.started
    assert recorder.stopped == [first, second]
    assert recorder.commands == [["serve"], ["serve"]]
    assert "mod.py 바뀜" in out.getvalue()


def fake_watch(*batches: set[tuple[Change, str]] | None) -> Callable[..., Iterator[Any]]:
    """정해 둔 변경 묶음을 차례로 내고 Ctrl+C로 끝나는 watchfiles.watch 대역. None은 시간 초과."""

    def watch(*paths: Path, **options: Any) -> Iterator[set[tuple[Change, str]]]:
        assert options["debounce"] == reloader.DEBOUNCE_MS
        for batch in batches:
            yield batch or set()
        raise KeyboardInterrupt

    return watch


def test_ctrl_c_stops_the_child_and_exits_cleanly(tmp_path: Path) -> None:
    recorder = Recorder()

    code = reloader.run(
        ["serve"],
        dirs=[tmp_path],
        start=recorder.start,
        stop=recorder.stop,
        watch=fake_watch(),
        out=io.StringIO(),
    )

    assert code == 0
    assert recorder.stopped == recorder.started and len(recorder.started) == 1


def test_second_ctrl_c_while_stopping_kills_without_waiting(tmp_path: Path) -> None:
    slow = FakeChild("slow")
    recorder = Recorder([slow])
    out = io.StringIO()

    def interrupted(child: Any) -> bool:
        raise KeyboardInterrupt

    reloader.run(
        ["serve"],
        dirs=[tmp_path],
        start=recorder.start,
        stop=interrupted,
        watch=fake_watch(),
        out=out,
    )

    assert slow.killed
    assert "강제로 끝냈다" in out.getvalue()


def test_crashed_child_is_reported_once_and_restarted_on_the_next_change(tmp_path: Path) -> None:
    crashed = FakeChild("crashed", exit_code=1)
    recorder = Recorder([crashed])
    out = io.StringIO()
    change = {(Change.modified, str(tmp_path / "a.py")), (Change.added, str(tmp_path / "b.py"))}

    reloader.run(
        ["serve"],
        dirs=[tmp_path],
        start=recorder.start,
        stop=recorder.stop,
        watch=fake_watch(None, None, change),
        out=out,
    )

    text = out.getvalue()
    assert text.count("종료 코드 1로 끝났다") == 1
    assert "a.py, " in text and "b.py 바뀜" in text
    assert recorder.started[0] is crashed and len(recorder.started) == 2
    assert recorder.stopped == recorder.started


def test_stop_child_waits_for_the_shutdown_path() -> None:
    child = FakeChild("server")

    assert reloader.stop_child(child, timeout=3)
    assert child.signals == [reloader.STOP_SIGNAL]
    assert child.wait_timeouts == [3]
    assert not child.killed


def test_stop_child_kills_after_the_timeout() -> None:
    child = FakeChild("stuck")
    child.ignore_signal = True

    assert not reloader.stop_child(child, timeout=0.1)
    assert child.killed
    assert child.wait_timeouts == [0.1, None]


def test_stop_child_kills_when_no_console_can_carry_the_signal() -> None:
    child = FakeChild("headless")
    child.signal_error = OSError(6, "The handle is invalid")

    assert not reloader.stop_child(child)
    assert child.killed


def test_stop_child_leaves_a_finished_child_alone() -> None:
    child = FakeChild("done", exit_code=0)

    assert reloader.stop_child(child)
    assert child.signals == [] and not child.killed


def has_console() -> bool:
    if sys.platform != "win32":
        return True
    import ctypes

    processes = (ctypes.c_uint * 4)()
    return ctypes.WinDLL("kernel32").GetConsoleProcessList(processes, 4) > 0


CHILD_SCRIPT = """
import os, signal, sys, time
from pathlib import Path

marker = Path(sys.argv[1])

def finish(signum, frame):
    marker.write_text(os.environ.get("PLATFORM_RELOAD_CHILD", ""), encoding="utf-8")
    sys.exit(0)

signal.signal(getattr(signal, "SIGBREAK", signal.SIGTERM), finish)
marker.with_suffix(".ready").write_text("ready", encoding="utf-8")
while True:
    time.sleep(0.05)
"""


@pytest.mark.skipif(not has_console(), reason="Ctrl+Break는 콘솔이 있어야 보낼 수 있다")
def test_real_child_gets_the_stop_signal_and_runs_its_handler(tmp_path: Path) -> None:
    marker = tmp_path / "stopped"
    child = reloader.start_child([sys.executable, "-c", CHILD_SCRIPT, str(marker)])
    try:
        wait_until(lambda: marker.with_suffix(".ready").exists() or child.poll() is not None)
        assert child.poll() is None

        graceful = reloader.stop_child(child, timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    assert graceful
    assert marker.read_text(encoding="utf-8") == "1"
