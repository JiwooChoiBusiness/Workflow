import { ChevronLeft, ChevronRight, CornerDownLeft, History } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { SqlQueryLogOut } from "@/api/types";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { relativeBase } from "@/features/knowledge/documentStatus";
import { LOG_PAGE_SIZE, useSqlQueryLogs } from "@/features/sql/api";
import { sqlCodeText } from "@/features/sql/validation";
import { formatDuration, formatNumber, formatRelative } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { SqlErrorAlert } from "./SqlErrorAlert";

const STATUS_TONE: Record<SqlQueryLogOut["status"], BadgeTone> = {
  ok: "ok",
  rejected: "warn",
  error: "danger",
  timeout: "danger",
};

/*
 * 질의 기록(설계 10.4 sql_query_log). SQL 시험·노드·에이전트 도구가 남긴 검사·실행 결과다. 거부된 질의는 그
 * 이유를 문장으로 보이고, 누르면 그 SQL로 편집기를 채운다.
 */
export function QueryLogTable({
  sourceId,
  onReuse,
}: {
  sourceId: string;
  onReuse: (sql: string) => void;
}) {
  const { t } = useTranslation("sql");
  const { t: tc } = useTranslation();
  const now = useNow();
  const [page, setPage] = useState(1);
  const logs = useSqlQueryLogs(sourceId, page);

  if (logs.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (logs.isError) {
    return (
      <SqlErrorAlert
        error={logs.error}
        title={t("logs.loadFailed")}
        onRetry={() => void logs.refetch()}
      />
    );
  }
  if (logs.data.items.length === 0) return <EmptyState icon={History} title={t("logs.empty")} />;
  const hasMore = logs.data.page * LOG_PAGE_SIZE < logs.data.total;
  return (
    <div className="flex flex-col gap-2">
      <table className="w-full text-sm" aria-label={t("logs.title")}>
        <thead className="text-left text-xs text-ink-muted">
          <tr>
            <th className="pb-2 font-medium">{t("logs.column.sql")}</th>
            <th className="pb-2 font-medium">{t("logs.column.origin")}</th>
            <th className="pb-2 font-medium">{t("logs.column.status")}</th>
            <th className="pb-2 text-right font-medium">{t("logs.column.rows")}</th>
            <th className="pb-2 text-right font-medium">{t("logs.column.duration")}</th>
            <th className="pb-2 pl-3 font-medium">{t("logs.column.time")}</th>
            <th className="pb-2" />
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {logs.data.items.map((log) => {
            const reason = log.reject_code
              ? (sqlCodeText(log.reject_code, t) ?? log.error)
              : log.error;
            return (
              <tr key={log.id} className="align-top">
                <td className="max-w-80 py-1.5 pr-3">
                  <p className="truncate font-mono text-xs text-ink" title={log.sql}>
                    {log.sql}
                  </p>
                  {reason ? (
                    <p className="truncate text-xs text-ink-muted" title={log.error ?? reason}>
                      {reason}
                    </p>
                  ) : null}
                </td>
                <td className="py-1.5 pr-3">
                  <Badge>{t(`logs.origin.${log.origin}`)}</Badge>
                </td>
                <td className="py-1.5 pr-3">
                  <Badge tone={STATUS_TONE[log.status]}>{t(`logs.status.${log.status}`)}</Badge>
                </td>
                <td className="py-1.5 pr-3 text-right tabular-nums">
                  {log.row_count === null ? "-" : formatNumber(log.row_count)}
                </td>
                <td className="py-1.5 pr-3 text-right text-xs text-ink-muted tabular-nums">
                  {formatDuration(log.duration_ms)}
                </td>
                <td className="py-1.5 pl-3 text-xs whitespace-nowrap text-ink-muted">
                  {formatRelative(log.created_at, relativeBase(log.created_at, now))}
                </td>
                <td className="py-1 text-right">
                  <Button
                    size="iconSm"
                    variant="ghost"
                    aria-label={t("logs.reuse")}
                    title={t("logs.reuse")}
                    onClick={() => onReuse(log.sql)}
                  >
                    <CornerDownLeft className="size-3.5" aria-hidden />
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <div className="flex items-center justify-end gap-1">
        <Button
          size="iconSm"
          variant="ghost"
          disabled={page <= 1}
          aria-label={t("logs.prev")}
          title={t("logs.prev")}
          onClick={() => setPage(page - 1)}
        >
          <ChevronLeft className="size-4" aria-hidden />
        </Button>
        <Button
          size="iconSm"
          variant="ghost"
          disabled={!hasMore}
          aria-label={t("logs.next")}
          title={t("logs.next")}
          onClick={() => setPage(page + 1)}
        >
          <ChevronRight className="size-4" aria-hidden />
        </Button>
      </div>
    </div>
  );
}
