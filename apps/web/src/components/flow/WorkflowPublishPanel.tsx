import { useTranslation } from "react-i18next";
import { isApiError } from "@/api/http";
import type { WfIssue } from "@/api/types";
import { PublishPanelView } from "@/components/builder/PublishPanel";
import {
  usePublishWorkflow,
  useRestoreWorkflowVersion,
  useWorkflowVersions,
} from "@/features/workflows/api";
import { isDirty } from "@/features/workflows/flowStore";
import { useFlow, useFlowActions } from "@/features/workflows/flowStoreContext";

// 발행·버전·롤백(설계 6.1). 1단계 PublishPanel을 쓰고, 발행 전에 초안을 먼저 저장한다(설계 7.3).
export function WorkflowPublishPanel({
  workflowId,
  flush,
}: {
  workflowId: string;
  flush: () => Promise<boolean>;
}) {
  const { t } = useTranslation("workflows");
  const actions = useFlowActions();
  const dirty = useFlow(isDirty);
  const versions = useWorkflowVersions(workflowId);
  const publish = usePublishWorkflow(workflowId);
  const restore = useRestoreWorkflowVersion(workflowId);
  return (
    <PublishPanelView
      source={{ versions, publish, restore }}
      dirty={dirty}
      beforePublish={flush}
      description={t("publish.description")}
      pendingText={t("publish.pending")}
      onPublishError={(error) => {
        // draft_invalid의 details는 검증 이슈 목록이다. 캔버스와 속성 패널에 바로 보이게 넣는다.
        if (
          isApiError(error, 422) &&
          error.code === "draft_invalid" &&
          Array.isArray(error.details)
        ) {
          actions.setIssues(error.details as WfIssue[]);
        }
      }}
    />
  );
}
