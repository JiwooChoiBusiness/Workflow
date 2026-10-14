import { useTranslation } from "react-i18next";
import type { InteractionInput } from "@/api/run-events";
import { ApprovalPrompt } from "@/components/chat/ApprovalPrompt";
import { InteractionPrompt } from "@/components/chat/InteractionPrompt";
import "@/features/workflows/i18n";
import type { WfPendingInteraction } from "@/features/workflows/wfRunStore";

/*
 * 워크플로우 런이 기다리는 승인·질문(workflow.waiting_input). 응답은 워크플로우 런의 /input으로 보내고
 * 서버가 자식 런에 넘긴 뒤 같은 런을 다시 넣는다(설계 3.7). 로그 화면과 웹앱이 쓰고, 편집기는 자식 세션을
 * 열 수 없을 때만 쓴다.
 */
export function PendingPrompt({
  pending,
  nodeTitle,
  busy,
  onRespond,
}: {
  pending: WfPendingInteraction;
  nodeTitle: string;
  busy: boolean;
  onRespond: (input: InteractionInput) => void;
}) {
  const { t } = useTranslation("workflows");
  const interaction = pending.interaction;
  if (interaction.kind === "approval") {
    return (
      <ApprovalPrompt
        interaction={interaction}
        busy={busy}
        title={t("run.pendingApproval", { node: nodeTitle })}
        autoApproveLabel={t("node.autoApprove")}
        autoApproveHint={t("node.autoApproveHint")}
        onApprove={({ sessionAutoApprove }) =>
          onRespond({ decision: "approve", session_auto_approve: sessionAutoApprove })
        }
        onReject={(note) => onRespond({ decision: "reject", note: note || null })}
      />
    );
  }
  return (
    <InteractionPrompt
      interaction={interaction}
      busy={busy}
      onAnswer={(answer) => onRespond({ answer })}
    />
  );
}
