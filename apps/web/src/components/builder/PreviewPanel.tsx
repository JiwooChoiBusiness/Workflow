import { RotateCcw, X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useStore } from "zustand";
import type { ApprovalMode } from "@/api/types";
import { ChatPanel } from "@/components/chat/ChatPanel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { createPreviewSession } from "@/features/sessions/api";
import { loadPreviewSessionId, savePreviewSessionId } from "@/features/sessions/previewSession";
import { useChatSession } from "@/features/sessions/useChatSession";
import { isRunOpen } from "@/stores/runStore";
import { RunStoreProvider } from "@/stores/runStoreContext";

// 빌더 미리보기(설계 7.3). 저장된 초안으로 실행하는 미리보기 세션이고, 세션은 첫 메시지를 보낼 때 만든다.
export function PreviewPanel({
  agentId,
  approvalMode,
  dirty = false,
  onClose,
}: {
  agentId: string;
  // 저장된 초안의 승인 모드. 미리보기 런은 이 값으로 돈다.
  approvalMode: ApprovalMode;
  dirty?: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation("agents");
  const [initialSessionId] = useState(() => loadPreviewSessionId(agentId));
  const chat = useChatSession({
    initialSessionId,
    createSession: async () => (await createPreviewSession(agentId)).id,
    onSessionChange: (sessionId) => savePreviewSessionId(agentId, sessionId),
  });
  const { controller } = chat;
  const runOpen = useStore(controller.store, (state) => isRunOpen(state.status));
  const [confirmReset, setConfirmReset] = useState(false);
  const [resetting, setResetting] = useState(false);

  async function reset() {
    setResetting(true);
    try {
      await controller.reset();
    } finally {
      setResetting(false);
      setConfirmReset(false);
    }
  }

  return (
    <aside
      aria-label={t("preview.title")}
      className="flex w-[26rem] shrink-0 flex-col border-l border-line bg-surface"
    >
      <header className="flex items-center gap-2 border-b border-line px-4 py-2.5">
        <h2 className="text-sm font-semibold text-ink">{t("preview.title")}</h2>
        <Badge tone="info">{t("preview.draftBadge")}</Badge>
        <Badge tone={approvalMode === "off" ? "danger" : "neutral"}>
          {t(`approval.chip.${approvalMode}`)}
        </Badge>
        <div className="ml-auto flex items-center">
          <Button
            variant="ghost"
            size="iconSm"
            aria-label={t("preview.resetSession")}
            title={t("preview.resetSession")}
            disabled={(!chat.sessionId && !runOpen) || resetting}
            onClick={() => {
              if (runOpen) setConfirmReset(true);
              else void reset();
            }}
          >
            <RotateCcw className="size-4" aria-hidden />
          </Button>
          <Button
            variant="ghost"
            size="iconSm"
            aria-label={t("builder.previewClose")}
            onClick={onClose}
          >
            <X className="size-4" aria-hidden />
          </Button>
        </div>
      </header>
      {dirty ? (
        <p className="border-b border-line bg-warn-soft px-4 py-1.5 text-xs text-ink-muted">
          {t("preview.dirtyHint")}
        </p>
      ) : null}
      <RunStoreProvider store={chat.store}>
        <ChatPanel
          className="min-h-0 flex-1"
          sessionId={chat.sessionId}
          attachments
          loading={chat.loading}
          loadError={chat.loadError}
          onRetryLoad={() => void controller.resync()}
          connection={chat.connection}
          sending={chat.sending}
          stopping={chat.stopping}
          onSend={(text, files) => controller.send(text, files)}
          onStop={() => void controller.stop()}
          onRespond={(runId, input) => controller.respond(runId, input)}
          onDisableAutoApprove={() => controller.setAutoApprove(false)}
        />
      </RunStoreProvider>
      <ConfirmDialog
        open={confirmReset}
        onOpenChange={setConfirmReset}
        title={t("preview.resetTitle")}
        description={t("preview.resetBody")}
        confirmLabel={t("preview.resetConfirm")}
        pending={resetting}
        onConfirm={() => void reset()}
      />
    </aside>
  );
}
