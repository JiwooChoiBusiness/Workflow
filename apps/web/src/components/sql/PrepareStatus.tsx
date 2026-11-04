import { RefreshCw, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { SqlSourceOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toast";
import { usePrepareSqlSource } from "@/features/sql/api";
import { useSqlError } from "@/features/sql/useSqlError";
import type { ST } from "@/features/sql/validation";
import { formatBytes, formatDateTime, formatDuration } from "@/lib/format";
import { SqlStateBadge } from "./SqlStateBadge";

interface Warning {
  key: string;
  text: string;
  detail: string | null;
}

// 준비가 알린 것: 자동으로 숨긴 뷰(view_exposes_hidden), 깨진 뷰(view_broken), 표마다 남은 오류.
function warningsOf(source: SqlSourceOut, t: ST): Warning[] {
  const found: Warning[] = [];
  const covered = new Set<string>();
  for (const [index, warning] of (source.schema?.warnings ?? []).entries()) {
    const table = warning.table ?? "";
    if (table) covered.add(table);
    if (warning.code === "view_exposes_hidden" || warning.code === "view_broken") {
      found.push({
        key: `${warning.code}:${table}:${index}`,
        text: t(`prepare.warning.${warning.code}`, { table }),
        detail: warning.code === "view_broken" ? warning.message : null,
      });
    } else {
      found.push({ key: `${warning.code}:${index}`, text: warning.message, detail: null });
    }
  }
  for (const table of source.schema?.tables ?? []) {
    if (!table.error || covered.has(table.name)) continue;
    found.push({
      key: `error:${table.name}`,
      text: t("prepare.tableError", { table: table.name, error: table.error }),
      detail: null,
    });
  }
  return found;
}

/*
 * 준비 상태(설계 10.2): empty·not_prepared·stale·ready와 준비 단추, 질의 파일(또는 숨김을 뺀 SQLite 사본) 크기,
 * 준비가 남긴 경고. 준비는 끝날 때까지 기다리는 요청이다. CSV·엑셀은 DuckDB 런타임이 있어야 한다.
 */
export function PrepareStatus({
  source,
  runtimeReady,
}: {
  source: SqlSourceOut;
  // files 소스의 런타임 준비 여부(모르면 null). sqlite 소스는 늘 true로 넘긴다.
  runtimeReady: boolean | null;
}) {
  const { t } = useTranslation("sql");
  const prepare = usePrepareSqlSource(source.id);
  const fileName = (fileId: string) =>
    source.files.find((file) => file.file_id === fileId)?.filename;
  const errorText = useSqlError({ fileName });
  const state = source.status.state;
  const schema = source.schema ?? null;
  const warnings = warningsOf(source, t);
  const blockedByRuntime = source.kind === "files" && runtimeReady === false;
  const canPrepare = state !== "empty" && !blockedByRuntime;

  let summary: string;
  if (state === "empty") summary = t("prepare.emptyHint");
  else if (state === "not_prepared") {
    summary =
      source.kind === "files" ? t("prepare.notPreparedFiles") : t("prepare.notPreparedSqlite");
  } else if (state === "stale") summary = t("prepare.staleHint");
  else summary = schema ? t("prepare.readyAt", { time: formatDateTime(schema.captured_at) }) : "";

  const facts: string[] = [];
  if (state === "ready" && schema) {
    facts.push(t("prepare.tables", { count: schema.tables.length }));
    if (typeof schema.duration_ms === "number") {
      facts.push(t("prepare.duration", { duration: formatDuration(schema.duration_ms) }));
    }
    if (source.materialized) {
      const size = formatBytes(source.materialized.bytes);
      facts.push(
        source.kind === "files"
          ? t("prepare.queryFile", { size })
          : t("prepare.copyFile", { size }),
      );
    } else if (source.kind === "sqlite") {
      facts.push(t("prepare.original"));
    }
  }

  return (
    <section
      aria-label={t("prepare.title")}
      className="flex flex-col gap-2 rounded-lg border border-line bg-surface px-4 py-3"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <SqlStateBadge state={state} />
        <p className="min-w-0 flex-1 text-sm text-ink">{summary}</p>
        <Button
          size="sm"
          variant={state === "ready" ? "secondary" : "primary"}
          loading={prepare.isPending}
          disabled={!canPrepare}
          title={blockedByRuntime ? t("prepare.runtimeFirst") : undefined}
          onClick={() =>
            prepare.mutate(undefined, {
              onSuccess: () => toast.success(t("prepare.done")),
            })
          }
        >
          <RefreshCw className="size-3.5" aria-hidden />
          {state === "ready" || state === "stale" ? t("prepare.again") : t("prepare.button")}
        </Button>
      </div>
      {facts.length > 0 ? (
        <p className="text-xs text-ink-muted" data-testid="sql-prepare-facts">
          {facts.join(" · ")}
        </p>
      ) : null}
      {state === "stale" && source.status.message ? (
        <p className="text-xs text-warn">{source.status.message}</p>
      ) : null}
      {blockedByRuntime && state !== "empty" ? (
        <p className="text-xs text-ink-muted">{t("prepare.runtimeFirst")}</p>
      ) : null}
      {prepare.isError ? (
        <Alert tone="danger" title={t("prepare.failed")}>
          {errorText(prepare.error)}
        </Alert>
      ) : null}
      {warnings.length > 0 ? (
        <ul aria-label={t("prepare.warnings")} className="flex flex-col gap-1">
          {warnings.map((warning) => (
            <li key={warning.key} className="flex items-start gap-1.5 text-xs text-warn">
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              <span>
                {warning.text}
                {warning.detail ? (
                  <span className="block text-ink-muted">{warning.detail}</span>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
