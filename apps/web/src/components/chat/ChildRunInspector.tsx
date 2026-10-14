import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { useStore } from "zustand";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ChatSessionController } from "@/features/sessions/chatSession";
import { errorMessage } from "@/lib/errors";
import type { PendingInteraction } from "@/stores/runStore";
import { RunStoreProvider } from "@/stores/runStoreContext";
import { ChildScopeContext, type ChildScope } from "./childScope";
import { ConnectionNotice, MessageList } from "./MessageList";

/*
 * 하위 에이전트 런의 세션을 채팅 패널과 같은 컨트롤러로 하나 더 연다(오케스트레이션 8.1). 기록 적재,
 * 이벤트 재구독, 승인 응답(respond), 중지(stop)가 컨트롤러에 그대로 있다. active가 거짓이 되면 스트림을
 * 닫고, 다시 참이 되면 세션 기록부터 다시 읽는다.
 */
export function useChildSession(
  scope: ChildScope,
  sessionId: string | null,
  active: boolean,
): ChatSessionController {
  const { t } = useTranslation();
  const [controller] = useState(() => new ChatSessionController(scope.api));

  useEffect(() => {
    controller.configure({ onError: (error) => toast.error(errorMessage(error, t)) });
  });

  useEffect(() => {
    if (!sessionId || !active) return;
    void controller.open(sessionId);
    return () => controller.dispose();
  }, [controller, sessionId, active]);

  return controller;
}

// 자식 화면 안에서 쓰는 범위. 손자 카드에서 켠 자동 승인은 이 자식의 화면과 그 위로 함께 알린다.
export function useNestedScope(scope: ChildScope, controller: ChatSessionController): ChildScope {
  return useMemo(
    () => ({
      ...scope,
      markAutoApprove: () => {
        controller.store.getState().actions.setSessionAutoApprove(true);
        scope.markAutoApprove();
      },
    }),
    [scope, controller],
  );
}

// 하위 에이전트의 대화를 부모 대화와 같은 MessageList로 그린다. 입력창은 없다. 손자 호출은 안에서 다시
// 하위 에이전트 카드가 된다.
export function ChildRunInspector({
  controller,
  scope,
  renderInteraction,
}: {
  controller: ChatSessionController;
  scope: ChildScope;
  renderInteraction: (interaction: PendingInteraction) => ReactNode;
}) {
  const { t } = useTranslation("chat");
  const meta = useStore(controller.meta);
  const sessionId = meta.sessionId;
  const fileHref = useCallback(
    (fileId: string) => (sessionId ? scope.fileUrl(sessionId, fileId) : null),
    [scope, sessionId],
  );
  const nested = useNestedScope(scope, controller);

  return (
    <div className="flex flex-col border-t border-line bg-canvas">
      {meta.connection ? (
        <ConnectionNotice status={meta.connection.status} attempt={meta.connection.attempt} />
      ) : null}
      {meta.loadError ? (
        <ErrorAlert
          error={meta.loadError}
          title={t("child.loadFailed")}
          onRetry={() => void controller.resync()}
          className="mx-3 mt-3"
        />
      ) : null}
      {meta.loading ? (
        <div className="flex items-center justify-center gap-2 py-6 text-xs text-ink-muted">
          <Spinner className="size-4" />
          {t("child.loading")}
        </div>
      ) : (
        <RunStoreProvider store={controller.store}>
          <ChildScopeContext value={nested}>
            <MessageList
              className="max-h-[32rem]"
              renderInteraction={renderInteraction}
              fileHref={fileHref}
            />
          </ChildScopeContext>
        </RunStoreProvider>
      )}
    </div>
  );
}
