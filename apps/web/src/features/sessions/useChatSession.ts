import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useStore } from "zustand";
import { toast } from "@/components/ui/toast";
import { workspaceKeys } from "@/features/workspaces/api";
import { errorMessage } from "@/lib/errors";
import { consoleSessionApi, sessionKeys } from "./api";
import { ChatSessionController, type ChatSessionApi } from "./chatSession";

export interface UseChatSessionOptions {
  // 처음 열 세션. 없으면 첫 메시지를 보낼 때 createSession으로 만든다.
  initialSessionId: string | null;
  createSession?: () => Promise<string>;
  onSessionChange?: (sessionId: string | null) => void;
  // 런이 끝나 세션이 바뀌었다. 화면이 따로 들고 있는 목록(웹앱 대화 목록 등)을 다시 읽게 한다.
  onSettled?: (sessionId: string) => void;
  api?: ChatSessionApi;
}

export function useChatSession(options: UseChatSessionOptions) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [controller] = useState(() => new ChatSessionController(options.api ?? consoleSessionApi));
  const [initialSessionId] = useState(options.initialSessionId);
  const { createSession, onSessionChange, onSettled } = options;

  // 아래 세션 열기보다 먼저 돈다(효과는 선언 순서대로 실행된다).
  useEffect(() => {
    controller.configure({
      createSession,
      onSessionChange,
      onSettled: (sessionId) => {
        // 메시지·파일·작업공간이 바뀌었다. 같은 세션을 보여 주는 다른 화면이 다시 읽게 한다.
        void queryClient.invalidateQueries({ queryKey: sessionKeys.detail(sessionId) });
        void queryClient.invalidateQueries({ queryKey: workspaceKeys.session(sessionId) });
        void queryClient.invalidateQueries({ queryKey: workspaceKeys.list, exact: true });
        onSettled?.(sessionId);
      },
      onError: (error) => toast.error(errorMessage(error, t)),
    });
  });

  useEffect(() => {
    // StrictMode가 효과를 두 번 돌려도 같은 세션을 다시 연다.
    void controller.open(controller.sessionId ?? initialSessionId);
    return () => controller.dispose();
  }, [controller, initialSessionId]);

  const meta = useStore(controller.meta);
  return { controller, store: controller.store, ...meta };
}
