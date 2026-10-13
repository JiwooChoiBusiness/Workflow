import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import type { InteractionInput } from "@/api/run-events";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Spinner } from "@/components/ui/spinner";
import { sessionFileUrl } from "@/features/sessions/api";
import type { StreamStatus } from "@/lib/sse";
import { cn } from "@/lib/cn";
import { isRunActive, type PendingInteraction } from "@/stores/runStore";
import { useRunStore } from "@/stores/runStoreContext";
import { ApprovalPrompt } from "./ApprovalPrompt";
import { Composer, type SendResult } from "./Composer";
import { InteractionPrompt } from "./InteractionPrompt";
import { ConnectionNotice, MessageList } from "./MessageList";
import { RunStatusBar } from "./RunStatusBar";
import { FileChangeList, SessionAutoApproveChip } from "./SessionBits";

export interface ChatPanelProps {
  // 세션 파일 다운로드 주소를 만들 때 쓴다.
  sessionId?: string | null;
  // 세션 파일 내려받기 주소. 기본은 콘솔 API(웹앱은 /api/web 경로를 넘긴다).
  fileUrl?: (sessionId: string, sessionFileId: string) => string;
  onSend: (text: string, files: File[]) => SendResult | Promise<SendResult>;
  onStop: () => void;
  // POST /runs/{rid}/input 본문 그대로(설계 6.2). false면 서버가 받지 않았다.
  onRespond: (runId: string, input: InteractionInput) => SendResult | Promise<SendResult>;
  onDisableAutoApprove?: () => Promise<void> | void;
  connection?: { status: StreamStatus; attempt?: number } | null;
  // 세션 기록을 불러오는 중이거나 불러오지 못했다.
  loading?: boolean;
  loadError?: unknown;
  onRetryLoad?: () => void;
  sending?: boolean;
  stopping?: boolean;
  disabled?: boolean;
  attachments?: boolean;
  className?: string;
}

export function ChatPanel({
  sessionId = null,
  fileUrl = sessionFileUrl,
  onSend,
  onStop,
  onRespond,
  onDisableAutoApprove,
  connection,
  loading = false,
  loadError = null,
  onRetryLoad,
  sending = false,
  stopping = false,
  disabled = false,
  attachments = false,
  className,
}: ChatPanelProps) {
  const { t } = useTranslation("chat");
  const status = useRunStore((state) => state.status);
  const pending = useRunStore((state) => state.pendingInteraction);
  const allowFreeText = useRunStore((state) => {
    const interaction = state.pendingInteraction;
    if (interaction?.kind !== "question") return true;
    if (typeof interaction.allow_free_text === "boolean") return interaction.allow_free_text;
    // 이벤트에 값이 없으면 ask_user 도구 입력을 본다(기본 true, 설계 4.4).
    if (!interaction.tool_call_id) return true;
    return state.toolCalls[interaction.tool_call_id]?.input?.allow_free_text !== false;
  });
  const [busyId, setBusyId] = useState<string | null>(null);
  const [disabling, setDisabling] = useState(false);
  const fileHref = useCallback(
    (sessionFileId: string) => (sessionId ? fileUrl(sessionId, sessionFileId) : null),
    [sessionId, fileUrl],
  );

  async function respond(
    interaction: PendingInteraction,
    input: InteractionInput,
  ): Promise<SendResult> {
    setBusyId(interaction.interaction_id);
    try {
      return await onRespond(interaction.runId, input);
    } finally {
      setBusyId(null);
    }
  }

  function renderInteraction(interaction: PendingInteraction) {
    const busy = busyId === interaction.interaction_id;
    if (interaction.kind === "approval") {
      return (
        <ApprovalPrompt
          interaction={interaction}
          busy={busy}
          onApprove={({ sessionAutoApprove }) =>
            void respond(interaction, {
              decision: "approve",
              session_auto_approve: sessionAutoApprove,
            })
          }
          onReject={(note) => void respond(interaction, { decision: "reject", note: note || null })}
        />
      );
    }
    return (
      <InteractionPrompt
        interaction={interaction}
        allowFreeText={allowFreeText}
        busy={busy}
        onAnswer={(answer) => void respond(interaction, { answer })}
      />
    );
  }

  const answerMode = pending?.kind === "question";
  return (
    <div className={cn("relative flex h-full min-h-0 flex-col bg-canvas", className)}>
      <div className="flex min-h-10 items-center gap-2 border-b border-line bg-surface px-4 py-1.5">
        <RunStatusBar />
        <div className="ml-auto">
          <SessionAutoApproveChip
            pending={disabling}
            onDisable={
              onDisableAutoApprove
                ? () => {
                    setDisabling(true);
                    void Promise.resolve(onDisableAutoApprove()).finally(() => setDisabling(false));
                  }
                : undefined
            }
          />
        </div>
      </div>
      {connection ? (
        <ConnectionNotice status={connection.status} attempt={connection.attempt} />
      ) : null}
      {loadError ? (
        <ErrorAlert
          error={loadError}
          title={t("session.loadFailed")}
          onRetry={onRetryLoad}
          className="mx-4 mt-3"
        />
      ) : null}
      {loading ? (
        <div className="flex flex-1 items-center justify-center gap-2 text-sm text-ink-muted">
          <Spinner className="size-4" />
          {t("session.loading")}
        </div>
      ) : (
        <MessageList renderInteraction={renderInteraction} fileHref={fileHref} />
      )}
      <FileChangeList fileHref={fileHref} />
      <Composer
        running={isRunActive(status)}
        stoppable={status === "waiting_input"}
        stopping={stopping}
        disabled={
          disabled ||
          loading ||
          sending ||
          pending?.kind === "approval" ||
          (answerMode && !allowFreeText) ||
          busyId !== null
        }
        answerMode={answerMode}
        attachments={attachments}
        onStop={onStop}
        onSend={(text, files) =>
          answerMode && pending ? respond(pending, { answer: text }) : onSend(text, files)
        }
      />
    </div>
  );
}
