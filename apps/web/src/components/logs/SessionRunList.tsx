import { useTranslation } from "react-i18next";
import { ErrorAlert } from "@/components/common/ErrorAlert";
import { Spinner } from "@/components/ui/spinner";
import type { RunLogItem } from "@/features/logs/types";
import { cn } from "@/lib/cn";
import { formatDateTime, formatNumber } from "@/lib/format";
import { RunStatusBadge } from "./badges";

// 세션의 런 목록(설계 7.3 RunList). 고르면 인스펙터가 그 런을 보인다.
export function SessionRunList({
  runs,
  loading,
  error,
  onRetry,
  selectedRunId,
  onSelect,
}: {
  runs: RunLogItem[];
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  selectedRunId: string | null;
  // view=children이면 인스펙터의 하위 런 탭을 연다.
  onSelect: (runId: string, view?: "children") => void;
}) {
  const { t } = useTranslation("logs");
  if (loading) return <Spinner label={t("runs.loading")} />;
  if (error) return <ErrorAlert error={error} title={t("runs.loadFailed")} onRetry={onRetry} />;
  if (runs.length === 0) return <p className="text-sm text-ink-muted">{t("runs.none")}</p>;
  return (
    <ul className="flex max-h-64 flex-col gap-1 overflow-y-auto" aria-label={t("runs.label")}>
      {runs.map((run, index) => {
        const selected = run.id === selectedRunId;
        return (
          <li key={run.id} className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => onSelect(run.id)}
              aria-current={selected ? "true" : undefined}
              className={cn(
                "flex min-w-0 flex-1 flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-2.5 py-1.5 text-left text-xs",
                selected ? "bg-accent-soft text-ink" : "text-ink-muted hover:bg-hover",
              )}
            >
              <span className="font-medium text-ink tabular-nums">
                {t("runs.number", { number: runs.length - index })}
              </span>
              <RunStatusBadge status={run.status} />
              <span className="tabular-nums">{formatDateTime(run.queued_at)}</span>
              <span className="tabular-nums">{t("runs.steps", { count: run.steps })}</span>
              <span className="tabular-nums">
                {t("tokensPair", {
                  input: formatNumber(run.usage.input_tokens),
                  output: formatNumber(run.usage.output_tokens),
                })}
              </span>
              {run.error ? <span className="font-mono text-danger">{run.error.code}</span> : null}
            </button>
            {run.child_count > 0 ? (
              <button
                type="button"
                onClick={() => onSelect(run.id, "children")}
                title={t("children.countHint", { count: run.child_count })}
                className="inline-flex h-5 shrink-0 items-center rounded-sm border border-line bg-sunken px-1.5 text-xs text-ink-muted hover:text-accent"
              >
                {t("children.count", { count: run.child_count })}
              </button>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
