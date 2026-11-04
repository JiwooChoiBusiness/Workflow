import { BookmarkPlus, Download, Play } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { SqlQueryResult, SqlSourceOut } from "@/api/types";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Panel } from "@/components/ui/layout";
import { CodeEditor } from "@/components/ui/lazy-code-editor";
import { useRunSqlQuery } from "@/features/sql/api";
import { csvFilename, toCsv } from "@/features/sql/results";
import { useSqlError } from "@/features/sql/useSqlError";
import { failureText } from "@/features/sql/validation";
import { saveBlob } from "@/lib/download";
import { formatDuration, formatNumber } from "@/lib/format";
import { QueryLogTable } from "./QueryLogTable";
import { ResultTable } from "./ResultTable";
import { SaveExampleDialog } from "./SaveExampleDialog";

// 결과 표는 받은 행(행 상한까지)을 이만큼만 그린다. CSV에는 받은 행이 모두 들어간다.
const SHOWN_ROWS = 200;

function ResultView({
  result,
  sourceName,
  onSaveExample,
}: {
  result: SqlQueryResult;
  sourceName: string;
  onSaveExample: () => void;
}) {
  const { t } = useTranslation("sql");
  if (!result.ok) {
    const failure = result.error ?? { code: "sql_failed", message: "", stage: "execute" as const };
    const text = failureText(failure, t);
    return (
      <Alert
        tone={failure.stage === "validate" ? "warn" : "danger"}
        title={failure.stage === "validate" ? t("test.rejected") : t("test.failed")}
      >
        <span className="flex flex-wrap items-center gap-1.5">
          <Badge tone={failure.stage === "validate" ? "warn" : "danger"}>
            {t(`stage.${failure.stage}`)}
          </Badge>
          <span className="font-mono text-xs">{failure.code}</span>
        </span>
        <span className="mt-1 block text-ink" data-testid="sql-failure-text">
          {text.title}
        </span>
        {text.detail ? <span className="mt-0.5 block text-xs">{text.detail}</span> : null}
      </Alert>
    );
  }
  const columns = result.columns ?? [];
  const rows = result.rows ?? [];
  return (
    <section aria-label={t("test.result")} className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium text-ink" data-testid="sql-result-summary">
          {t("test.summary", {
            rows: formatNumber(result.row_count),
            duration: formatDuration(result.elapsed_ms),
          })}
        </span>
        {result.truncated ? (
          <Badge tone="warn">{t("test.truncated", { max: formatNumber(result.row_count) })}</Badge>
        ) : null}
        {result.rows_cut ? (
          <Badge tone="warn">{t("test.rowsCut", { shown: formatNumber(rows.length) })}</Badge>
        ) : null}
        <span className="ml-auto flex items-center gap-1.5">
          <Button
            size="sm"
            disabled={columns.length === 0}
            onClick={() =>
              saveBlob(
                new Blob([toCsv(columns, rows)], { type: "text/csv;charset=utf-8" }),
                csvFilename(sourceName),
              )
            }
          >
            <Download className="size-3.5" aria-hidden />
            {t("test.csv")}
          </Button>
          <Button size="sm" onClick={onSaveExample}>
            <BookmarkPlus className="size-3.5" aria-hidden />
            {t("test.saveExample")}
          </Button>
        </span>
      </div>
      {result.sql_executed ? (
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-ink-muted">{t("test.executed")}</span>
            <CopyButton value={result.sql_executed} size="iconSm" variant="ghost" />
          </div>
          <pre className="max-h-40 overflow-auto rounded-md bg-code px-3 py-2 font-mono text-xs whitespace-pre-wrap text-code-ink">
            {result.sql_executed}
          </pre>
        </div>
      ) : null}
      {rows.length === 0 ? (
        <p className="text-sm text-ink-muted">{t("test.noRows")}</p>
      ) : (
        <>
          <ResultTable columns={columns} rows={rows} limit={SHOWN_ROWS} label={t("test.result")} />
          {rows.length > SHOWN_ROWS ? (
            <p className="text-xs text-ink-muted">{t("test.shown", { shown: SHOWN_ROWS })}</p>
          ) : null}
        </>
      )}
    </section>
  );
}

/*
 * SQL 시험(설계 10.4~10.6, 12.3). 노드·도구와 같은 검사·실행 경로(origin=test)로 돌리고 결과 표·실행한 SQL·잘림을
 * 보인다. 거부·실패는 코드별 문장으로, 결과는 CSV로 받을 수 있고 "예시로 저장"으로 Q&A 지식 베이스에 넣는다.
 */
export function SqlTestPanel({ source }: { source: SqlSourceOut }) {
  const { t } = useTranslation("sql");
  const run = useRunSqlQuery(source.id);
  const errorText = useSqlError();
  const [sql, setSql] = useState("");
  const [maxRows, setMaxRows] = useState("");
  // 예시로 저장은 실행한 뒤 편집기를 고쳤어도 이 결과를 낸 SQL을 넣는다.
  const [ran, setRan] = useState<{ sql: string; result: SqlQueryResult } | null>(null);
  const [saving, setSaving] = useState(false);
  const ready = source.status.state === "ready";
  const rowsValue = maxRows.trim() ? Number(maxRows) : null;
  const rowsInvalid = rowsValue !== null && (!Number.isInteger(rowsValue) || rowsValue < 1);

  function execute() {
    if (!sql.trim() || rowsInvalid) return;
    const sent = sql;
    run.mutate(
      { sql: sent, max_rows: rowsValue },
      {
        onSuccess: (result) => setRan({ sql: sent, result }),
        onError: () => setRan(null),
      },
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <Panel title={t("test.title")} description={t("test.hint")}>
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            execute();
          }}
        >
          {!ready ? <Alert tone="warn" title={t("test.notReady")} /> : null}
          <CodeEditor
            value={sql}
            onChange={setSql}
            language="text"
            ariaLabel={t("test.sql")}
            placeholder={t("test.placeholder")}
            minHeight={120}
            maxHeight={320}
          />
          <div className="flex flex-wrap items-end gap-3">
            <Field
              label={t("test.maxRows")}
              hint={t("test.maxRowsHint", { value: source.limits.max_rows })}
              className="w-40"
            >
              <Input
                type="number"
                inputMode="numeric"
                min={1}
                step={1}
                value={maxRows}
                aria-invalid={rowsInvalid || undefined}
                onChange={(event) => setMaxRows(event.target.value)}
              />
            </Field>
            <Button
              type="submit"
              variant="primary"
              className="mb-5"
              loading={run.isPending}
              disabled={!ready || !sql.trim() || rowsInvalid}
            >
              <Play className="size-3.5" aria-hidden />
              {run.isPending ? t("test.running") : t("test.run")}
            </Button>
          </div>
          {run.isError ? <Alert tone="danger" title={errorText(run.error)} /> : null}
          {ran ? (
            <ResultView
              result={ran.result}
              sourceName={source.name}
              onSaveExample={() => setSaving(true)}
            />
          ) : null}
        </form>
      </Panel>
      <Panel title={t("logs.title")}>
        <QueryLogTable sourceId={source.id} onReuse={setSql} />
      </Panel>
      <SaveExampleDialog
        sourceId={source.id}
        sql={ran?.sql ?? ""}
        open={saving}
        onOpenChange={setSaving}
      />
    </div>
  );
}
