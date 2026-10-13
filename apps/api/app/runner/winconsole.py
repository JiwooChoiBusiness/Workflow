import asyncio
import contextlib
import signal
import sys
import threading
from collections.abc import Callable
from types import FrameType
from typing import Any

import structlog

from app.runner.supervisor import Supervisor

log = structlog.get_logger("app.runner")

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
# 창 닫기·로그오프·시스템 종료는 핸들러가 돌아오거나 5초가 지나면 OS가 프로세스를 끝낸다.
# 그 안에 런 정리와 DB 체크포인트까지 마치도록 런 유예를 짧게 잡는다.
CLOSE_WAIT_SEC = 4.0
CLOSE_GRACE_SEC = 2.5


def make_handler(loop: asyncio.AbstractEventLoop, supervisor: Supervisor) -> Callable[[int], bool]:
    def handle(event: int) -> bool:
        # Ctrl+C와 Ctrl+Break는 다음 핸들러로 넘긴다. 파이썬이 SIGINT·SIGBREAK로 바꾸고
        # uvicorn이 받아 lifespan 종료를 거친다.
        if event in (CTRL_C_EVENT, CTRL_BREAK_EVENT):
            return False
        try:
            loop.call_soon_threadsafe(supervisor.request_shutdown, CLOSE_GRACE_SEC)
        except RuntimeError:
            return False
        supervisor.finished.wait(CLOSE_WAIT_SEC)
        return True

    return handle


def _noop() -> None:
    pass


def ensure_proactor_loop_policy() -> None:
    # 도구 실행은 asyncio 서브프로세스라 Windows에서는 Proactor 루프에서만 된다. 3.12의 기본
    # 정책이 이미 Proactor라 보통은 할 일이 없고, 다른 코드가 Selector 정책으로 바꿔 둔 경우를
    # 되돌린다. uvicorn --reload는 이 정책을 거치지 않으므로 reload_warning을 본다.
    if sys.platform != "win32":
        return
    if not isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsProactorEventLoopPolicy):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def reload_warning() -> str | None:
    # uvicorn --reload는 Windows에서 정책과 관계없이 SelectorEventLoop를 만든다. loop="none"으로
    # Proactor를 쓰게 하면 처음 뜬 서버는 되지만, 부모가 만든 리슨 소켓을 IOCP에 한 번밖에 붙일
    # 수 없어 코드를 고쳐 다시 뜬 서버는 연결을 받지 못한다(WinError 87). 그래서 알리기만 한다.
    if sys.platform != "win32":
        return None
    return (
        "Windows에서 --reload로 띄우면 서버가 SelectorEventLoop로 돌아 셸·파이썬 도구"
        "(서브프로세스)를 실행하지 못한다. 도구를 쓰려면 --reload 없이 띄운다."
    )


def chain_exit_signals(
    loop: asyncio.AbstractEventLoop, supervisor: Supervisor
) -> Callable[[], None]:
    """uvicorn의 종료 신호 처리기 앞에 런 종료 요청을 끼운다.

    uvicorn은 신호를 받으면 열린 HTTP 연결이 모두 닫힐 때까지 기다린 뒤에 lifespan 종료를 부른다.
    진행 중인 런의 SSE 응답은 런이 끝나야 닫히므로 그대로 두면 서로를 기다려 Ctrl+C로 꺼지지 않는다.
    신호를 받는 즉시 Supervisor 종료를 시작해 런이 run.completed로 끝나고 SSE가 닫히게 한다.
    """
    if threading.current_thread() is not threading.main_thread():
        return _noop
    names = ["SIGINT", "SIGTERM", "SIGBREAK"]
    previous: dict[signal.Signals, Any] = {}
    for name in names:
        signum = getattr(signal, name, None)
        if not isinstance(signum, signal.Signals):
            continue
        current = signal.getsignal(signum)
        if not callable(current):
            continue

        def handler(
            received: int, frame: FrameType | None, _next: Callable[..., Any] = current
        ) -> None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(supervisor.request_shutdown)
            _next(received, frame)

        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            continue
        previous[signum] = current

    def uninstall() -> None:
        for signum, original in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, original)

    return uninstall


def install(loop: asyncio.AbstractEventLoop, supervisor: Supervisor) -> Callable[[], None]:
    if sys.platform != "win32":
        return _noop
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    # OS가 다른 스레드에서 부르는 콜백이라 수거되면 안 된다. uninstall 클로저가 참조를 붙잡아 둔다.
    callback = handler_type(make_handler(loop, supervisor))
    if not kernel32.SetConsoleCtrlHandler(callback, True):
        log.warning("console_handler.install_failed", winerror=ctypes.get_last_error())
        return _noop

    def uninstall() -> None:
        kernel32.SetConsoleCtrlHandler(callback, False)

    return uninstall
