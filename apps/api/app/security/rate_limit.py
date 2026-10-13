import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_sec: int = 0


class SlidingWindowLimiter:
    # 키별 최근 요청 시각을 deque로 들고 창 밖으로 나간 것을 앞에서 버린다.
    # 프로세스 메모리에만 있어 재기동하면 초기화된다(설계 6.1에서 허용).

    def __init__(
        self, limit: int, window_sec: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if limit < 1 or window_sec <= 0:
            raise ValueError("limit은 1 이상, window_sec는 0보다 커야 한다")
        self.limit = limit
        self.window_sec = window_sec
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> RateLimitDecision:
        now = self._clock()
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
        cutoff = now - self.window_sec
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            # 거절한 요청은 세지 않는다. 막힌 쪽이 계속 두드려도 풀리는 시각은 늦춰지지 않는다.
            wait = hits[0] + self.window_sec - now
            return RateLimitDecision(allowed=False, retry_after_sec=max(1, math.ceil(wait)))
        hits.append(now)
        return RateLimitDecision(allowed=True)

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)
