"""런 큐와 워커(설계 2.5, 2.6).

워커는 큐에서 런 키를 꺼내 주입받은 실행 함수(app/services/run_executor.py)를 부른다. 승인·질문을
기다리는 런은 실행 함수가 waiting_input으로 끝내고 돌아오므로 그때 슬롯이 반납된다. 답이 오면 같은
런 키가 다시 들어온다. 큐는 프로세스 메모리에만 있어서 재시작하면 기동 복구(recovery.py)가 queued
런을 다시 넣는다.
"""

import asyncio
import threading
from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger("app.runner")


class SupervisorClosed(RuntimeError):
    pass


class CancelSignal:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str | None = None

    def set(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


RunFn = Callable[[str, CancelSignal], Awaitable[None]]
ShutdownHook = Callable[[], Awaitable[None]]


class Supervisor:
    def __init__(self, run_fn: RunFn, *, max_concurrent: int = 1, grace_sec: float = 20) -> None:
        self._run_fn = run_fn
        self._max_concurrent = max_concurrent
        self._grace_sec = grace_sec
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        # 대기 순서. run.queued{position}과 /healthz의 queued에 쓴다.
        self._waiting: list[str] = []
        self._cancels: dict[str, CancelSignal] = {}
        self._workers: list[asyncio.Task[None]] = []
        # 워커 번호별 실행 중인 run_id. 종료할 때 쉬는 워커와 일하는 워커를 가른다.
        self._current: list[str | None] = [None] * max_concurrent
        self._hooks: list[ShutdownHook] = []
        self._shutdown_task: asyncio.Task[None] | None = None
        # 콘솔 창 닫기 핸들러가 다른 스레드에서 기다리므로 threading 이벤트다(winconsole.py).
        self.finished = threading.Event()

    @property
    def shutting_down(self) -> bool:
        return self._shutdown_task is not None

    @property
    def running(self) -> bool:
        return bool(self._workers) and not self.shutting_down

    def stats(self) -> dict[str, int | bool]:
        return {
            "active": sum(run_id is not None for run_id in self._current),
            "queued": len(self._waiting),
            "shutting_down": self.shutting_down,
        }

    def position(self, run_id: str) -> int | None:
        """큐에서 몇 번째인지(1부터). 실행 중이거나 큐에 없으면 None."""
        try:
            return self._waiting.index(run_id) + 1
        except ValueError:
            return None

    def is_running(self, run_id: str) -> bool:
        return run_id in self._current

    def add_shutdown_hook(self, hook: ShutdownHook) -> None:
        self._hooks.append(hook)

    def start(self) -> None:
        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._work(slot), name=f"runner-worker-{slot}")
            for slot in range(self._max_concurrent)
        ]

    def enqueue(self, run_id: str) -> None:
        if self.shutting_down:
            raise SupervisorClosed("shutting_down")
        self._cancels[run_id] = CancelSignal()
        self._waiting.append(run_id)
        self._queue.put_nowait(run_id)

    def cancel(self, run_id: str, reason: str = "user") -> bool:
        signal = self._cancels.get(run_id)
        if signal is None:
            return False
        signal.set(reason)
        return True

    def request_shutdown(self, grace_sec: float | None = None) -> None:
        # uvicorn을 거치지 않는 종료(콘솔 창 닫기)에서 loop.call_soon_threadsafe로 부른다.
        self._begin_shutdown(grace_sec)

    async def shutdown(self) -> None:
        # 콘솔 핸들러가 먼저 시작한 종료가 있으면 그것을 같이 기다린다.
        await asyncio.shield(self._begin_shutdown(None))

    def _begin_shutdown(self, grace_sec: float | None) -> asyncio.Task[None]:
        if self._shutdown_task is None:
            grace = self._grace_sec if grace_sec is None else grace_sec
            self._shutdown_task = asyncio.get_running_loop().create_task(
                self._shutdown(grace), name="runner-shutdown"
            )
        return self._shutdown_task

    async def _work(self, slot: int) -> None:
        # 종료가 시작되면 지금 런만 마치고 빠진다. 빈 큐에서 기다리면 유예 시간을 다 쓰게 된다.
        while not self.shutting_down:
            run_id = await self._queue.get()
            if run_id in self._waiting:
                self._waiting.remove(run_id)
            if self.shutting_down:
                # 꺼낸 런은 DB에 queued로 남아 있고 다음 기동의 복구가 다시 넣는다.
                return
            signal = self._cancels.setdefault(run_id, CancelSignal())
            self._current[slot] = run_id
            try:
                await self._run_fn(run_id, signal)
            except Exception:
                log.exception("runner.run_crashed", run_id=run_id)
            finally:
                self._current[slot] = None
                # 실행 함수가 끝나기 직전에 같은 런이 재개되어 새 신호가 들어왔을 수 있다.
                if self._cancels.get(run_id) is signal:
                    del self._cancels[run_id]

    async def _shutdown(self, grace_sec: float) -> None:
        log.info("runner.shutdown_started", active=self.stats()["active"], grace_sec=grace_sec)
        for signal in self._cancels.values():
            signal.set("shutdown")

        busy: list[asyncio.Task[None]] = []
        for slot, worker in enumerate(self._workers):
            if self._current[slot] is None:
                worker.cancel()
            else:
                busy.append(worker)
        if busy:
            _, pending = await asyncio.wait(busy, timeout=grace_sec)
            for worker in pending:
                log.warning("runner.force_cancel", worker=worker.get_name())
                worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

        for hook in self._hooks:
            try:
                await hook()
            except Exception:
                log.exception("runner.shutdown_hook_failed", hook=getattr(hook, "__qualname__", ""))
        log.info("runner.shutdown_finished")
        self.finished.set()
