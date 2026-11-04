import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import "@/features/workflows/i18n";
import type { WfRunError } from "@/features/workflows/wfRunStore";
import { formatDateTime } from "@/lib/format";

/*
 * 워크플로우 런의 오류. 고른 모델이 모두 일일 한도에 걸린 실패는 retry_after(3단계 12.4)가 있어 언제 다시 할
 * 수 있는지 함께 알린다.
 */
export function RunErrorAlert({
  error,
  nodeTitle,
}: {
  error: Pick<WfRunError, "code" | "message" | "nodeId" | "retryAt">;
  nodeTitle?: (nodeId: string) => string;
}) {
  const { t } = useTranslation("workflows");
  return (
    <Alert tone="danger" title={t("run.error", { code: error.code })}>
      {error.nodeId && nodeTitle ? `${nodeTitle(error.nodeId)}: ` : ""}
      {error.message}
      {error.retryAt ? (
        <span className="mt-1 block text-xs">
          {t("run.retryAt", { time: formatDateTime(error.retryAt) })}
        </span>
      ) : null}
    </Alert>
  );
}
