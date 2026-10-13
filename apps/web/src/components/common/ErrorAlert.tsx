import { useTranslation } from "react-i18next";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { errorDetail, errorMessage, isMissingEndpoint } from "@/lib/errors";

export function ErrorAlert({
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
  const message = errorMessage(error, t);
  const detail = errorDetail(error, t);
  // 아직 없는 API는 오류라기보다 준비 중이라 경고 색으로 보인다.
  const tone = isMissingEndpoint(error) ? "warn" : "danger";
  return (
    <Alert
      tone={tone}
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
