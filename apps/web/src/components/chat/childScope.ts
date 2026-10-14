import { createContext, use } from "react";
import { consoleSessionApi, sessionFileUrl } from "@/features/sessions/api";
import type { ChatSessionApi } from "@/features/sessions/chatSession";

// 하위 에이전트 카드가 자식 세션을 열 때 쓰는 경로(콘솔·웹앱)와, 카드 안에서 켠 자동 승인을 위 단계 화면에
// 알리는 함수. 채팅 패널이 넣고 하위 에이전트 인스펙터가 한 단계씩 감싸 내려 준다.
export interface ChildScope {
  api: ChatSessionApi;
  fileUrl: (sessionId: string, fileId: string) => string;
  // 서버는 자식 카드에서 켠 "이 대화에서는 묻지 않기"를 조상 세션까지 쓴다(오케스트레이션 3.5).
  markAutoApprove: () => void;
}

export const ChildScopeContext = createContext<ChildScope>({
  api: consoleSessionApi,
  fileUrl: sessionFileUrl,
  markAutoApprove: () => undefined,
});

export function useChildScope(): ChildScope {
  return use(ChildScopeContext);
}
