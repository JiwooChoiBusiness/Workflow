import { ChevronLeft, ChevronRight, CornerDownLeft, History, ListPlus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/layout";
import { PageSpinner } from "@/components/ui/spinner";
import { useQueryLogs } from "@/features/knowledge/api";
import { relativeBase } from "@/features/knowledge/documentStatus";
import { formatRelative } from "@/lib/format";
import { useNow } from "@/lib/useNow";
import { KbErrorAlert } from "./KbErrorAlert";

// 질의 기록(설계 7.7, Dify의 Records). 누르면 그 질의로 검색 시험 칸을 채우고, 옆 단추로 평가셋에 넣는다.
export function QueryLogTable({
  kbId,
  onReuse,
  onAddCase,
}: {
  kbId: string;
  onReuse: (query: string) => void;
  onAddCase: (query: string) => void;
}) {
  const { t } = useTranslation("knowledge");
  const { t: tc } = useTranslation();
  const now = useNow();
  const [page, setPage] = useState(1);
  const logs = useQueryLogs(kbId, page);

  if (logs.isPending) return <PageSpinner label={tc("state.loading")} />;
  if (logs.isError) {
    return (
      <KbErrorAlert
        error={logs.error}
        title={t("logs.loadFailed")}
        onRetry={() => void logs.refetch()}
      />
    );
  }
  if (logs.data.items.length === 0) return <EmptyState icon={History} title={t("logs.empty")} />;
  return (
    <div className="flex flex-col gap-2">
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-ink-muted">
          <tr>
            <th className="pb-2 font-medium">{t("logs.column.query")}</th>
            <th className="pb-2 font-medium">{t("logs.column.source")}</th>
            <th className="pb-2 text-right font-medium">{t("logs.column.results")}</th>
            <th className="pb-2 text-right font-medium">{t("logs.column.latency")}</th>
            <th className="pb-2 pl-3 font-medium">{t("logs.column.time")}</th>
            <th className="pb-2" />
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {logs.data.items.map((log) => (
            <tr key={log.id}>
              <td className="max-w-72 truncate py-1.5 pr-3 text-ink" title={log.query}>
                {log.query}
              </td>
              <td className="py-1.5 pr-3">
                <Badge>{t(`logs.sources.${log.source}`)}</Badge>
              </td>
              <td className="py-1.5 pr-3 text-right tabular-nums">{log.result_count}</td>
              <td className="py-1.5 pr-3 text-right text-xs text-ink-muted tabular-nums">
                {log.latency_ms}ms
              </td>
              <td className="py-1.5 pl-3 text-xs whitespace-nowrap text-ink-muted">
                {formatRelative(log.created_at, relativeBase(log.created_at, now))}
              </td>
              <td className="py-1 text-right whitespace-nowrap">
                <Button
                  size="iconSm"
                  variant="ghost"
                  aria-label={`${t("logs.reuse")}: ${log.query}`}
                  title={t("logs.reuse")}
                  onClick={() => onReuse(log.query)}
                >
                  <CornerDownLeft className="size-3.5" aria-hidden />
                </Button>
                <Button
                  size="iconSm"
                  variant="ghost"
                  aria-label={`${t("logs.addCase")}: ${log.query}`}
                  title={t("logs.addCase")}
                  onClick={() => onAddCase(log.query)}
                >
                  <ListPlus className="size-3.5" aria-hidden />
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="flex items-center justify-end gap-1">
        <Button
          size="iconSm"
          variant="ghost"
          disabled={page <= 1}
          aria-label={t("documents.prev")}
          title={t("documents.prev")}
          onClick={() => setPage(page - 1)}
        >
          <ChevronLeft className="size-4" aria-hidden />
        </Button>
        <Button
          size="iconSm"
          variant="ghost"
          disabled={!logs.data.has_more}
          aria-label={t("documents.next")}
          title={t("documents.next")}
          onClick={() => setPage(page + 1)}
        >
          <ChevronRight className="size-4" aria-hidden />
        </Button>
      </div>
    </div>
  );
}
