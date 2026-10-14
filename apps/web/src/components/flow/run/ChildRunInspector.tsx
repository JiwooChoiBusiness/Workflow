import { MessagesSquare } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useStore } from "zustand";
import { useShallow } from "zustand/react/shallow";
import type { InteractionInput } from "@/api/run-events";
import { ApprovalPrompt } from "@/components/chat/ApprovalPrompt";
import { ChildRunCard } from "@/components/chat/ChildRunCard";
import {
  ChildRunInspector as ChildConversation,
  useChildSession,
  useNestedScope,
} from "@/components/chat/ChildRunInspector";
import { ChildScopeContext, useChildScope } from "@/components/chat/childScope";
import { InteractionPrompt } from "@/components/chat/InteractionPrompt";
import { Button } from "@/components/ui/button";
import "@/features/workflows/i18n";
import type { PendingInteraction } from "@/stores/runStore";
import { RunStoreProvider } from "@/stores/runStoreContext";

/*
 * agent 노드가 만든 자식 런(설계 7.3 ChildRunInspector). 오케스트레이션 O2의 인스펙터를 그대로 쓴다: 자식
 * 과업 세션을 1단계 ChatSessionController로 하나 더 열어 대화와 도구 카드를 그리고, 승인·질문은 자식 런에
 * 답한다. 서버가 그 응답 안에서 기다리던 워크플로우 런을 다시 큐에 넣으므로 onResponded로 실행 패널이
 * 이벤트 스트림을 잇는다. 자식이 부른 하위 에이전트(손자)의 승인 카드도 여기 뜬다.
 */
export function ChildRunInspector({
  sessionId,
  live,
  agentLabel,
  onResponded,
}: {
  sessionId: string;
  // 노드가 아직 자식을 기다린다. 접혀 있어도 자식 스트림을 받아 승인 카드를 띄운다.
  live: boolean;
  agentLabel: string;
  onResponded: () => void;
}) {
  const { t } = useTranslation("workflows");
  const scope = useChildScope();
  const [open, setOpen] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const controller = useChildSession(scope, sessionId, live || open);
  const pending = useStore(controller.store, (state) => state.pendingInteraction);
  const nested = useNestedScope(scope, controller);
  const grandchildren = useStore(
    controller.store,
    useShallow((state) =>
      Object.values(state.toolCalls)
        .filter((item) => item.phase === "child" && item.child && !item.child.end)
        .map((item) => item.id),
    ),
  );

  async function respond(interaction: PendingInteraction, value: InteractionInput) {
    setBusyId(interaction.interaction_id);
    try {
      if (await controller.respond(interaction.runId, value)) onResponded();
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
          title={t("node.approvalTitle", { agent: agentLabel })}
          autoApproveLabel={t("node.autoApprove")}
          autoApproveHint={t("node.autoApproveHint")}
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
        busy={busy}
        onAnswer={(answer) => void respond(interaction, { answer })}
      />
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {!open && live && pending ? renderInteraction(pending) : null}
      {!open && live && grandchildren.length > 0 ? (
        <RunStoreProvider store={controller.store}>
          <ChildScopeContext value={nested}>
            {grandchildren.map((id) => (
              <ChildRunCard key={id} toolCallId={id} />
            ))}
          </ChildScopeContext>
        </RunStoreProvider>
      ) : null}
      <div>
        <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen(!open)}>
          <MessagesSquare className="size-3.5" aria-hidden />
          {open ? t("node.childClose") : t("node.childOpen")}
        </Button>
      </div>
      {open ? (
        <div className="overflow-hidden rounded-md border border-line">
          <ChildConversation
            controller={controller}
            scope={scope}
            renderInteraction={renderInteraction}
          />
        </div>
      ) : null}
    </div>
  );
}
