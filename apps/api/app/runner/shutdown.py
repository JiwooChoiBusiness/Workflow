"""종료 코디네이터(2단계 설계 3.6).

레인(Supervisor)이 둘이라 종료 훅을 어느 한 레인에 걸면 먼저 끝난 레인이 이벤트 버스와 DB를 닫아
다른 레인의 failed(shutdown) 기록이 실패한다. 그래서 훅은 여기서 한 번만 돈다. 두 레인의 종료를
같은 유예로 함께 시작해 모두 끝나기를 기다린 뒤, 등록한 순서대로 훅을 돌리고 finished를 켠다.
"""

import asyncio
import threading
from collections.abc import Sequence

import structlog

from app.runner.supervisor import ShutdownHook, Supervisor

log = structlog.get_logger("app.runner")


def _hook_name(hook: ShutdownHook) -> str:
    return getattr(hook, "__qualname__", None) or repr(hook)


class ShutdownCoordinator:
    def __init__(self, lanes: Sequence[Supervisor]) -> None:
        self._lanes = tuple(lanes)
        self._hooks: list[ShutdownHook] = []
        self._task: asyncio.Task[None] | None = None
        # 콘솔 창 닫기 핸들러가 다른 스레드에서 기다리므로 threading 이벤트다(winconsole.py).
        self.finished = threading.Event()

    @property
    def lanes(self) -> tuple[Supervisor, ...]:
        return self._lanes

    @property
    def shutting_down(self) -> bool:
        return self._task is not None

    @property
    def running(self) -> bool:
        return not self.shutting_down and all(lane.running for lane in self._lanes)

    def add_hook(self, hook: ShutdownHook) -> None:
        self._hooks.append(hook)

    def request_shutdown(self, grace_sec: float | None = None) -> None:
        # uvicorn을 거치지 않는 종료(콘솔 창 닫기)와 종료 신호 처리기가 loop.call_soon_threadsafe로
        # 부른다.
        self._begin(grace_sec)

    async def shutdown(self) -> None:
        # 콘솔 핸들러가 먼저 시작한 종료가 있으면 그것을 같이 기다린다.
        await asyncio.shield(self._begin(None))

    def _begin(self, grace_sec: float | None) -> asyncio.Task[None]:
        if self._task is None:
            # 레인마다 같은 유예로 종료를 바로 시작한다. 이 줄이 돈 뒤로는 어느 레인도 새 런을 받지
            # 않는다.
            for lane in self._lanes:
                lane.request_shutdown(grace_sec)
            self._task = asyncio.get_running_loop().create_task(
                self._shutdown(), name="shutdown-coordinator"
            )
        return self._task

    async def _shutdown(self) -> None:
        log.info("shutdown.started", lanes=[lane.name for lane in self._lanes])
        await asyncio.gather(*(lane.shutdown() for lane in self._lanes), return_exceptions=True)
        log.info("shutdown.lanes_finished", lanes=[lane.name for lane in self._lanes])
        for hook in self._hooks:
            name = _hook_name(hook)
            log.info("shutdown.hook", hook=name)
            try:
                await hook()
            except Exception:
                log.exception("shutdown.hook_failed", hook=name)
        log.info("shutdown.finished")
        self.finished.set()
