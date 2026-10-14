"""serve --reload의 감시 프로세스(설계 M10.1 (3)).

uvicorn 리로더는 Windows에서 서버를 SelectorEventLoop로 띄워 셸·파이썬 도구가 돌지 않는다.
여기서는 부모가 파일만 지켜보다가 자식 `platform serve`를 평소와 같은 방식(Proactor 루프)으로
내렸다 다시 띄운다. 부모는 메모리를 아끼려고 uvicorn·FastAPI·설정을 불러오지 않는다.
"""

import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from types import FrameType
from typing import Any, Protocol, TextIO

import watchfiles

CHILD_ENV = "PLATFORM_RELOAD_CHILD"
DEBOUNCE_MS = 500
STOP_TIMEOUT_SEC = 10.0
# 변경이 없어도 이 간격으로 깨어나 자식이 혼자 죽었는지 본다.
POLL_MS = 1000

API_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = API_DIR.parent.parent
WATCH_DIRS = (API_DIR / "app", API_DIR / "migrations")
# 새 프로세스 그룹에서는 Ctrl+C가 꺼져 있어 Windows에서는 Ctrl+Break로 끝내게 한다.
# 서버(uvicorn)는 둘 다 같은 종료 절차로 받는다.
STOP_SIGNAL: int = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
_NEW_PROCESS_GROUP: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class Child(Protocol):
    def poll(self) -> int | None: ...

    def send_signal(self, sig: int) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


def is_child() -> bool:
    return os.environ.get(CHILD_ENV) == "1"


def start_child(command: Sequence[str]) -> subprocess.Popen[bytes]:
    # 자식을 새 프로세스 그룹에 두어야 재시작할 때 자식에게만 신호를 보낼 수 있다. 그 대신 콘솔의
    # Ctrl+C는 부모만 받으므로 부모가 같은 절차로 자식을 끝낸다. Windows는 process_group을 무시하고
    # creationflags로, POSIX는 process_group으로 그룹을 만든다.
    return subprocess.Popen(
        list(command),
        env={**os.environ, CHILD_ENV: "1"},
        creationflags=_NEW_PROCESS_GROUP,
        process_group=0,
    )


def stop_child(child: Child, timeout: float = STOP_TIMEOUT_SEC) -> bool:
    """자식이 종료 절차를 마치게 한다. 시간 안에 끝나지 않아 kill했으면 False."""
    if child.poll() is not None:
        return True
    try:
        child.send_signal(STOP_SIGNAL)
    except OSError:
        # 콘솔이 없는 프로세스는 Ctrl+Break를 보낼 수 없다. 기다릴 것 없이 kill로 넘어간다.
        pass
    else:
        try:
            child.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            pass
    child.kill()
    child.wait()
    return False


def _raise_interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _route_stop_signals_to_interrupt() -> Callable[[], None]:
    # Ctrl+Break(Windows)와 SIGTERM(POSIX)도 Ctrl+C처럼 자식을 정리하고 나가게 한다.
    previous: dict[int, Any] = {}
    for name in ("SIGBREAK", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.signal(signum, _raise_interrupt)
        except ValueError:
            # 메인 스레드가 아니면 처리기를 바꿀 수 없다(테스트).
            continue

    def restore() -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return restore


def _display(path: str | Path) -> str:
    absolute = Path(path)
    return str(absolute.relative_to(REPO_ROOT) if absolute.is_relative_to(REPO_ROOT) else absolute)


def _describe(changes: Iterable[tuple[Any, str]]) -> str:
    paths = sorted({_display(path) for _, path in changes})
    shown = ", ".join(paths[:3])
    return shown if len(paths) <= 3 else f"{shown} 외 {len(paths) - 3}개"


def run(
    command: Sequence[str],
    *,
    dirs: Sequence[Path] = WATCH_DIRS,
    start: Callable[[Sequence[str]], Child] = start_child,
    stop: Callable[[Child], bool] = stop_child,
    watch: Callable[..., Iterable[set[tuple[Any, str]]]] = watchfiles.watch,
    stop_event: Any = None,
    out: TextIO | None = None,
) -> int:
    def say(text: str) -> None:
        print(f"reload: {text}", file=out or sys.stderr, flush=True)

    say(f"{', '.join(_display(path) for path in dirs)}의 .py 파일을 지켜본다. 끝내려면 Ctrl+C")
    child = start(command)
    restore_signals = _route_stop_signals_to_interrupt()
    exit_reported = False
    try:
        for changes in watch(
            *dirs,
            watch_filter=watchfiles.PythonFilter(),
            debounce=DEBOUNCE_MS,
            rust_timeout=POLL_MS,
            yield_on_timeout=True,
            stop_event=stop_event,
        ):
            if not changes:
                code = child.poll()
                if code is not None and not exit_reported:
                    say(f"서버가 종료 코드 {code}로 끝났다. 파일을 고치면 다시 띄운다")
                    exit_reported = True
                continue
            say(f"{_describe(changes)} 바뀜. 서버를 다시 띄운다")
            if not stop(child):
                say(f"서버가 {STOP_TIMEOUT_SEC:.0f}초 안에 끝나지 않아 강제로 끝냈다")
            child = start(command)
            exit_reported = False
    except KeyboardInterrupt:
        pass
    finally:
        try:
            graceful = stop(child)
        except KeyboardInterrupt:
            # 정리하는 중에 Ctrl+C를 또 누르면 더 기다리지 않는다.
            graceful = child.poll() is not None
            if not graceful:
                child.kill()
                child.wait()
        restore_signals()
        if not graceful:
            say("서버를 강제로 끝냈다")
    return 0
