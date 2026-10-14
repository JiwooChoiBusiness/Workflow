import { createContext, use, type ReactNode } from "react";
import { useStore } from "zustand";
import type { WfRunController, WfRunMeta } from "./wfRunController";
import { createWfRunStore, type WfRunStoreState } from "./wfRunStore";

// 실행 패널·캔버스 카드·로그 화면이 같은 런 컨트롤러를 본다. 편집기 밖(목록 등)에서 카드가 그려져도
// 깨지지 않게 카드는 useOptionalWfRun으로 읽는다.
const WfRunContext = createContext<WfRunController | null>(null);

const EMPTY = createWfRunStore();

export function WfRunProvider({
  controller,
  children,
}: {
  controller: WfRunController;
  children: ReactNode;
}) {
  return <WfRunContext value={controller}>{children}</WfRunContext>;
}

export function useWfRunController(): WfRunController {
  const controller = use(WfRunContext);
  if (!controller) throw new Error("WfRunProvider 안에서만 쓸 수 있다");
  return controller;
}

export function useWfRun<T>(selector: (state: WfRunStoreState) => T): T {
  return useStore(useWfRunController().store, selector);
}

export function useWfRunMeta<T>(selector: (meta: WfRunMeta) => T): T {
  return useStore(useWfRunController().meta, selector);
}

export function useOptionalWfRun<T>(selector: (state: WfRunStoreState) => T): T {
  const controller = use(WfRunContext);
  return useStore(controller?.store ?? EMPTY, selector);
}

// 캔버스 카드에서 실행 패널로 보내는 요청. 편집기가 넣고 카드가 부른다.
export interface FlowRunUi {
  // "이 노드만 실행" 대화상자를 연다.
  requestSingleRun: (nodeId: string) => void;
  // 실행 패널을 열고 그 노드 런 카드를 펼친다.
  focusNodeRun: (nodeRunId: string) => void;
}

const FlowRunUiContext = createContext<FlowRunUi | null>(null);

export function FlowRunUiProvider({ ui, children }: { ui: FlowRunUi; children: ReactNode }) {
  return <FlowRunUiContext value={ui}>{children}</FlowRunUiContext>;
}

export function useFlowRunUi(): FlowRunUi | null {
  return use(FlowRunUiContext);
}
