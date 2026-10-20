import { useTranslation } from "react-i18next";
import type { WfInteractionInput } from "@/api/run-events";
import type { WfFileOut } from "@/api/types";
import { ApprovalPrompt } from "@/components/chat/ApprovalPrompt";
import { FormPrompt } from "@/components/chat/FormPrompt";
import { InteractionPrompt } from "@/components/chat/InteractionPrompt";
import "@/features/workflows/i18n";
import type { WfPendingInteraction } from "@/features/workflows/wfRunStore";

function noUpload(): Promise<WfFileOut> {
  return Promise.reject(new Error("upload_unavailable"));
}

/*
 * 워크플로우 런이 기다리는 승인·질문·폼(workflow.waiting_input). 응답은 워크플로우 런의 /input으로 보낸다.
 * 승인·질문은 서버가 자식 런에 넘긴 뒤, 폼(human_input)은 검사한 값을 담아 같은 런을 다시 넣는다(설계 3.7).
 * 로그 화면과 웹앱이 쓰고, 편집기는 자식 세션을 열 수 없을 때만 쓴다(폼은 늘 여기서 받는다).
 */
export function PendingPrompt({
  pending,
  nodeTitle,
  busy,
  onRespond,
  upload,
  onSubmitForm,
}: {
  pending: WfPendingInteraction;
  nodeTitle: string;
  busy: boolean;
  onRespond: (input: WfInteractionInput) => void;
  // 폼의 파일 칸을 올리는 곳(콘솔·웹앱 경로가 다르다)
  upload?: (file: File) => Promise<WfFileOut>;
  // 폼 응답. 서버가 받았으면 true, 아니면 그 오류(칸 오류·안내로 보인다). 없으면 onRespond로 보낸다.
  onSubmitForm?: (values: Record<string, unknown>) => Promise<true | unknown>;
}) {
  const { t } = useTranslation("workflows");
  if (pending.kind === "form") {
    return (
      <FormPrompt
        key={pending.form.form_id}
        form={pending.form}
        nodeTitle={nodeTitle}
        busy={busy}
        upload={upload ?? noUpload}
        onSubmit={
          onSubmitForm ??
          ((values) => {
            onRespond({ kind: "form", values });
            return Promise.resolve(true);
          })
        }
      />
    );
  }
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
