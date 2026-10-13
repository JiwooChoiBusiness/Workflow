import { useSyncExternalStore } from "react";

// 렌더 중에 Date.now()를 부르지 않도록 시계를 밖에 두고 구독한다. 구독자가 없으면 타이머도 멈춘다.
export interface Clock {
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => number;
}

export function createClock(intervalMs: number): Clock {
  let now = Date.now();
  let timer: ReturnType<typeof setInterval> | null = null;
  const listeners = new Set<() => void>();
  return {
    subscribe(listener) {
      listeners.add(listener);
      if (!timer) {
        now = Date.now();
        timer = setInterval(() => {
          now = Date.now();
          listeners.forEach((notify) => notify());
        }, intervalMs);
      }
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0 && timer) {
          clearInterval(timer);
          timer = null;
        }
      };
    },
    getSnapshot: () => now,
  };
}

export const secondClock = createClock(1000);
export const halfMinuteClock = createClock(30_000);

export function useNow(clock: Clock = halfMinuteClock): number {
  return useSyncExternalStore(clock.subscribe, clock.getSnapshot);
}
