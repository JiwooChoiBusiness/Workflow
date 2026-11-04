import { Clock } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { KbDocumentOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { documentError } from "@/features/knowledge/documentStatus";
import { formatDateTime } from "@/lib/format";
import { useNow } from "@/lib/useNow";

// 일일 임베딩 한도로 멈춘 문서가 있을 때 KB 화면 위에 보인다(설계 4.4, 6.6). 자동으로 다시 넣지 않으므로
// 풀리는 시각과 "다시 시도"를 안내한다.
export function RateLimitNotice({
  count,
  retryAfter,
  className,
}: {
  count: number;
  retryAfter: string | null | undefined;
  className?: string;
}) {
  const { t } = useTranslation("knowledge");
  const now = useNow();
  if (count <= 0) return null;
  const at = retryAfter ? new Date(retryAfter).getTime() : null;
  return (
    <Alert tone="warn" title={t("limit.documents", { count })} className={className}>
      <span className="block">
        {at !== null && at > now
          ? t("limit.retryAt", { time: formatDateTime(retryAfter) })
          : t("limit.retryNow")}
      </span>
      <span className="block text-xs">{t("limit.reset")}</span>
    </Alert>
  );
}

// 문서 표·문서 화면에서 일일 한도로 멈춘 문서 한 줄 안내.
export function DocumentLimitNote({ document }: { document: Pick<KbDocumentOut, "error"> }) {
  const { t } = useTranslation("knowledge");
  const now = useNow();
  const error = documentError(document);
  if (error?.code !== "rate_limited_daily") return null;
  const waiting = error.retryAfter !== null && error.retryAfter.getTime() > now;
  return (
    <span className="inline-flex items-center gap-1 text-xs text-warn">
      <Clock className="size-3" aria-hidden />
      {t("limit.document")} ·{" "}
      {waiting && error.retryAfter
        ? t("limit.documentRetryAt", { time: formatDateTime(error.retryAfter.toISOString()) })
        : t("limit.documentRetryNow")}
    </span>
  );
}
