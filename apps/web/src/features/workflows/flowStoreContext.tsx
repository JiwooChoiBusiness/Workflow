import { createContext, use, type ReactNode } from "react";
import { useStore } from "zustand";
import type { FlowActions, FlowState, FlowStore } from "./flowStore";

const FlowStoreContext = createContext<FlowStore | null>(null);

export function FlowStoreProvider({ store, children }: { store: FlowStore; children: ReactNode }) {
  return <FlowStoreContext value={store}>{children}</FlowStoreContext>;
}

export function useFlowStoreApi(): FlowStore {
  const store = use(FlowStoreContext);
  if (!store) throw new Error("FlowStoreProvider 안에서만 쓸 수 있다");
  return store;
}

export function useFlow<T>(selector: (state: FlowState) => T): T {
  return useStore(useFlowStoreApi(), selector);
}

export function useFlowActions(): FlowActions {
  return useFlow((state) => state.actions);
}
