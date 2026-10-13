import type { RunEvent } from "@/api/run-events";

export interface PlayerSnapshot {
  done: number;
  total: number;
}

// 녹음된 이벤트를 원래 시각 간격(최대 1.2초)대로 하나씩 흘려 보낸다. speed가 0이면 한 번에 적용한다.
export class FixturePlayer {
  private speed = 1;
  private queue: RunEvent[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;
  private lastTs: number | null = null;
  private snapshot: PlayerSnapshot = { done: 0, total: 0 };
  private readonly listeners = new Set<() => void>();
  private readonly apply: (event: RunEvent) => void;

  constructor(apply: (event: RunEvent) => void) {
    this.apply = apply;
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = () => this.snapshot;

  setSpeed(speed: number) {
    this.speed = speed;
  }

  enqueue(events: RunEvent[]) {
    this.queue.push(...events);
    this.update({ total: this.snapshot.total + events.length });
    if (!this.timer) this.pump();
  }

  clear() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.queue = [];
    this.lastTs = null;
    this.update({ done: 0, total: 0 });
  }

  private update(patch: Partial<PlayerSnapshot>) {
    this.snapshot = { ...this.snapshot, ...patch };
    this.listeners.forEach((listener) => listener());
  }

  private step() {
    const event = this.queue.shift();
    if (!event) return;
    if (event.ts) this.lastTs = Date.parse(event.ts);
    this.apply(event);
    this.update({ done: this.snapshot.done + 1 });
  }

  private pump() {
    this.timer = null;
    if (this.speed === 0) {
      while (this.queue.length > 0) this.step();
      return;
    }
    const next = this.queue[0];
    if (!next) return;
    const ts = next.ts ? Date.parse(next.ts) : null;
    const gap = ts !== null && this.lastTs !== null ? ts - this.lastTs : 0;
    const delay = Math.min(Math.max(gap, 0), 1200) / this.speed;
    this.timer = setTimeout(() => {
      this.step();
      this.pump();
    }, delay);
  }
}
