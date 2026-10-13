import { createContext, use, type ReactNode } from "react";
import { useStore } from "zustand";
import type { RunStore, RunStoreActions, RunStoreState } from "./runStore";

// 채팅 패널마다(빌더 미리보기, 세션 상세, 개발용 픽스처 화면) 스토어를 따로 둔다.
const RunStoreContext = createContext<RunStore | null>(null);

export function RunStoreProvider({ store, children }: { store: RunStore; children: ReactNode }) {
  return <RunStoreContext value={store}>{children}</RunStoreContext>;
}

function useRunStoreApi(): RunStore {
  const store = use(RunStoreContext);
  if (!store) throw new Error("RunStoreProvider 안에서만 쓸 수 있다");
  return store;
}

export function useRunStore<T>(selector: (state: RunStoreState) => T): T {
  return useStore(useRunStoreApi(), selector);
}

export function useRunActions(): RunStoreActions {
  return useRunStore((state) => state.actions);
}
