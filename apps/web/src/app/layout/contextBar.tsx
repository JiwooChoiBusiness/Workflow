import { createContext, use, type ReactNode } from "react";
import { createPortal } from "react-dom";

// 화면이 상단 컨텍스트 바 오른쪽에 자기 동작(저장, 미리보기 등)을 끼워 넣는 자리
export const ContextBarSlotContext = createContext<HTMLElement | null>(null);

export function ContextBarActions({ children }: { children: ReactNode }) {
  const slot = use(ContextBarSlotContext);
  return slot ? createPortal(children, slot) : null;
}

export interface RouteHandle {
  crumb?: (params: Record<string, string | undefined>) => ReactNode;
}
