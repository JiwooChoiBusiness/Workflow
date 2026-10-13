import asyncio
import sys
from collections.abc import Callable

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
