import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useKnowledgeError } from "@/features/knowledge/useKnowledgeError";
import { errorDetail } from "@/lib/errors";

// ErrorAlert와 같지만 지식 API의 오류 코드를 화면 문장으로 바꿔 보인다.
export function KbErrorAlert({
  error,
  title,
  onRetry,
  className,
}: {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
}) {
  const { t } = useTranslation();
  const errorText = useKnowledgeError();
  const message = errorText(error);
  const detail = errorDetail(error, t);
  return (
    <Alert
      tone="danger"
      title={title ?? message}
      className={className}
      action={
        onRetry ? (
          <Button size="sm" onClick={onRetry}>
            {t("actions.retry")}
          </Button>
        ) : null
      }
    >
      {title ? <span className="block">{message}</span> : null}
      {detail ? <span className="block text-xs">{detail}</span> : null}
    </Alert>
  );
}
