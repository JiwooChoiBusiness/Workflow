import { ChevronRight, Download, FileText } from "lucide-react";
import { Collapsible } from "radix-ui";
import { useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ResultTable } from "@/components/sql/ResultTable";
import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Spinner } from "@/components/ui/spinner";
import { parseToolResult } from "@/features/sql/results";
import { cn } from "@/lib/cn";
import { formatBytes, formatDuration, formatNumber } from "@/lib/format";
import { CALL_AGENT, type ToolCallView } from "@/stores/runStore";
import { useRunStore } from "@/stores/runStoreContext";
import { ChildRunCard } from "./ChildRunCard";
import { CitationChips } from "./CitationChips";
import { ToolIcon } from "./ToolIcon";
import {
  knowledgeHits,
  prettyJson,
  QUERY_SQL,
  SEARCH_KNOWLEDGE,
  summarizeInput,
} from "./toolSummary";

type FileHref = (sessionFileId: string) => string | null;

const RESULT_TONE: Record<string, BadgeTone> = {
  ok: "ok",
  error: "danger",
  timeout: "warn",
  cancelled: "neutral",
  rejected: "neutral",
};

function StatusBadge({ tool }: { tool: ToolCallView }) {
  const { t } = useTranslation("chat");
  if (tool.phase === "done" && tool.result) {
    return (
      <Badge tone={RESULT_TONE[tool.result] ?? "neutral"}>{t(`tool.result.${tool.result}`)}</Badge>
    );
  }
  if (tool.phase === "done") return null;
  const tone: BadgeTone = tool.phase === "awaiting_approval" ? "warn" : "info";
  return (
    <Badge tone={tone}>
      {tool.phase === "running" || tool.phase === "input" ? <Spinner className="size-3" /> : null}
      {t(`tool.phase.${tool.phase}`)}
    </Badge>
  );
}

function ToolOutput({ tool }: { tool: ToolCallView }) {
  const { t } = useTranslation("chat");
  const ref = useRef<HTMLPreElement>(null);
  const stickRef = useRef(true);

  // 사용자가 위로 올려 읽는 중이 아니면 새 출력이 올 때 맨 아래를 보여 준다.
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [tool.outputChars, tool.droppedChars]);

  if (tool.output.length === 0) {
    return <p className="text-xs text-ink-subtle">{t("tool.noOutput")}</p>;
  }
  return (
    <pre
      ref={ref}
      onScroll={(event) => {
        const el = event.currentTarget;
        stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }}
      className="max-h-80 overflow-auto rounded-md bg-term px-3 py-2 font-mono text-xs leading-relaxed whitespace-pre-wrap text-term-ink"
    >
      {tool.output.map((chunk, index) => (
        <span key={index} className={chunk.stream === "stderr" ? "text-term-err" : undefined}>
          {chunk.text}
        </span>
      ))}
    </pre>
  );
}

/*
 * search_knowledge 결과 요약(3단계 설계 12.3): 결과·문서 수와 1위 문서, 인용 칩. 실시간 카드는
 * tool_call.completed의 인용으로, 메시지 기록에서 다시 그린 카드는 결과 글의 <document> 머리로 요약한다.
 */
function KnowledgeSummary({ tool, citationLinks }: { tool: ToolCallView; citationLinks: boolean }) {
  const { t } = useTranslation("chat");
  const resources = tool.retrieverResources;
  const hits = resources
    ? resources.map((item) => ({
        position: item.position,
        documentName: item.document_name,
        documentKey: item.document_id,
      }))
    : knowledgeHits(tool.output.map((chunk) => chunk.text).join("")).map((hit) => ({
        ...hit,
        documentKey: `${hit.datasetName}/${hit.documentName}`,
      }));
  if (tool.result !== "ok") return null;
  const documents = new Set(hits.map((hit) => hit.documentKey)).size;
  return (
    <div className="flex flex-col gap-1.5 border-t border-line px-3 py-2">
      <p className="text-xs text-ink-muted">
        {hits.length === 0
          ? t("tool.knowledgeNone")
          : t("tool.knowledgeSummary", {
              count: hits.length,
              documents,
              name: hits[0]?.documentName ?? "",
            })}
      </p>
      {resources?.length ? (
        <CitationChips citations={resources} chunkLinks={citationLinks} />
      ) : null}
    </div>
  );
}

/*
 * query_sql 결과(3단계 설계 10.3, 12.3): 행 수·잘림·실행한 SQL과 결과 글의 표(앞 llm_rows행). 잘렸으면 전체가
 * 작업공간 sql/<호출>.csv에 남고, 런이 끝나 file.changed가 오면 그 파일을 내려받는 링크가 된다.
 */
function SqlSummary({ tool, fileHref }: { tool: ToolCallView; fileHref?: FileHref }) {
  const { t } = useTranslation("chat");
  const output = tool.output.map((chunk) => chunk.text).join("");
  const parsed = parseToolResult(output);
  const csvPath = parsed.csvPath;
  const file = useRunStore((state) =>
    csvPath ? state.files.find((item) => item.path === csvPath) : undefined,
  );
  if (tool.result !== "ok") return null;
  const executed = tool.sql?.sql_executed ?? parsed.executed;
  const rows = tool.sql?.row_count ?? parsed.rowCount ?? parsed.table?.rows.length ?? 0;
  const truncated = tool.sql?.truncated ?? parsed.truncated;
  const href =
    file && fileHref && file.downloadable && file.sessionFileId
      ? fileHref(file.sessionFileId)
      : null;
  const shown = parsed.table?.rows.length ?? 0;
  return (
    <div className="flex flex-col gap-1.5 border-t border-line px-3 py-2">
      <p className="flex flex-wrap items-center gap-1.5 text-xs text-ink-muted">
        <span className="font-medium text-ink">
          {t("tool.sqlRows", { rows: formatNumber(rows) })}
        </span>
        {truncated ? <Badge tone="warn">{t("tool.sqlTruncated")}</Badge> : null}
        {shown > 0 && shown < rows ? <span>{t("tool.sqlShown", { shown })}</span> : null}
      </p>
      {executed ? (
        <pre
          className="max-h-24 overflow-auto rounded-md bg-code px-2.5 py-1.5 font-mono text-xs whitespace-pre-wrap text-code-ink"
          aria-label={t("tool.sqlExecuted")}
        >
          {executed}
        </pre>
      ) : null}
      {parsed.table && parsed.table.rows.length > 0 ? (
        <ResultTable
          columns={parsed.table.columns}
          rows={parsed.table.rows}
          label={t("tool.sqlTable")}
          className="max-h-64"
        />
      ) : null}
      {csvPath ? (
        <p className="flex items-center gap-1.5 text-xs">
          <FileText className="size-3.5 shrink-0 text-ink-subtle" aria-hidden />
          {href ? (
            <a
              href={href}
              download
              className="inline-flex items-center gap-1 text-accent hover:underline"
            >
              {t("tool.sqlCsv")}
              <Download className="size-3.5" aria-hidden />
            </a>
          ) : (
            <span className="text-ink-muted">{t("tool.sqlCsvPending", { path: csvPath })}</span>
          )}
        </p>
      ) : null}
    </div>
  );
}

export function ToolCallCard({
  toolCallId,
  citationLinks = false,
  fileHref,
}: {
  toolCallId: string;
  citationLinks?: boolean;
  // 작업공간 파일(세션 파일) 내려받기 주소. query_sql의 잘린 결과 CSV에 쓴다.
  fileHref?: FileHref;
}) {
  const name = useRunStore((state) => state.toolCalls[toolCallId]?.name);
  if (name === CALL_AGENT) return <ChildRunCard toolCallId={toolCallId} />;
  return (
    <BasicToolCard toolCallId={toolCallId} citationLinks={citationLinks} fileHref={fileHref} />
  );
}

function BasicToolCard({
  toolCallId,
  citationLinks,
  fileHref,
}: {
  toolCallId: string;
  citationLinks: boolean;
  fileHref?: FileHref;
}) {
  const { t } = useTranslation("chat");
  const tool = useRunStore((state) => state.toolCalls[toolCallId]);
  const [open, setOpen] = useState(false);
  if (!tool) return null;

  const summary = summarizeInput(tool.name, tool.input, tool.inputText);
  const done = tool.phase === "done";
  const restoredResult = tool.restored && tool.result !== "cancelled";
  const showDownload =
    done &&
    (tool.droppedChars > 0 ||
      tool.outputIsExcerpt ||
      Boolean(tool.outputStorageKey) ||
      restoredResult);
  const outputUrl = `/api/console/runs/${encodeURIComponent(tool.runId)}/tool-calls/${encodeURIComponent(tool.id)}/output`;

  return (
    <Collapsible.Root
      open={open}
      onOpenChange={setOpen}
      className={cn(
        "rounded-md border bg-surface",
        tool.phase === "awaiting_approval" ? "border-warn/60" : "border-line",
      )}
    >
      <Collapsible.Trigger className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left hover:bg-hover">
        <ChevronRight
          className={cn(
            "size-3.5 shrink-0 text-ink-subtle transition-transform",
            open && "rotate-90",
          )}
          aria-hidden
        />
        <ToolIcon name={tool.name} className="size-4 shrink-0 text-ink-muted" />
        <span className="shrink-0 font-mono text-xs font-medium text-ink">{tool.name || "…"}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink-muted">{summary}</span>
        <StatusBadge tool={tool} />
        {tool.durationMs !== null ? (
          <span className="shrink-0 text-xs text-ink-subtle tabular-nums">
            {formatDuration(tool.durationMs)}
          </span>
        ) : null}
      </Collapsible.Trigger>
      {tool.name === SEARCH_KNOWLEDGE && done ? (
        <KnowledgeSummary tool={tool} citationLinks={citationLinks} />
      ) : null}
      {tool.name === QUERY_SQL && done ? <SqlSummary tool={tool} fileHref={fileHref} /> : null}
      <Collapsible.Content className="flex flex-col gap-3 border-t border-line px-3 py-3">
        <section>
          <h4 className="mb-1 text-xs font-medium text-ink-muted">{t("tool.input")}</h4>
          <pre className="max-h-64 overflow-auto rounded-md bg-code px-3 py-2 font-mono text-xs whitespace-pre-wrap text-code-ink">
            {tool.input ? prettyJson(tool.input) : tool.inputText || "…"}
          </pre>
        </section>
        <section>
          <h4 className="mb-1 text-xs font-medium text-ink-muted">{t("tool.output")}</h4>
          {tool.droppedChars > 0 ? (
            <p className="mb-1 text-xs text-ink-muted">
              {t("tool.dropped", { count: formatNumber(tool.droppedChars) })}
            </p>
          ) : null}
          {tool.outputIsExcerpt ? (
            <p className="mb-1 text-xs text-ink-muted">{t("tool.excerpt")}</p>
          ) : null}
          {restoredResult ? (
            <p className="mb-1 text-xs text-ink-muted">{t("tool.restored")}</p>
          ) : null}
          <ToolOutput tool={tool} />
        </section>
        {done ? (
          <footer className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-ink-muted">
            {tool.exitCode !== null ? (
              <span className={tool.exitCode === 0 ? undefined : "text-danger"}>
                {t("tool.exitCode", { code: tool.exitCode })}
              </span>
            ) : null}
            {tool.durationMs !== null ? <span>{formatDuration(tool.durationMs)}</span> : null}
            {tool.peakMemoryBytes ? (
              <span>{t("tool.peakMemory", { size: formatBytes(tool.peakMemoryBytes) })}</span>
            ) : null}
            {tool.outputBytes !== null ? (
              <span>{t("tool.outputSize", { size: formatBytes(tool.outputBytes) })}</span>
            ) : null}
            {showDownload ? (
              <a
                href={outputUrl}
                className="inline-flex items-center gap-1 text-accent hover:underline"
                download
              >
                <Download className="size-3.5" aria-hidden />
                {t("tool.fullOutput")}
              </a>
            ) : null}
          </footer>
        ) : null}
      </Collapsible.Content>
    </Collapsible.Root>
  );
}
