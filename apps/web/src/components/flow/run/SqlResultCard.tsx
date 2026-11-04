import { BookmarkPlus, Download, FileText } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { SaveExampleDialog } from "@/components/sql/SaveExampleDialog";
import { ResultTable } from "@/components/sql/ResultTable";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import "@/features/sql/i18n";
import { resultSetOf } from "@/features/sql/results";
import {
  failureKind,
  failureText,
  splitNodeError,
  type FailureKind,
} from "@/features/sql/validation";
import { cn } from "@/lib/cn";
import { formatBytes, formatNumber } from "@/lib/format";
import { isFileValue, type FileUrl } from "./OutputsView";

const SHOWN_ROWS = 50;
// 거절과 데이터 소스 상태는 SQL을 실행하지 않은 것이라 경고색을 쓰지 않는다.
const FAILURE_TONE: Record<FailureKind, BadgeTone> = {
  validate: "warn",
  execute: "danger",
  rejected: "neutral",
  source: "neutral",
};

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/*
 * sql_query 노드 런의 결과(설계 10.6, 12.3): 실행한 SQL, 앞 50행 표, 행 수, 잘림, 잘린 결과 CSV 파일 링크. 실패면
 * 오류 코드를 사용자 문장으로 보이고 시도한 SQL을 남긴다(승인 카드에서 고친 SQL이 재검사에서 거부된 경우도
 * 여기서 이유를 본다). 콘솔에서는 성공한 SQL을 "예시로 저장"으로 Q&A 지식 베이스에 넣는다.
 */
export function SqlResultCard({
  outputs,
  inputs,
  error,
  fileUrl,
  preview = false,
  examples = false,
}: {
  outputs: unknown;
  inputs: unknown;
  error: { code: string; message: string } | null;
  fileUrl?: FileUrl;
  // 이벤트의 4KB 미리보기라 행이 일부만 있다.
  preview?: boolean;
  // 예시로 저장 단추(콘솔만)
  examples?: boolean;
}) {
  const { t } = useTranslation("sql");
  const [saving, setSaving] = useState(false);
  const out = record(outputs);
  const sql = typeof out.sql_executed === "string" ? out.sql_executed : "";
  const failure = error ?? splitNodeError(typeof out.error === "string" ? out.error : "") ?? null;
  const given = record(inputs);
  const sourceId = given.source_id;
  // 예시에는 LIMIT을 넣고 열을 풀어 쓰기 전의 SQL(노드 입력)을 넣는다.
  const exampleSql = typeof given.sql === "string" && given.sql.trim() ? given.sql : sql;

  if (failure) {
    const text = failureText(failure, t);
    const kind = failureKind(failure.code);
    const tone = FAILURE_TONE[kind];
    return (
      <section aria-label={t("result.title")} className="flex flex-col gap-2">
        <div
          className={cn(
            "flex flex-col gap-1 rounded-md border px-3 py-2 text-xs",
            tone === "neutral" ? "border-line bg-sunken" : "border-warn/30 bg-warn-soft",
          )}
        >
          <Badge tone={tone} className="self-start">
            {t(`stage.${kind}`)}
          </Badge>
          <p className="text-ink" data-testid="sql-node-failure">
            {text.title}
          </p>
        </div>
        {sql ? (
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium text-ink-muted">{t("result.attempted")}</span>
            <pre className="max-h-40 overflow-auto rounded-md bg-code px-2.5 py-1.5 font-mono text-xs whitespace-pre-wrap text-code-ink">
              {sql}
            </pre>
          </div>
        ) : null}
      </section>
    );
  }

  const set = resultSetOf(outputs);
  const rowCount = typeof out.row_count === "number" ? out.row_count : (set?.rows.length ?? 0);
  const truncated = out.truncated === true;
  const file = isFileValue(out.file) ? out.file : null;
  const href = file && fileUrl ? fileUrl(file.file_id) : null;
  const rows = set?.rows ?? [];
  return (
    <section aria-label={t("result.title")} className="flex flex-col gap-2">
      {sql ? (
        <div className="flex flex-col gap-1">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-ink-muted">{t("result.sql")}</span>
            <span className="flex items-center gap-1">
              {examples && typeof sourceId === "string" ? (
                <Button size="sm" variant="ghost" onClick={() => setSaving(true)}>
                  <BookmarkPlus className="size-3.5" aria-hidden />
                  {t("result.saveExample")}
                </Button>
              ) : null}
              <CopyButton value={sql} size="iconSm" variant="ghost" />
            </span>
          </div>
          <pre className="max-h-40 overflow-auto rounded-md bg-code px-2.5 py-1.5 font-mono text-xs whitespace-pre-wrap text-code-ink">
            {sql}
          </pre>
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-ink-muted">
        <span className="font-medium text-ink" data-testid="sql-node-rows">
          {t("result.rows", { rows: formatNumber(rowCount) })}
        </span>
        {truncated ? <Badge tone="warn">{t("result.truncated")}</Badge> : null}
        {rows.length > 0 && rows.length < rowCount ? (
          <span>{t("result.shown", { shown: Math.min(rows.length, SHOWN_ROWS) })}</span>
        ) : rows.length > SHOWN_ROWS ? (
          <span>{t("result.shown", { shown: SHOWN_ROWS })}</span>
        ) : null}
      </div>
      {set && set.columns.length > 0 && rows.length > 0 ? (
        <ResultTable
          columns={set.columns}
          rows={rows}
          limit={SHOWN_ROWS}
          label={t("result.table")}
        />
      ) : null}
      {preview ? <p className="text-xs text-ink-subtle">{t("result.previewOnly")}</p> : null}
      {file ? (
        <p className="flex items-center gap-2 text-xs">
          <FileText className="size-3.5 shrink-0 text-ink-subtle" aria-hidden />
          <span className="text-ink-muted">{t("result.file")}</span>
          {href ? (
            <a
              href={href}
              download
              className="inline-flex min-w-0 items-center gap-1 text-accent hover:underline"
            >
              <span className="truncate">{file.filename}</span>
              <Download className="size-3.5 shrink-0" aria-hidden />
            </a>
          ) : (
            <span className="truncate text-ink">{file.filename}</span>
          )}
          <span className="text-ink-subtle tabular-nums">{formatBytes(file.size_bytes)}</span>
        </p>
      ) : null}
      {examples && typeof sourceId === "string" ? (
        <SaveExampleDialog
          sourceId={sourceId}
          sql={exampleSql}
          open={saving}
          onOpenChange={setSaving}
        />
      ) : null}
    </section>
  );
}
